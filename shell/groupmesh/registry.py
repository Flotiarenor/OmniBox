"""注册与发现（设计文档 §7）。

## 与名单解耦（§7.2）

    团体名单：群主/管理员签发，低频变更，内容是"主体、角色、设备公钥"
    注册记录：**设备自己**签发，高频变更，内容是"设备公钥、当前端点、共享清单、序号"

注册是成员自助行为，不需要管理员参与，因此成员开关机不影响团体管理。

## 同步方式（§7.3）

注册记录是小数据集（按 200 台设备估算约 20～40 KB），因此**不使用 DHT**：
任一在线共享节点都能提供完整快照；新上线设备拉一次快照，之后按 `seq` 增量同步。
记录带设备自身签名，中继方无法伪造内容 —— 这也是 `Registry.add()` 只接受
**设备自签**记录的原因。

## 单调性

注册记录是可重放的小对象，因此必须防回滚：同一设备的 `seq` 只能严格递增，
否则任何人都能拿旧记录把别人的端点覆盖掉（旧记录签名依然有效）。
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import crypto_prims as cp
from .records import RecordError, SignedRecord, b64_field, int_field, pretty, str_list_field

# 端点：["240e:xxxx::1", 18443] 或 ["192.168.31.4", 19443]
Endpoint = Tuple[str, int]

# 单条注册记录的端点数量上限（§4.5/§7 的边界检查要求）
MAX_ENDPOINTS = 16


def _validate_endpoints(raw: Any) -> List[Endpoint]:
    if not isinstance(raw, list):
        raise RecordError('字段 endpoints 必须是数组')
    if len(raw) > MAX_ENDPOINTS:
        raise RecordError(f'endpoints 最多 {MAX_ENDPOINTS} 项，收到 {len(raw)}')
    endpoints: List[Endpoint] = []
    for item in raw:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or not isinstance(item[0], str) or not isinstance(item[1], int)
                or isinstance(item[1], bool)):
            raise RecordError(f'端点格式必须是 [地址, 端口]，收到 {item!r}')
        address, port = item[0], item[1]
        try:
            ipaddress.ip_address(address)
        except ValueError as e:
            raise RecordError(f'端点地址非法: {address!r}') from e
        if not 1 <= port <= 65535:
            raise RecordError(f'端点端口越界: {port}')
        endpoints.append((address, port))
    return endpoints


@dataclass
class Registration(SignedRecord):
    """一台共享节点的注册记录。由设备私钥自签。"""

    device_key: bytes
    seq: int
    endpoints: List[Endpoint]
    shares: List[str]
    ts: int
    signature: bytes = b''

    FIELDS = ('device', 'seq', 'endpoints', 'shares', 'ts')

    def _to_fields(self) -> Dict[str, Any]:
        return {'device': cp.b64(self.device_key), 'seq': self.seq,
                'endpoints': [[a, p] for a, p in self.endpoints],
                'shares': list(self.shares), 'ts': self.ts}

    def to_dict(self) -> Dict[str, Any]:
        data = self._to_fields()
        data['sig'] = cp.b64(self.signature)
        return data

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'Registration':
        shares = str_list_field(data, 'shares')
        for share_id in shares:
            from .shares import SHARE_ID_RE
            if not SHARE_ID_RE.match(share_id):
                raise RecordError(f'注册记录里的 share_id 非法: {share_id!r}')
        return Registration(
            device_key=b64_field(data, 'device', 32),
            seq=int_field(data, 'seq', minimum=0),
            endpoints=_validate_endpoints(data.get('endpoints')),
            shares=shares,
            ts=int_field(data, 'ts', minimum=0),
            signature=b64_field(data, 'sig', 64),
        )

    def verify_signature(self) -> bool:
        return self.verify(self.device_key, self.signature)

    def sign(self, private_key: bytes) -> bytes:
        self.signature = super().sign(private_key)
        return self.signature

    @property
    def device_id(self) -> str:
        return self.device_key[:8].hex()

    def describe(self) -> str:
        return pretty(self.to_dict())


def new_registration(device_key: bytes, device_private_key: bytes, seq: int,
                     endpoints: Iterable[Endpoint], shares: Iterable[str],
                     ts: Optional[int] = None) -> Registration:
    """签发一条注册记录（设备自助，无需管理员参与）。"""
    if cp.sign_public_from_private(device_private_key) != device_key:
        raise RecordError('设备私钥与 device 公钥不匹配（拒绝用错误的密钥签发注册）')
    registration = Registration(device_key=device_key, seq=seq,
                                endpoints=_validate_endpoints([[a, p] for a, p in endpoints]),
                                shares=list(shares), ts=int(time.time()) if ts is None else ts)
    registration.sign(device_private_key)
    return registration


@dataclass
class Registry:
    """本地持有的注册记录集合（设备公钥 -> 记录）。"""

    records: Dict[bytes, Registration] = field(default_factory=dict)

    # ── 增改 ──────────────────────────────────────────────────────────────

    def add(self, registration: Registration) -> bool:
        """接收一条注册记录。返回 True 表示被采纳。

        拒绝的情形（全部静默返回 False，由调用方决定是否记日志）：
          * 签名无法用 `device` 公钥验证；
          * 同设备已有记录的 `seq` 不小于新记录（防回滚 / 防重放）。

        为什么必须严格大于而不是大于等于：等值记录可能是重放，而"同一 seq 两条
        内容不同的记录"意味着同设备自相矛盾，采纳谁都会让不同节点的视图分叉。
        """
        if not registration.verify_signature():
            return False
        current = self.records.get(registration.device_key)
        if current is not None and registration.seq <= current.seq:
            return False
        self.records[registration.device_key] = registration
        return True

    def merge(self, other: 'Registry') -> int:
        """并入另一份注册表视图，返回被采纳的记录数（§7.3 的增量同步）。"""
        return sum(1 for record in other.records.values() if self.add(record))

    # ── 查询 ──────────────────────────────────────────────────────────────

    def get(self, device_key: bytes) -> Optional[Registration]:
        return self.records.get(device_key)

    def devices_sharing(self, share_id: str) -> List[Registration]:
        """哪些设备声明提供某个共享项（按 seq 无关，取当前视图）。"""
        return [r for r in self.records.values() if share_id in r.shares]

    def all_endpoints(self) -> List[Endpoint]:
        seen: List[Endpoint] = []
        for record in self.records.values():
            for endpoint in record.endpoints:
                if endpoint not in seen:
                    seen.append(endpoint)
        return seen

    def snapshot(self) -> List[Registration]:
        """完整快照（§7.3：任一在线共享节点都能提供）。"""
        return list(self.records.values())

    def snapshot_dicts(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.records.values()]

    @staticmethod
    def from_snapshot_dicts(items: Any) -> 'Registry':
        """由快照构造注册表。**逐条独立校验**：一条坏记录不影响其余记录。"""
        registry = Registry()
        if not isinstance(items, list):
            raise RecordError('注册快照必须是数组')
        for item in items:
            try:
                registry.add(Registration.from_dict(item))
            except RecordError:
                # 单条损坏 / 验签失败：跳过，不让整个快照作废
                continue
        return registry

    def __len__(self) -> int:
        return len(self.records)

    def describe(self) -> str:
        return pretty({'count': len(self.records), 'records': self.snapshot_dicts()})


# ── 本机端点探测（§7.4 稳定地址）───────────────────────────────────────────

def local_addresses(prefer_ipv6: bool = True) -> List[str]:
    """列出本机可用于注册的地址。

    设计文档 §7.4 要求使用**稳定地址**（EUI-64 或 RFC 7217 stable-privacy），
    不使用 Windows 默认启用的 RFC 4941 临时地址（会轮换）。Python 标准库拿不到
    "该地址是否是临时地址"这一信息，因此这里只做"排除回环与链路本地"的粗过滤，
    **临时地址的识别留作待实测项**（见实现路径文档 P1）。"""
    import socket

    addresses: List[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, proto=socket.IPPROTO_TCP)
    except OSError:
        infos = []
    for family, _, _, _, sockaddr in infos:
        address = sockaddr[0]
        if family == socket.AF_INET6:
            address = address.split('%')[0]
            if address.startswith('fe80') or address == '::1':
                continue
        elif family == socket.AF_INET:
            if address.startswith('127.'):
                continue
        if address not in addresses:
            addresses.append(address)
    if prefer_ipv6:
        addresses.sort(key=lambda a: 0 if ':' in a else 1)
    return addresses
