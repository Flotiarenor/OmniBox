"""主体（principal）与设备（device）身份。

设计文档 §4.1/§4.2 的关键约束：**权限绑定在主体上，不绑定在设备上**。
一个主体可以有多台设备；每台设备持有自己的密钥对。团体名单里成员以主体公钥
标识，并把该主体已授权的设备公钥列在 `devices` 下（见 roster.py）。

本模块负责密钥的**生成、落盘与读取**，以及"设备凭据"这一层绑定关系的表达。

## 落盘布局

    身份目录/
      principal.json      主体：名称 + 长期私钥（主密钥）
      devices/<dev_id>.json  设备：名称 + 设备私钥

## 明文私钥的定位（重要，需在实现路径文档中标注为待办）

设计文档要求"设备私钥不导出，由操作系统提供的密钥存储保存（Windows 为 DPAPI，
Android 为 Keystore）"。MVP 期间私钥是**明文落盘的**，仅靠文件权限保护，
且密钥文件已声明为受保护路径（不给文件服务端出去）。这是 MVP 与设计文档之间
已知的偏离，上生产前必须换成 DPAPI / keyring。
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import crypto_prims as cp
from .records import RecordError, b64_field, pretty, str_field


def short_id(public_key: bytes) -> str:
    """把公钥压成便于人读的短标识：前 8 字节的十六进制。

    只用于界面展示与文件名；**任何授权判定都必须用完整公钥**，不得用短标识。
    """
    return public_key[:8].hex()


@dataclass
class Principal:
    """一个主体：人。持有长期密钥对，可以拥有多台设备。"""

    name: str
    private_key: bytes
    public_key: bytes

    @property
    def id(self) -> str:
        return short_id(self.public_key)

    @staticmethod
    def create(name: str) -> 'Principal':
        private_key, public_key = cp.generate_sign_keypair()
        return Principal(name=name, private_key=private_key, public_key=public_key)

    def to_dict(self) -> Dict[str, Any]:
        return {'name': self.name, 'private_key': cp.b64(self.private_key),
                'public_key': cp.b64(self.public_key)}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'Principal':
        name = str_field(data, 'name')
        private_key = b64_field(data, 'private_key', 32)
        public_key = b64_field(data, 'public_key', 32)
        derived = cp.sign_public_from_private(private_key)
        if derived != public_key:
            raise RecordError('主体私钥与公钥不匹配（文件被篡改或版本不兼容）')
        return Principal(name=name, private_key=private_key, public_key=public_key)


@dataclass
class Device:
    """一台设备。持有自己的密钥对，归属于某个主体。

    设备有**两把由同一份种子派生的密钥**（设计文档 §4.3"身份统一"）：

        private_key / public_key   Ed25519，用于签名（名单、共享项、注册记录）
        dh_private / dh_public     X25519，用于 Noise 握手的静态密钥

    两者不可混用：Ed25519 的私钥不是合法的 X25519 标量，直接拿来 DH 会让双方
    算出不同的共享密钥且**都不报错**（详见 crypto_prims.dh_keypair_from_sign_seed）。
    DH 密钥由种子确定性派生，因此设备仍然只有一份需要持久化的身份材料。
    """

    name: str
    private_key: bytes
    public_key: bytes
    # 该设备所属主体的公钥。设备凭据的核心绑定关系。
    principal_key: bytes
    # 由所属主体的已有设备签发的凭据签名（自举设备时由主体私钥自签）。
    credential: bytes
    # Noise 静态密钥（X25519），由 private_key 派生，见 dh_keypair_from_sign_seed
    dh_private: bytes = b''
    dh_public: bytes = b''

    @property
    def id(self) -> str:
        return short_id(self.public_key)

    @staticmethod
    def create(name: str, principal: Principal) -> 'Device':
        """在本地创建一台归属于 `principal` 的设备并自签凭据。

        设计文档 §4.2 的理想路径是"通过已有设备授权（局域网配对或二维码）由该主体
        确认"。当调用方就是主体本人（主体私钥在手）时，直接由主体签发等价，
        省掉一次配对；跨设备配对属于后续工作（见实现路径文档的 P2 阶段）。
        """
        private_key, public_key = cp.generate_sign_keypair()
        dh_private, dh_public = cp.dh_keypair_from_sign_seed(private_key)
        device = Device(name=name, private_key=private_key, public_key=public_key,
                        principal_key=principal.public_key, credential=b'',
                        dh_private=dh_private, dh_public=dh_public)
        device.credential = device._credential_bytes(principal)
        return device

    def _credential_fields(self, principal_public_key: bytes) -> Dict[str, Any]:
        return {'principal': cp.b64(principal_public_key), 'device': cp.b64(self.public_key),
                'name': self.name, 'kind': 'device-credential-v1'}

    def _credential_bytes(self, principal: Principal) -> bytes:
        from .records import encode_fields
        return cp.sign(principal.private_key, encode_fields(self._credential_fields(principal.public_key)))

    def verify_credential(self, principal_public_key: bytes) -> bool:
        """验证本设备凭据确实由 `principal_public_key` 签发。"""
        from .records import encode_fields
        if self.principal_key != principal_public_key:
            return False
        return cp.verify(principal_public_key,
                         encode_fields(self._credential_fields(principal_public_key)),
                         self.credential)

    def to_dict(self) -> Dict[str, Any]:
        return {'name': self.name, 'private_key': cp.b64(self.private_key),
                'public_key': cp.b64(self.public_key), 'principal': cp.b64(self.principal_key),
                'credential': cp.b64(self.credential), 'dh_public': cp.b64(self.dh_public)}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'Device':
        name = str_field(data, 'name')
        private_key = b64_field(data, 'private_key', 32)
        public_key = b64_field(data, 'public_key', 32)
        principal_key = b64_field(data, 'principal', 32)
        credential = b64_field(data, 'credential', 64)
        if cp.sign_public_from_private(private_key) != public_key:
            raise RecordError('设备私钥与公钥不匹配')
        # DH 密钥总是由种子重新派生，不依赖落盘的那一份；落盘的 dh_public 只作
        # 交叉校验用（若与派生结果不符，说明文件被改过或是跨版本不兼容）。
        dh_private, dh_public = cp.dh_keypair_from_sign_seed(private_key)
        stored_dh = data.get('dh_public')
        if stored_dh is not None and cp.b64d(stored_dh) != dh_public:
            raise RecordError('设备文件的 dh_public 与由私钥派生的结果不符（文件被改动）')
        return Device(name=name, private_key=private_key, public_key=public_key,
                      principal_key=principal_key, credential=credential,
                      dh_private=dh_private, dh_public=dh_public)


# ── 设备密钥绑定证明（Ed25519 身份密钥 ↔ X25519 握手密钥）─────────────────

# 参与签名的域分隔标签与版本。改动它等于让所有旧客户端无法互相认证。
BINDING_LABEL = 'device-dh-binding-v1'


def binding_fields(device_public_key: bytes, dh_public_key: bytes) -> Dict[str, Any]:
    return {'kind': BINDING_LABEL,
            'device': cp.b64(device_public_key),
            'dh': cp.b64(dh_public_key)}


def make_binding(device_private_key: bytes, device_public_key: bytes,
                 dh_public_key: bytes) -> bytes:
    """用设备的 **Ed25519 身份私钥**签一份"这把 DH 公钥属于我"的证明。

    为什么需要这层：Noise 握手认证的是 **X25519 静态公钥**，而团体名单里列的是
    **Ed25519 设备公钥**（签名用）。两者由同一份种子派生、但不是同一个值，
    因此必须有一步把"握手认证出的 DH 公钥"映射回"名单里的设备公钥"。
    不做这一步，`authorize_peer` 就会拿 DH 公钥去名单里查、永远查不到。

    这一步不能省成"对方在负载里自报设备公钥"：自报的值没有任何约束，
    任何人都能报别人的公钥。签名把自报值与**已经过 Noise 认证的 DH 公钥**绑在
    一起，于是只有真正持有该 DH 私钥的设备才能产出一个可验证的证明。
    """
    from .records import encode_fields
    return cp.sign(device_private_key, encode_fields(binding_fields(device_public_key, dh_public_key)))


def _binding_payload_dict(payload: bytes) -> Dict[str, Any]:
    """解析绑定负载为字段字典（不做验签）。"""
    import json

    from .records import RecordError
    if not payload:
        raise RecordError('对端没有发送设备绑定负载（无法确认它属于名单里的哪台设备）')
    try:
        data = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as e:
        raise RecordError(f'设备绑定负载不是合法 JSON: {e}') from e
    if not isinstance(data, dict) or data.get('kind') != BINDING_LABEL:
        raise RecordError(f'设备绑定负载的 kind 不是 {BINDING_LABEL}')
    return data


def parse_binding(payload: bytes) -> bytes:
    """解析对端负载里的设备公钥并验证绑定证明，返回 **Ed25519 设备公钥**。

    任何一步不成立都抛 RecordError —— 调用方按"拒绝该连接"处理。
    """
    from .records import RecordError, encode_fields
    data = _binding_payload_dict(payload)

    device_public_key = cp.b64d(str(data.get('device', '')))
    dh_public_key = cp.b64d(str(data.get('dh', '')))
    signature = cp.b64d(str(data.get('sig', '')))
    if len(device_public_key) != 32 or len(dh_public_key) != 32 or len(signature) != 64:
        raise RecordError('设备绑定负载字段长度非法')
    if not cp.verify(device_public_key, encode_fields(binding_fields(device_public_key, dh_public_key)),
                     signature):
        raise RecordError('设备绑定证明验签失败（对方无法证明该设备公钥属于它）')
    return device_public_key


def remote_dh_public(payload: bytes) -> bytes:
    """取绑定负载里声明的 DH 公钥（不验签，仅用于与 Noise 实际认证值比对）。"""
    from .records import RecordError
    data = _binding_payload_dict(payload)
    value = cp.b64d(str(data.get('dh', '')))
    if len(value) != 32:
        raise RecordError('设备绑定负载里的 dh 字段长度非法')
    return value


def encode_binding_payload(device_private_key: bytes, device_public_key: bytes,
                           dh_public_key: bytes) -> bytes:
    """生成握手负载：设备公钥 + DH 公钥 + 绑定签名。"""
    import json

    payload = {'kind': BINDING_LABEL, 'device': cp.b64(device_public_key),
               'dh': cp.b64(dh_public_key),
               'sig': cp.b64(make_binding(device_private_key, device_public_key, dh_public_key))}
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


# ── 拒绝通告 ──────────────────────────────────────────────────────────────

# 握手负载里的拒绝前缀。响应方（服务端）在"对端不在名单里"时用它明确回绝，
# 否则发起方只能等到自己超时或后续请求失败 —— 而它当时其实**已经被拒绝**了，
# 却以为连接是好的（这是真实观察到的现象：不在名单里的设备 connect() 会"成功"）。
DENY_PREFIX = b'!deny:'
DENY_REASON_CHARSET = 'abcdefghijklmnopqrstuvwxyz-_'


def encode_deny_payload(reason: str) -> bytes:
    """生成拒绝负载。`reason` 只允许小写字母与 `-`/`_`，便于对端分类处理。"""
    reason = (reason or 'rejected').strip().lower()
    reason = ''.join(ch if ch in DENY_REASON_CHARSET else '-' for ch in reason)
    return DENY_PREFIX + reason.encode('ascii')


def deny_reason(payload: bytes) -> Optional[str]:
    """若负载是拒绝通告则返回原因，否则返回 None。"""
    if payload.startswith(DENY_PREFIX):
        raw = payload[len(DENY_PREFIX):]
        try:
            return raw.decode('ascii') or 'rejected'
        except UnicodeDecodeError:
            return 'rejected'
    return None


# ── 落盘 ──────────────────────────────────────────────────────────────────

def _write_private(path: Path, payload: Dict[str, Any]) -> None:
    """写私钥文件：目录 0700、文件 0600（Windows 上退化为目录属性）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows 上 chmod 语义受限；真正生效的是目录 ACL，这里不阻断流程
        pass
    tmp.replace(path)


@dataclass
class Identity:
    """一个身份目录：一个主体 + 其在本机登记的设备。"""

    root: Path
    principal: Principal
    device: Device

    @property
    def devices(self) -> List[Device]:
        return [self.device]

    @staticmethod
    def init(root: Path, principal_name: str, device_name: str) -> 'Identity':
        """在 `root` 下新建主体与首台设备。已存在则拒绝（避免静默覆盖私钥）。"""
        root = Path(root)
        principal_path = root / 'principal.json'
        if principal_path.exists():
            raise RecordError(f'身份目录已存在主体密钥: {principal_path}（拒绝覆盖私钥）')
        principal = Principal.create(principal_name)
        device = Device.create(device_name, principal)
        _write_private(principal_path, principal.to_dict())
        _write_private(root / 'devices' / f'{device.id}.json', device.to_dict())
        return Identity(root=root, principal=principal, device=device)

    @staticmethod
    def load(root: Path, device_id: Optional[str] = None) -> 'Identity':
        """读取身份。`device_id` 省略时取目录下唯一的设备；多台时必须显式指定。"""
        root = Path(root)
        principal_path = root / 'principal.json'
        if not principal_path.exists():
            raise RecordError(f'身份目录缺少 principal.json: {root}（先跑 init）')
        principal = Principal.from_dict(json.loads(principal_path.read_text(encoding='utf-8')))

        device_files = sorted((root / 'devices').glob('*.json'))
        if not device_files:
            raise RecordError(f'身份目录下没有任何设备: {root / "devices"}')
        if device_id is None and len(device_files) > 1:
            names = ', '.join(p.stem for p in device_files)
            raise RecordError(f'本机有多台设备（{names}），请用 --device 指定')
        path = device_files[0] if device_id is None else (root / 'devices' / f'{device_id}.json')
        if not path.exists():
            raise RecordError(f'找不到设备 {device_id}（可用: {", ".join(p.stem for p in device_files)}）')
        device = Device.from_dict(json.loads(path.read_text(encoding='utf-8')))

        if not device.verify_credential(principal.public_key):
            raise RecordError(f'设备 {device.id} 的凭据无法用主体公钥验证（文件被改动）')
        return Identity(root=root, principal=principal, device=device)

    def add_device(self, name: str) -> Device:
        """在已有主体下新增一台设备（由主体私钥签发凭据）。"""
        device = Device.create(name, self.principal)
        _write_private(self.root / 'devices' / f'{device.id}.json', device.to_dict())
        return device

    def encode_binding_payload(self) -> bytes:
        """本机的握手负载：设备公钥 + DH 公钥 + 绑定签名。"""
        return encode_binding_payload(self.device.private_key, self.device.public_key,
                                      self.device.dh_public)

    def describe(self) -> Dict[str, Any]:
        return {
            'principal': {'name': self.principal.name, 'id': self.principal.id},
            'device': {'name': self.device.name, 'id': self.device.id},
            'root': str(self.root),
        }

    def dump(self) -> str:
        return pretty(self.describe())
