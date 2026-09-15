"""团体名单（roster）：身份与授权的边界。

这是设计文档 §5 的可执行实现，验证规则 1–6 逐条对应 `Roster.accepts()`。

## 两个权威面（§3.3）

名单只回答"谁的名单里有谁、谁是群主/管理员"。**群主与管理员对成员设备上的数据
没有任何特权** —— 数据访问由各设备属主自己签发的共享项 ACL 决定（见 shares.py）。
本模块不提供任何"管理员可访问他人文件"的路径。

## 版本链与收敛（§5.2 / §5.3）

名单是**链式**的：`version` 单调递增，`prev` 指向前一份名单的哈希。
规则 2 保证一份名单只有一个直接后继，于是并发签发（两名管理员同时改名单）
产生的两份候选必然 `version` 与 `prev` 都相同 —— 此时按**名单哈希字典序取较小者**
收敛，各节点无需通信即可得到同一结果。被丢弃的一份需要重新签发。

## 宽限策略（§5.4）

**软件不做版本强制**：`expires` 只是提示字段，到期不阻断通信；节点也不因对方
名单较旧而拒绝连接（见 `staleness_report()`）。需要写明后果：移除成员的有效
窗口没有上界 —— 被移除者与尚未更新的节点之间仍可通信，直到后者更新。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import crypto_prims as cp
from .records import (
    RecordError,
    SignedRecord,
    b64_field,
    b64_list_field,
    encode_fields,
    int_field,
    pretty,
    str_field,
)

# 名单里的角色。owner 唯一，admin 可以多名，member 是其余主体。
ROLE_OWNER = 'owner'
ROLE_ADMIN = 'admin'
ROLE_MEMBER = 'member'

# 默认有效期：180 天。§5.4 明确它只是提示，不阻断通信。
DEFAULT_TTL_SECONDS = 180 * 24 * 3600


@dataclass
class RosterEntry:
    """名单里的一个成员：主体 + 该主体已授权的设备公钥集合。"""

    name: str
    principal_key: bytes
    device_keys: List[bytes] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.principal_key[:8].hex()

    def to_dict(self) -> Dict[str, Any]:
        return {'name': self.name, 'pubkey': cp.b64(self.principal_key),
                'devices': [cp.b64(d) for d in self.device_keys]}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'RosterEntry':
        return RosterEntry(name=str_field(data, 'name'),
                           principal_key=b64_field(data, 'pubkey', 32),
                           device_keys=b64_list_field(data, 'devices', expect_len=32))


@dataclass
class Roster(SignedRecord):
    """一份团体名单。

    `signature` 只有在 `sign()` 之后才有值；从线上收到的名单一律先 `verify()` 再
    `accepts()`，顺序不能反 —— 未验签的字段不可信，用它们做决策等于把授权交给
    任何能发包的人。
    """

    group: str
    version: int
    prev: Optional[bytes]
    expires: int
    owner_key: bytes
    admin_keys: List[bytes]
    members: List[RosterEntry]
    signature: bytes = b''

    FIELDS = ('group', 'version', 'prev', 'expires', 'owner', 'admins', 'members')

    # ── 序列化 ────────────────────────────────────────────────────────────

    def _to_fields(self) -> Dict[str, Any]:
        return {
            'group': self.group,
            'version': self.version,
            'prev': cp.b64(self.prev) if self.prev else None,
            'expires': self.expires,
            'owner': cp.b64(self.owner_key),
            'admins': [cp.b64(k) for k in self.admin_keys],
            'members': [m.to_dict() for m in self.members],
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self._to_fields()
        data['sig'] = cp.b64(self.signature)
        return data

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'Roster':
        prev_raw = data.get('prev')
        if prev_raw is not None and not isinstance(prev_raw, str):
            raise RecordError('字段 prev 必须是 base64 字符串或 null')
        members_raw = data.get('members')
        if not isinstance(members_raw, list):
            raise RecordError('字段 members 必须是数组')
        roster = Roster(
            group=str_field(data, 'group'),
            version=int_field(data, 'version', minimum=0),
            prev=cp.b64d(prev_raw) if prev_raw else None,
            expires=int_field(data, 'expires', minimum=0),
            owner_key=b64_field(data, 'owner', 32),
            admin_keys=b64_list_field(data, 'admins', expect_len=32),
            members=[RosterEntry.from_dict(m) for m in members_raw],
            signature=b64_field(data, 'sig', 64),
        )
        roster.validate_structure()
        return roster

    def validate_structure(self) -> None:
        """结构性检查：与"相对某份旧名单"无关的约束都在这里。"""
        if not self.members:
            raise RecordError('名单至少要有一名成员')
        principals = [m.principal_key for m in self.members]
        if len(set(principals)) != len(principals):
            raise RecordError('同一主体在名单里出现多次')
        devices = [d for m in self.members for d in m.device_keys]
        if len(set(devices)) != len(devices):
            raise RecordError('同一设备公钥出现在多个成员名下')
        if self.owner_key not in principals:
            raise RecordError('群主主体不在成员列表里')
        for admin in self.admin_keys:
            if admin not in principals:
                raise RecordError(f'管理员 {admin[:8].hex()} 不在成员列表里')
        if len(set(self.admin_keys)) != len(self.admin_keys):
            raise RecordError('管理员列表含重复项')
        if self.owner_key in self.admin_keys:
            raise RecordError('群主不应同时出现在管理员列表里（群主权限已更高）')

    # ── 身份查询 ──────────────────────────────────────────────────────────

    @property
    def content_hash(self) -> bytes:
        """名单内容的规范化哈希，即后继名单 `prev` 字段的取值。

        只对**参与签名的字段**求值（不含 `sig` 本身）：同一份名单内容无论由谁
        重签，`prev` 链的指向都一致，规则 2 的"直接后继"判定因此稳定。
        """
        return cp.blake2s(encode_fields(self._to_fields()))

    def entry_of(self, principal_key: bytes) -> Optional[RosterEntry]:
        for member in self.members:
            if member.principal_key == principal_key:
                return member
        return None

    def entry_of_device(self, device_key: bytes) -> Optional[RosterEntry]:
        """按**设备**公钥反查成员。

        设计文档 §3.3 要求"资源属主按主体判定，不按设备判定"，因此授权最终一定
        落到 `entry_of()` 的结果上；本方法只用于握手阶段"这台设备属于哪个主体"。
        """
        for member in self.members:
            if device_key in member.device_keys:
                return member
        return None

    def role_of(self, principal_key: bytes) -> Optional[str]:
        if principal_key == self.owner_key:
            return ROLE_OWNER
        if principal_key in self.admin_keys:
            return ROLE_ADMIN
        if self.entry_of(principal_key) is not None:
            return ROLE_MEMBER
        return None

    def contains_principal(self, principal_key: bytes) -> bool:
        return self.entry_of(principal_key) is not None

    def contains_device(self, device_key: bytes) -> bool:
        return self.entry_of_device(device_key) is not None

    def is_expired(self, now: Optional[int] = None) -> bool:
        now = int(time.time()) if now is None else now
        return now > self.expires

    # ── 验签与验证规则 ────────────────────────────────────────────────────

    def verify_signature(self) -> Tuple[bool, Optional[bytes]]:
        """尝试用群主与各管理员公钥验签，返回 (是否通过, 签名者公钥)。

        规则 3：签名者必须属于 `{owner} ∪ admins`。这里只判断"谁签的"，
        "这个人有没有资格签这份内容"由规则 4/5 判定（见 `accepts`）。
        """
        for candidate in [self.owner_key, *self.admin_keys]:
            if self.verify(candidate, self.signature):
                return True, candidate
        return False, None

    def sign(self, private_key: bytes) -> bytes:
        self.signature = super().sign(private_key)
        return self.signature

    def accepts(self, current: Optional['Roster'], now: Optional[int] = None) -> None:
        """按设计文档 §5.2 的规则 1–6 验证"本名单能否替换 `current`"。

        通过则返回 None，不通过抛 `RecordError`（消息里带上是哪条规则判掉的）。

        `current is None` 表示本地还没有名单 —— 规则 6：直接信任带外分发的创始名单。
        注意"带外"这三个字是安全前提：创始名单必须经可信渠道（局域网配对、面对面
        二维码、用户手工核对短标识）送达。若从网络上随便收到一份名单就当创世锚，
        规则 6 就变成了任意人可建团的入口。
        """
        self.validate_structure()

        ok, signer = self.verify_signature()
        if not ok:
            raise RecordError('规则 3：名单签名无法用群主或管理员公钥验证')

        if current is None:
            # 规则 6：信任锚起点
            return

        current.validate_structure()

        # 规则 1：防回滚
        if self.version <= current.version:
            raise RecordError(f'规则 1：版本未前进（新 {self.version} <= 当前 {current.version}）')
        # 规则 2：只接受直接后继
        if self.prev != current.content_hash:
            raise RecordError('规则 2：prev 与当前名单的哈希不符（不是直接后继）')
        # 团体标识不得改变（同一份名单链只描述一个团体）
        if self.group != current.group:
            raise RecordError(f'团体标识不符：{self.group} != {current.group}')

        if signer == current.owner_key:
            # 规则 4：群主全权（含转移群主、变更管理员集合）
            return

        # 规则 5：管理员只能增删普通成员
        if signer not in current.admin_keys:
            raise RecordError('规则 5：签名者既不是当前群主也不是当前管理员')
        if self.owner_key != current.owner_key:
            raise RecordError('规则 5：管理员不得变更群主')
        if set(self.admin_keys) != set(current.admin_keys):
            raise RecordError('规则 5：管理员不得变更管理员集合（含自我提权）')

    # ── 冲突收敛（§5.3）──────────────────────────────────────────────────

    @staticmethod
    def pick_concurrent(a: 'Roster', b: 'Roster') -> 'Roster':
        """`version` 与 `prev` 相同的两份候选，按名单哈希字典序取较小者。

        该规则是确定性的：各节点无需通信即可对"谁的后继有效"达成一致。
        """
        if (a.version, a.prev) != (b.version, b.prev):
            raise RecordError('只能对同版本、同 prev 的候选做收敛')
        return a if a.content_hash <= b.content_hash else b


# ── 签发助手：把"改名单"这件事写成受约束的操作 ──────────────────────────────

def next_roster(current: Optional[Roster], group: str, owner_key: bytes,
                members: List[RosterEntry], admin_keys: Optional[List[bytes]] = None,
                ttl_seconds: int = DEFAULT_TTL_SECONDS,
                now: Optional[int] = None) -> Roster:
    """基于 `current` 构造一份后继名单（尚未签名）。

    签名者由调用方决定；`Roster.accepts()` 会在接收端重新判定签名者资格，
    因此本函数**不做**权限检查 —— 权限是验证方的事，不是构造方的事。
    """
    now = int(time.time()) if now is None else now
    return Roster(
        group=group,
        version=1 if current is None else current.version + 1,
        prev=None if current is None else current.content_hash,
        expires=now + ttl_seconds,
        owner_key=owner_key,
        admin_keys=list(admin_keys if admin_keys is not None else (current.admin_keys if current else [])),
        members=list(members),
    )


def founding_roster(group: str, owner: 'PrincipalLike', device_keys: List[bytes],
                    ttl_seconds: int = DEFAULT_TTL_SECONDS,
                    now: Optional[int] = None) -> Roster:
    """创建团体的创始名单：群主是唯一成员，带上一台自己的设备。

    设计文档 §5.2 规则 6 的"信任锚起点"。返回的名单**已签名**（群主私钥）。
    """
    entry = RosterEntry(name=owner.name, principal_key=owner.public_key,
                        device_keys=list(device_keys))
    roster = next_roster(None, group=group, owner_key=owner.public_key, members=[entry],
                         admin_keys=[], ttl_seconds=ttl_seconds, now=now)
    roster.sign(owner.private_key)
    return roster


class PrincipalLike:
    """`founding_roster` 需要的形状（避免 identity 与 roster 互相 import）。"""

    name: str
    public_key: bytes
    private_key: bytes


# ── 宽限策略的界面化表达（§5.4）─────────────────────────────────────────────

def staleness_report(local: Roster, remote: Optional[Roster], now: Optional[int] = None) -> Dict[str, Any]:
    """生成"你的名单是否过期 / 与对方是否一致"的提示数据。

    按 §5.4，这些信息**只用于提示，不阻断连接**。
    """
    now = int(time.time()) if now is None else now
    expired_days = max(0, (now - local.expires) // 86400) if local.is_expired(now) else 0
    report: Dict[str, Any] = {
        'local_version': local.version,
        'expires': local.expires,
        'expired': local.is_expired(now),
        'expired_days': int(expired_days),
        'consistent': None,
    }
    if remote is not None:
        report['remote_version'] = remote.version
        report['consistent'] = remote.content_hash == local.content_hash
        report['remote_newer'] = remote.version > local.version
        report['local_newer'] = remote.version < local.version
    return report


def describe_roster(roster: Roster) -> str:
    return pretty(roster.to_dict())
