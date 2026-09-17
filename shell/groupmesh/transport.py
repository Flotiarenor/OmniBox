"""线上协议：协商、握手、加密会话、请求/应答。

## 连接建立顺序

    1. 明文 hello 双向交换（§4.5）—— proto / suites / features
    2. 协商结果并入 prologue，开始 Noise_XX 握手
    3. 握手验证对端静态公钥，再用**团体名单**判定该设备属于哪个主体（授权）
    4. 进入传输态，收发 JSON 消息

第 3 步是 Noise 不做、必须由应用层做的事：Noise 只保证"你确实持有这把静态私钥"，
不保证"这把公钥的我方名单里有"。

## 帧格式

TCP 是字节流，必须自己定界：

    4 字节大端长度 + 该长度的负载

协商阶段的负载是明文 JSON；握手与传输阶段的负载是 Noise 密文（ChaCha20-Poly1305
自带完整性，因此不需要额外校验字段）。

## 为什么明文 hello 是安全的

设计文档 §4.5 说明该消息"在认证之前处理"。它只携带协议版本与能力列表，不含
身份信息；而其内容会并入被认证的 prologue，因此中间人篡改版本/套件会导致双方
prologue 不一致、随后握手必然失败 —— 这就是防降级攻击的机制。
"""

from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import KNOWN_SUITES, PROTO_VERSION
from . import crypto_prims as cp
from . import noise as noise_mod
from .identity import Identity, parse_binding, remote_dh_public
from .records import RecordError
from .roster import Roster

# 单个文件分块的明文上限。请求-应答走 JSON + base64，块太大会让单条消息膨胀
# （base64 有 4/3 膨胀），也会一次性占用大量内存。
DEFAULT_CHUNK_BYTES = 256 * 1024
# 帧上限：分块 + base64 + JSON 包装的余量
MAX_FRAME_BYTES = 4 * 1024 * 1024
# socket 超时：任何一步卡住都要让连接失败，而不是永远挂住
SOCKET_TIMEOUT = 20.0

FEATURES = ('roster-v1', 'registration-v1', 'share-read', 'share-range')


class TransportError(Exception):
    """连接、协商、握手或消息层的失败。"""


class RemoteError(Exception):
    """对端返回了 error 应答。"""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


# ── 帧收发 ────────────────────────────────────────────────────────────────

def send_frame(sock: socket.socket, payload: bytes) -> None:
    if len(payload) > MAX_FRAME_BYTES:
        raise TransportError(f'帧过大（{len(payload)} > {MAX_FRAME_BYTES}）')
    sock.sendall(struct.pack('>I', len(payload)) + payload)


def recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(min(remaining, 64 * 1024))
        if not chunk:
            raise TransportError('连接在对端发送完数据前关闭')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def recv_frame(sock: socket.socket) -> bytes:
    header = recv_exactly(sock, 4)
    (length,) = struct.unpack('>I', header)
    if length > MAX_FRAME_BYTES:
        raise TransportError(f'收到的帧过大（{length} > {MAX_FRAME_BYTES}）')
    return recv_exactly(sock, length) if length else b''


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _parse_json(raw: bytes, what: str) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise TransportError(f'{what} 不是合法 JSON: {e}') from e
    if not isinstance(value, dict):
        raise TransportError(f'{what} 必须是 JSON 对象')
    return value


# ── 协商（§4.5）──────────────────────────────────────────────────────────

@dataclass
class Negotiation:
    """协商结果。

    保留 `local_hello` / `remote_hello` 两段**原始字节**（而不只是解析后的字段），
    因为 prologue 必须对两端实际发出的字节求值 —— 只有原始字节才能保证双方算出的
    prologue 一致。
    """

    suite: str
    features: List[str]
    local_hello: bytes
    remote_hello: bytes

    @staticmethod
    def local_hello_payload() -> Dict[str, Any]:
        return {'proto': PROTO_VERSION, 'suites': list(KNOWN_SUITES), 'features': list(FEATURES)}

    @staticmethod
    def resolve(local: Dict[str, Any], remote: Dict[str, Any], local_bytes: bytes,
                remote_bytes: bytes) -> 'Negotiation':
        # 边界检查：hello 在认证之前被处理，字段必须逐个校验（§4.5 约束 2）
        proto = remote.get('proto')
        if not isinstance(proto, int) or isinstance(proto, bool):
            raise TransportError('hello.proto 必须是整数')
        if proto != PROTO_VERSION:
            raise TransportError(
                f'协议主版本不一致（本地 {PROTO_VERSION}，对端 {proto}），请升级后再连')

        remote_suites = remote.get('suites')
        if (not isinstance(remote_suites, list) or not remote_suites
                or not all(isinstance(s, str) for s in remote_suites)):
            raise TransportError('hello.suites 必须是非空字符串数组')
        # 按**本地偏好顺序**取交集里的第一个
        common = [s for s in local['suites'] if s in remote_suites]
        if not common:
            raise TransportError(
                f'没有共同支持的加密套件（本地 {local["suites"]}，对端 {remote_suites}）')

        remote_features = remote.get('features', [])
        if not isinstance(remote_features, list) or not all(isinstance(f, str) for f in remote_features):
            raise TransportError('hello.features 必须是字符串数组')

        return Negotiation(suite=common[0],
                           features=[f for f in local['features'] if f in remote_features],
                           local_hello=local_bytes, remote_hello=remote_bytes)

    def prologue(self) -> bytes:
        """§4.5 约束 1：把协商结果并入被认证的握手转录。"""
        return noise_mod.prologue_from_negotiation(self.local_hello, self.remote_hello)


def exchange_hello(sock: socket.socket) -> Negotiation:
    """双向交换明文 hello。两端都先发后收，因此不会互相等待。"""
    local = Negotiation.local_hello_payload()
    local_bytes = _json_bytes(local)
    send_frame(sock, local_bytes)
    remote_bytes = recv_frame(sock)
    remote = _parse_json(remote_bytes, 'hello')
    return Negotiation.resolve(local, remote, local_bytes, remote_bytes)


# ── 授权：由名单把设备公钥映射到主体（§3.3 / §12 第 1 项）───────────────

@dataclass
class PeerIdentity:
    """握手认证出的对端身份。字段全部来自密码学验证，不含请求参数。"""

    device_key: bytes
    principal_key: bytes
    name: str
    # 对端的角色/主体：**未入名单时为 None**（见 `member`）
    role: Optional[str]
    group: str
    # 该设备是否在本机名单里。
    #
    # 为什么"不在名单里"不是直接断开（v0.2 改）：名单是**带外**建立信任锚的，
    # 群主加人后签发新名单，而新成员手里只有旧名单 —— 如果握手阶段就把不在名单的
    # 设备拒掉，它永远收不到那份新名单（实现路径文档 §5.10 的真实故障）。
    # 因此握手只要求**设备绑定证明有效**（密码学上自足），"能不能干活"由
    # `Node.handle()` 按 op 逐条判定：不在名单者只放行 `roster`（拉新名单）。
    member: bool = True

    @property
    def device_id(self) -> str:
        return self.device_key[:8].hex()

    @property
    def principal_id(self) -> str:
        return self.principal_key[:8].hex()


def authorize_peer(roster: Optional[Roster], device_key: bytes) -> PeerIdentity:
    """按名单判定对端设备属于哪个主体，并标出它是否已在名单里。

    设计文档 §12 第 1 项"调用者身份"在协议层的对应物：插件与壳都必须能从
    **已验证的凭据**得到主体，而不是从请求参数里读一个自称的 ID。

    `roster is None`（本机还没建团）仍然直接拒绝：那种状态下本机没有任何"团体"
    可言，放行只会变成一个开放式入口。

    不在名单里的设备返回 `member=False` 而不是抛错：它的设备绑定证明已经通过
    Noise 验证，因此连接本身是可信的，只有"能做什么"受限（§5.7 的名单分发需要
    让新成员连进来拿新名单）。
    """
    if roster is None:
        raise TransportError('本地没有团体名单，无法认证对端（先 join 或 create）')
    entry = roster.entry_of_device(device_key)
    if entry is None:
        return PeerIdentity(device_key=device_key, principal_key=b'', name='',
                            role=None, group=roster.group, member=False)
    role = roster.role_of(entry.principal_key)
    if role is None:  # pragma: no cover - entry_of_device 命中时必定有角色
        raise TransportError('对端主体在名单里没有角色，名单结构异常')
    return PeerIdentity(device_key=device_key, principal_key=entry.principal_key,
                        name=entry.name, role=role, group=roster.group, member=True)


# ── 连接 ──────────────────────────────────────────────────────────────────

class Connection:
    """一条已建立并完成握手的连接。"""

    def __init__(self, sock: socket.socket, session: noise_mod.SecureSession,
                 peer: PeerIdentity, negotiation: Negotiation) -> None:
        self.sock = sock
        self.session = session
        self.peer = peer
        self.negotiation = negotiation

    # ── 消息 ──────────────────────────────────────────────────────────────

    def send(self, payload: Dict[str, Any]) -> None:
        send_frame(self.sock, self.session.encrypt(_json_bytes(payload)))

    def recv(self) -> Dict[str, Any]:
        try:
            ciphertext = recv_frame(self.sock)
        except TransportError:
            raise
        try:
            plaintext = self.session.decrypt(ciphertext)
        except cp.CryptoError as e:
            raise TransportError(f'消息解密失败（连接可能被篡改）: {e}') from e
        return _parse_json(plaintext, '消息')

    def request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发一条请求并等一条应答；对端回 error 时抛 `RemoteError`。"""
        self.send(payload)
        response = self.recv()
        if response.get('status') == 'error':
            raise RemoteError(str(response.get('error', '未知错误')), response.get('code'))
        return response

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> 'Connection':
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def run_handshake(sock: socket.socket, identity: Identity, roster: Optional[Roster],
                  negotiation: Negotiation, initiator: bool) -> Tuple[noise_mod.SecureSession, PeerIdentity]:
    """执行 Noise_XX 握手 + 一步授权确认，返回会话与对端身份。

    消息顺序（Noise 规范 §8.3 的 XX 模式，末尾多一步本项目的确认帧）：

        发起方：写 msg1 → 读 msg2 → 写 msg3+绑定 → 读 **确认帧**
        响应方：读 msg1 → 写 msg2 → 读 msg3+绑定 → 写 **确认帧**

    ## 为什么需要那一步确认帧

    XX 的前三条消息只完成"互相认证静态公钥"。**授权要等双方都拿到对方的设备
    绑定负载之后才能判定**，而响应方手中的最后一个输入（发起方的静态公钥）是在
    msg3 里才到达的 —— 也就是说响应方在发 msg2 时根本还没有判定所需的全部信息。

    如果到此为止，被响应方拒绝的发起方会以为握手成功（它已经把 msg3 发出去了），
    只有等到后续请求失败或超时才发现，实测现象就是"不在名单里的设备 connect() 成功"。
    因此这里加一条**会话建立后的第一帧**：响应方在它上面回 `ok` 或 `deny:<原因>`，
    发起方据此决定是否返回连接。该帧走的是 Noise 传输态，已加密且带完整性。

    ## `prologue` 的一致性

    两端的 `Negotiation` 只有"自己那一侧"是本地产生的，因此这里统一按
    "`local_hello_payload()` × 对端实际字节"重新构造 prologue，
    避免两端因视角不同算出不同的 prologue。
    """
    role = noise_mod.ROLE_INITIATOR if initiator else noise_mod.ROLE_RESPONDER
    local_hello = _json_bytes(Negotiation.local_hello_payload())
    peer_hello = negotiation.remote_hello
    prologue = noise_mod.prologue_from_negotiation(local_hello, peer_hello)

    binding = identity.encode_binding_payload()
    # 握手用的是设备派生的 X25519 静态密钥，不是 Ed25519 身份密钥
    state = noise_mod.HandshakeState(role=role,
                                     local_static_private=identity.device.dh_private,
                                     local_static_public=identity.device.dh_public,
                                     prologue=prologue)

    if initiator:
        try:
            send_frame(sock, state.write_message())
            peer_payload = state.read_message(recv_frame(sock))
            send_frame(sock, state.write_message(binding))
        except noise_mod.NoiseError as e:
            raise TransportError(f'Noise 握手失败（发起方）: {e}') from e
    else:
        try:
            state.read_message(recv_frame(sock))
            send_frame(sock, state.write_message(binding))
            peer_payload = state.read_message(recv_frame(sock))
        except noise_mod.NoiseError as e:
            raise TransportError(f'Noise 握手失败（响应方）: {e}') from e

    remote_static = state.remote_static_public
    if remote_static is None:  # pragma: no cover - XX 必然交换静态公钥
        raise TransportError('握手结束仍未获得对端静态公钥')

    send, recv = state.split()
    handshake_hash = state.handshake_hash
    if handshake_hash is None:  # pragma: no cover - split 成功必然已有转录哈希
        raise TransportError('握手完成但没有转录哈希')
    session = noise_mod.SecureSession(send, recv, remote_static, handshake_hash)

    if initiator:
        # 等服务端的授权结论
        verdict = _decode_control(session, recv_frame(sock), '授权确认')
        if verdict.get('status') != 'ok':
            raise PeerDenied(str(verdict.get('reason') or 'rejected'),
                             verdict.get('message') or '对端拒绝了本次连接')

    try:
        peer_device_key = parse_binding(peer_payload)
        if remote_static != remote_dh_public(peer_payload):
            # 绑定负载里的 dh 必须与 Noise 实际认证的那把公钥一致：否则签名虽有效，
            # 却可能属于另一台设备（把别人的绑定负载重放过来的情况）。
            raise TransportError('对端绑定负载里的 DH 公钥与 Noise 协商出的静态公钥不一致')
        peer = authorize_peer(roster, peer_device_key)
    except (RecordError, TransportError) as e:
        if initiator:
            raise
        # 响应方：先把自己的拒绝结论发出去，再断开。对端据此能区分
        # "被明确拒绝"与"网络断了"。
        send_frame(sock, session.encrypt(_json_bytes(
            {'status': 'deny', 'reason': _deny_code(e), 'message': str(e)})))
        raise

    if not initiator:
        send_frame(sock, session.encrypt(_json_bytes({'status': 'ok'})))
    return session, peer


class PeerDenied(TransportError):
    """对端明确拒绝了本次连接（不是网络故障）。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _deny_code(error: Exception) -> str:
    """把授权/绑定失败的原因压成一个短代码，供对端分类显示。"""
    text = str(error)
    if '不在团体名单' in text:
        return 'not-in-roster'
    if '没有团体名单' in text:
        return 'no-roster'
    if '绑定' in text:
        return 'bad-binding'
    return 'rejected'


def _decode_control(session: noise_mod.SecureSession, frame: bytes, what: str) -> Dict[str, Any]:
    try:
        plaintext = session.decrypt(frame)
    except cp.CryptoError as e:
        raise TransportError(f'{what}解密失败: {e}') from e
    return _parse_json(plaintext, what)


def connect(host: str, port: int, identity: Identity, roster: Optional[Roster],
            timeout: float = SOCKET_TIMEOUT) -> Connection:
    """作为发起方连接一个节点。"""
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        negotiation = exchange_hello(sock)
        session, peer = run_handshake(sock, identity, roster, negotiation, initiator=True)
    except Exception:
        sock.close()
        raise
    return Connection(sock, session, peer, negotiation)
