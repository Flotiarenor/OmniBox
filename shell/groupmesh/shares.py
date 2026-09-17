"""共享项声明与 ACL 判定（设计文档 §6）。

## 谁签发什么

共享项声明由**设备自己的私钥**签发，绑定的属主是该设备所属的**主体**（§3.3：
资源属主按主体判定，不按设备判定）。因此同一主体的手机访问自己主力机上的
共享项时按属主处理。

## 判定在哪做（§6.3）

判定在**属主设备**上执行，依据是发起方主体的公钥 —— 该公钥来自 Noise 握手中
已验证的静态密钥，**不接受请求参数里的自称身份**。本模块的 `Authorizer` 只接受
调用方传入的"已经过握手认证的主体公钥"，从而在结构上消除了"客户端自称"这条路。

## 写权限只能新增（§6.2）

写入是**可加**的：协议只提供"新增文件"与"提交上传"，不提供"减少内容"（§6.4）。
因此判定只看 ACL，**不记录 `created_by`**：写权限是"能不能新增"，与"谁传的"无关。

## ACL 取值

`read` / `write` 两项取值均为 `"owner"`、`"group"` 或主体公钥数组。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from . import crypto_prims as cp
from .records import RecordError, SignedRecord, b64_field, int_field, pretty, str_field

# ACL 的两个符号值
ACL_OWNER = 'owner'
ACL_GROUP = 'group'

# 共享标识只允许这些字符：它会出现在请求路径与日志里，必须限制字符集，
# 避免有人用 ".." / 分隔符 / 控制字符构造出意料之外的路径。
SHARE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')

# 默认单共享项容量上限：**不限制**（None）。
#
# 曾经默认 1 GiB，理由是 §6.5 的"授予写权限等于允许对方占用磁盘"。按实测把它
# 放宽为默认不限制，原因是那个上限**限不住**：`node.py` 的判定是"每条连接量一次
# 基线，之后按基线推算"，于是 N 条并发连接各自量到基线 0，全部通过。实测 8 条
# 并发连接在"上限 1000 字节"的共享项上写进了 3200 字节，且全部返回成功。
#
# 结论：协议侧的容量上限只是**事后估算**，挡不住并发，也挡不住别人的进程往同一
# 目录写。既然它给不出可信的保证，就不要用它做默认防线 —— 需要时由属主显式设置
# （`LocalShare.max_bytes`，`None` = 不限制），并能看到它只是尽力而为。
#
# 真正的防线是"不要给不信任的人 write 权限"（§6.2 的写权限是**可加**的）。
DEFAULT_MAX_BYTES: Optional[int] = None


class Permission(Enum):
    READ = 'read'
    WRITE = 'write'


def _validate_acl_value(value: Any, permission: str) -> Union[str, List[bytes]]:
    """校验一项 ACL 取值。返回 `'owner'` / `'group'` 或主体公钥列表。"""
    if isinstance(value, str):
        if value not in (ACL_OWNER, ACL_GROUP):
            raise RecordError(f'ACL.{permission} 的字符串取值只能是 owner 或 group，收到 {value!r}')
        return value
    if isinstance(value, list):
        if not value:
            raise RecordError(f'ACL.{permission} 的数组不得为空（空数组应写成 owner 或 group）')
        keys: List[bytes] = []
        for item in value:
            if not isinstance(item, str):
                raise RecordError(f'ACL.{permission} 的元素必须是 base64 字符串')
            key = cp.b64d(item)
            if len(key) != 32:
                raise RecordError(f'ACL.{permission} 的公钥长度应为 32，实际 {len(key)}')
            keys.append(key)
        return keys
    raise RecordError(f'ACL.{permission} 必须是 owner / group / 公钥数组之一')


def _acl_to_json(value: Union[str, List[bytes]]) -> Any:
    if isinstance(value, str):
        return value
    return [cp.b64(k) for k in value]


@dataclass
class Acl:
    """一个共享项的两档权限。"""

    read: Union[str, List[bytes]] = ACL_GROUP
    write: Union[str, List[bytes]] = ACL_OWNER

    def to_dict(self) -> Dict[str, Any]:
        return {'read': _acl_to_json(self.read), 'write': _acl_to_json(self.write)}

    @staticmethod
    def from_dict(data: Any) -> 'Acl':
        if not isinstance(data, dict):
            raise RecordError('acl 必须是对象')
        missing = [p for p in ('read', 'write') if p not in data]
        if missing:
            raise RecordError(f'acl 缺少字段: {", ".join(missing)}')
        return Acl(read=_validate_acl_value(data['read'], 'read'),
                   write=_validate_acl_value(data['write'], 'write'))

    def allows(self, permission: Permission, requester_key: bytes, owner_key: bytes,
               group_member: bool) -> bool:
        """判定 `requester_key` 是否具备该权限。

        `group_member` 由调用方按**当前团体名单**判定后传入 —— 本模块不持有名单，
        因此不可能出现"用过期名单放行"这种耦合。
        """
        value = {'read': self.read, 'write': self.write}[permission.value]
        if isinstance(value, str):
            if value == ACL_OWNER:
                return requester_key == owner_key
            # ACL_GROUP：群主本人当然是成员，但这里显式让群主通过，
            # 以免调用方漏传 group_member 时把属主锁在门外（§3.3：群主无数据特权，
            # 他通过 ACL 里的 owner 分支或成员身份获得权限）。
            return requester_key == owner_key or group_member
        return requester_key == owner_key or requester_key in value


@dataclass
class ShareDeclaration(SignedRecord):
    """共享项声明：设备对外声明的"我有一个共享项，权限如下"。

    声明本身**不含本机路径** —— 路径是本机配置，不进入团体可见的元数据，
    避免把宿主机的目录结构泄露给所有成员。
    """

    node_key: bytes          # 提供该共享项的设备公钥
    owner_key: bytes         # 该设备所属主体的公钥（资源属主）
    share_id: str
    seq: int
    acl: Acl
    signature: bytes = b''

    FIELDS = ('node', 'owner', 'share_id', 'seq', 'acl')

    def _to_fields(self) -> Dict[str, Any]:
        return {'node': cp.b64(self.node_key), 'owner': cp.b64(self.owner_key),
                'share_id': self.share_id, 'seq': self.seq, 'acl': self.acl.to_dict()}

    def to_dict(self) -> Dict[str, Any]:
        data = self._to_fields()
        data['sig'] = cp.b64(self.signature)
        return data

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'ShareDeclaration':
        share = ShareDeclaration(
            node_key=b64_field(data, 'node', 32),
            owner_key=b64_field(data, 'owner', 32),
            share_id=str_field(data, 'share_id'),
            seq=int_field(data, 'seq', minimum=0),
            acl=Acl.from_dict(data.get('acl')),
            signature=b64_field(data, 'sig', 64),
        )
        if not SHARE_ID_RE.match(share.share_id):
            raise RecordError(f'share_id 非法: {share.share_id!r}（只允许字母数字与 . _ -）')
        return share

    def verify_signature(self) -> bool:
        """§6.1：声明由 `node` 的**设备私钥**签名。"""
        return self.verify(self.node_key, self.signature)

    def sign(self, private_key: bytes) -> bytes:
        self.signature = super().sign(private_key)
        return self.signature

    def allows(self, permission: Permission, requester_key: bytes, group_member: bool) -> bool:
        return self.acl.allows(permission, requester_key, self.owner_key, group_member)

    def describe(self) -> str:
        return pretty(self.to_dict())


# ── 本机共享项（声明 + 本机路径 + 运行时状态）───────────────────────────────

@dataclass
class LocalShare:
    """本机的一个共享项：对外声明 + 只在本机保存的路径与容量设置。"""

    declaration: ShareDeclaration
    path: str
    max_bytes: Optional[int] = DEFAULT_MAX_BYTES

    @property
    def share_id(self) -> str:
        return self.declaration.share_id

    def to_dict(self) -> Dict[str, Any]:
        return {'declaration': self.declaration.to_dict(), 'path': self.path,
                'max_bytes': self.max_bytes}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'LocalShare':
        max_bytes = data.get('max_bytes', DEFAULT_MAX_BYTES)
        if max_bytes is not None and (not isinstance(max_bytes, int) or isinstance(max_bytes, bool)):
            raise RecordError('max_bytes 必须是整数或 null')
        share = LocalShare(
            declaration=ShareDeclaration.from_dict(data.get('declaration') or {}),
            path=str_field(data, 'path'),
            max_bytes=max_bytes,
        )
        if not share.declaration.verify_signature():
            raise RecordError(f'共享项 {share.share_id} 的声明验签失败（文件被改动）')
        return share


def new_share(share_id: str, path: str, owner_key: bytes, node_key: bytes,
              node_private_key: bytes, acl: Optional[Acl] = None,
              seq: int = 1, max_bytes: Optional[int] = DEFAULT_MAX_BYTES) -> LocalShare:
    """构造并签发一个本机共享项。`node_private_key` 必须是 `node_key` 的私钥。"""
    if not SHARE_ID_RE.match(share_id):
        raise RecordError(f'share_id 非法: {share_id!r}')
    if cp.sign_public_from_private(node_private_key) != node_key:
        raise RecordError('设备私钥与 node 公钥不匹配（拒绝用错误的密钥签发共享项）')
    declaration = ShareDeclaration(node_key=node_key, owner_key=owner_key, share_id=share_id,
                                   seq=seq, acl=acl or Acl())
    declaration.sign(node_private_key)
    return LocalShare(declaration=declaration, path=path, max_bytes=max_bytes)


# ── 授权判定入口 ──────────────────────────────────────────────────────────

@dataclass
class Authorizer:
    """把"握手认证出的主体公钥"翻译成"允许/拒绝"。

    刻意设计成**必须显式传入** `requester_key` 与 `group_member`：没有"默认放行"
    的分支，也没有从请求体读身份的口子。调用方拿到的是 Noise 握手验证过的静态
    密钥对应的主体，伪造请求参数无法改变它。
    """

    requester_key: bytes
    group_member: bool
    requester_device_key: Optional[bytes] = None

    def allows(self, declaration: ShareDeclaration, permission: Permission) -> bool:
        return declaration.allows(permission, self.requester_key, self.group_member)

    def check(self, declaration: ShareDeclaration, permission: Permission) -> None:
        if not self.allows(declaration, permission):
            who = self.requester_key[:8].hex()
            raise AclDenied(f'主体 {who} 对共享项 {declaration.share_id} 没有 {permission.value} 权限')


class AclDenied(Exception):
    """ACL 判定失败。协议层把它转成 403 语义的应答。"""


def describe_share(share: LocalShare) -> str:
    return pretty(share.to_dict())
