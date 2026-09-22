"""Noise_XX_25519_ChaChaPoly_BLAKE2s 握手与传输态。

## 与设计文档的关系

设计文档 §4.4 选了 `Noise_XX`：两端均持有名单，不需要预先知道对方的静态密钥，
握手过程中双方互相认证并交换静态公钥。XX 的消息模式（Noise 规范 §8.3）是：

    -> e
    <- e, ee, s, es
    -> s, se

握手结束后双方都已知晓对方的静态公钥，再由应用层把它与**团体名单**比对 ——
这是 Noise 不做、必须在应用层做的部分（见 `transport.py` 的 `authorize_peer`）。

## 实现来源

握手与传输态由 **`noiseprotocol`**（纯 Python，MIT，标准
`Noise_XX_25519_ChaChaPoly_BLAKE2s`）实现；本模块只做四件事：

1. 把本项目的角色 / 静态 X25519 密钥 / prologue 翻译成它的 API；
2. 把它的异常统一成 `NoiseError`，把 AEAD 认证失败统一成
   `crypto_prims.CryptoError`（传输层只按这一种"密文坏了"处理）；
3. 保留原来的 `HandshakeState` / `CipherState` / `SecureSession` 接口，
   让 `transport.py` 与既有用例不需要跟着换一遍；
4. 用 Noise 官方测试向量（`tests/fixtures/noise_XX_25519_ChaChaPoly_BLAKE2s.json`，
   取自 cacophony 向量）锁住字节级互通，而不是只验"两端能互通"。

替换前的手写状态机已验证与官方向量逐字节一致（2026-09-17），因此这次替换
**不改变线格式**，协议版本保持 2。`noiseprotocol` 自身是 Alpha 状态、
未做过独立安全审计；它相对手写实现的价值是"独立实现 + 官方向量可复核"，
不是"已被审计"。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple, cast

from cryptography.exceptions import InvalidTag

from . import crypto_prims as cp

try:  # pragma: no cover - 依赖缺失时给出可读错误
    from noise.connection import Keypair, NoiseConnection
    from noise.exceptions import (
        NoiseHandshakeError,
        NoiseInvalidMessage,
        NoiseMaxNonceError,
        NoiseProtocolNameError,
        NoisePSKError,
        NoiseValidationError,
        NoiseValueError,
    )
except ImportError as e:  # pragma: no cover
    raise ImportError(
        'group-mesh 需要 noiseprotocol：pip install -r requirements.txt'
    ) from e

# 规范里的协议名（大小写敏感），Noise 要求它参与初始哈希。
# noiseprotocol 的 from_name() 内部只对 bytes 走正确路径，因此这里就用 bytes。
NOISE_LIB_NAME = b'Noise_XX_25519_ChaChaPoly_BLAKE2s'

# 保留旧名字，供文档与调试代码引用
PROTOCOL_NAME = NOISE_LIB_NAME

# 握手阶段单条消息的明文上限。规范 §3 规定所有 Noise 消息不超过 65535 字节。
MAX_HANDSHAKE_PAYLOAD = 65535 - 3 * (32 + cp.TAGLEN)
# 传输阶段单帧明文上限。设计文档 §4.5 要求"解析必须做长度与取值边界检查"。
MAX_TRANSPORT_FRAME = 1 << 20  # 1 MiB

ROLE_INITIATOR = 'initiator'
ROLE_RESPONDER = 'responder'

# noiseprotocol 的异常没有共同基类，逐个列出；这些都是"协议层失败"。
_LIB_ERRORS = (NoiseHandshakeError, NoiseInvalidMessage, NoiseMaxNonceError,
               NoisePSKError, NoiseProtocolNameError, NoiseValidationError,
               NoiseValueError)
# 握手阶段的 AEAD 失败由底层 cryptography 直接抛 InvalidTag（库没有包装它）。
_HANDSHAKE_ERRORS = (*_LIB_ERRORS, InvalidTag)


class NoiseError(Exception):
    """握手或传输态的失败。调用方一律断开连接。"""


def _as_noise_error(e: BaseException) -> NoiseError:
    return NoiseError(f'{type(e).__name__}: {e}')


class CipherState:
    """传输态的一个方向：直接包 `noiseprotocol` 的 CipherState。

    为什么不用 `NoiseConnection.encrypt()/decrypt()`：那个高层 API 会强制
    Noise 规范的"单条消息 ≤ 65535 字节"限制，而本项目的应用帧是"整条 JSON
    请求/应答"（最大 1 MiB，目录列表可能超过 65535）。握手仍然走
    `NoiseConnection`（安全关键的状态机），传输只借它的 CipherState ——
    同一套密钥、同一套 nonce 推进、同一套 AEAD，只是不套那层长度检查。
    代价是应用帧可能超过规范的 65535 字节上限，与只实现规范长度检查的
    实现对传大帧时会被它们拒绝；这是本协议有意的帧长选择，记录在
    docs/group-mesh-design.md。
    """

    def __init__(self, cipher, key: bytes, direction: str) -> None:
        if direction not in ('send', 'recv'):
            raise NoiseError(f'未知方向: {direction!r}')
        self._cipher = cipher
        self.key = key
        self._direction = direction

    def encrypt(self, plaintext: bytes, associated_data: bytes = b'') -> bytes:
        if self._direction != 'send':
            raise NoiseError('该 CipherState 是接收方向，不能用来加密')
        if associated_data:
            raise NoiseError('Noise 传输态不使用关联数据（AD 固定为空）')
        if len(plaintext) > MAX_TRANSPORT_FRAME:
            raise NoiseError(f'单帧明文超过上限（{len(plaintext)} > {MAX_TRANSPORT_FRAME}）')
        try:
            return bytes(self._cipher.encrypt_with_ad(None, plaintext))
        except _HANDSHAKE_ERRORS as e:
            raise _as_noise_error(e) from e

    def decrypt(self, ciphertext: bytes, associated_data: bytes = b'') -> bytes:
        if self._direction != 'recv':
            raise NoiseError('该 CipherState 是发送方向，不能用来解密')
        if associated_data:
            raise NoiseError('Noise 传输态不使用关联数据（AD 固定为空）')
        if len(ciphertext) > MAX_TRANSPORT_FRAME + cp.TAGLEN:
            raise NoiseError(f'单帧密文超过上限（{len(ciphertext)}）')
        try:
            return bytes(self._cipher.decrypt_with_ad(None, ciphertext))
        except _HANDSHAKE_ERRORS as e:
            # 认证失败是本节最常被上层依赖的语义：统一成 CryptoError，
            # 让 transport.Connection.recv 只按一种异常处理。
            raise cp.CryptoError(f'消息认证失败（连接可能被篡改）: {e}') from e


class HandshakeState:
    """Noise_XX 握手状态机（对 `noiseprotocol.NoiseConnection` 的薄封装）。

    `local_static_*` 是 Noise 意义上的静态 X25519 密钥对，由设备身份种子派生
    （`crypto_prims.dh_keypair_from_sign_seed`）。Ed25519 身份密钥不参与握手。

    `ephemeral_private` 只给测试向量用：官方向量要求确定性临时密钥，
    生产路径不传，由 `noiseprotocol` 随机生成。
    """

    def __init__(self, role: str, local_static_private: bytes, local_static_public: bytes,
                 prologue: bytes = b'', ephemeral_private: Optional[bytes] = None) -> None:
        if role not in (ROLE_INITIATOR, ROLE_RESPONDER):
            raise NoiseError(f'未知角色: {role}')
        if len(local_static_private) != 32 or len(local_static_public) != 32:
            raise NoiseError('静态密钥必须是 32 字节的 X25519 密钥对')
        if cp.dh_public_from_private(local_static_private) != local_static_public:
            raise NoiseError('静态公钥与私钥不匹配（拒绝用错误的密钥握手）')
        if ephemeral_private is not None and len(ephemeral_private) != 32:
            raise NoiseError('临时私钥必须是 32 字节的 X25519 私钥')

        self.role = role
        self.local_static_private = local_static_private
        self.local_static_public = local_static_public
        self._complete = False
        self._split: Optional[Tuple[CipherState, CipherState]] = None
        self._remote_static_public: Optional[bytes] = None

        try:
            connection = NoiseConnection.from_name(NOISE_LIB_NAME)
            if role == ROLE_INITIATOR:
                connection.set_as_initiator()
            else:
                connection.set_as_responder()
            if prologue:
                connection.set_prologue(prologue)
            connection.set_keypair_from_private_bytes(Keypair.STATIC, local_static_private)
            if ephemeral_private is not None:
                connection.set_keypair_from_private_bytes(Keypair.EPHEMERAL, ephemeral_private)
            connection.start_handshake()
        except _HANDSHAKE_ERRORS as e:
            raise _as_noise_error(e) from e
        self._connection = connection
        self._capture_remote_static()

    def _capture_remote_static(self) -> None:
        """在 `handshake_done` 清空字段之前，把对端静态公钥抄下来。

        `noiseprotocol` 的 `HandshakeState` 把 `rs` 存在自己身上，`handshake_done()`
        会先 `self.rs = None` 再删掉 handshake_state；`NoiseProtocol.keypairs['rs']`
        自始至终没被赋值。因此在 `handshake_done()` 外面包一层，取到 `rs` 再放行 ——
        否则 `transport.authorize_peer()` 拿不到对端设备公钥。
        """
        protocol = cast(Any, self._connection.noise_protocol)
        original_done = protocol.handshake_done
        owner = self

        def _capture() -> None:
            handshake = getattr(protocol, 'handshake_state', None)
            remote = getattr(handshake, 'rs', None) if handshake is not None else None
            public = getattr(remote, 'public_bytes', None)
            if public is not None:
                owner._remote_static_public = bytes(public)
            original_done()

        protocol.handshake_done = _capture  # type: ignore[method-assign]

    # ── 握手 ──────────────────────────────────────────────────────────────

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def remote_static_public(self) -> Optional[bytes]:
        """对端静态 X25519 公钥；握手进行到一半时为 None。"""
        return self._remote_static_public

    @property
    def handshake_hash(self) -> Optional[bytes]:
        """握手转录哈希；握手未完成时为 None。"""
        if not self._complete:
            return None
        return bytes(self._connection.get_handshake_hash())

    def write_message(self, payload: bytes = b'') -> bytes:
        """构造下一条本方发出的握手消息。"""
        if self._complete:
            raise NoiseError('握手已完成，不能再写握手消息')
        if len(payload) > MAX_HANDSHAKE_PAYLOAD:
            raise NoiseError(f'握手负载超过上限（{len(payload)} > {MAX_HANDSHAKE_PAYLOAD}）')
        try:
            message = bytes(self._connection.write_message(payload))
        except _HANDSHAKE_ERRORS as e:
            raise _as_noise_error(e) from e
        self._maybe_finish()
        return message

    def read_message(self, message: bytes) -> bytes:
        """解析并校验对端发来的握手消息，返回其中的负载。"""
        if self._complete:
            raise NoiseError('握手已完成，不能再读握手消息')
        if not message:
            raise NoiseError('握手消息为空')
        if len(message) > MAX_HANDSHAKE_PAYLOAD + 3 * (32 + cp.TAGLEN):
            raise NoiseError(f'握手消息过长（{len(message)} 字节），拒绝解析')
        try:
            payload = bytes(self._connection.read_message(message))
        except _HANDSHAKE_ERRORS as e:
            raise _as_noise_error(e) from e
        self._maybe_finish()
        return payload

    def _maybe_finish(self) -> None:
        if self._connection.handshake_finished:
            self._complete = True

    def split(self) -> Tuple[CipherState, CipherState]:
        """握手完成后拆出两个方向的传输密钥。"""
        if not self._complete:
            raise NoiseError('握手尚未完成，不能 split')
        if self._split is None:
            protocol = cast(Any, self._connection.noise_protocol)
            send_cipher = protocol.cipher_state_encrypt
            recv_cipher = protocol.cipher_state_decrypt
            self._split = (CipherState(send_cipher, bytes(send_cipher.k), 'send'),
                           CipherState(recv_cipher, bytes(recv_cipher.k), 'recv'))
        return self._split


class SecureSession:
    """握手完成后的加密会话：两个方向的 CipherState。"""

    def __init__(self, send: CipherState, recv: CipherState,
                 remote_static_public: bytes, handshake_hash: bytes) -> None:
        self.send = send
        self.recv = recv
        self.remote_static_public = remote_static_public
        self.handshake_hash = handshake_hash

    def encrypt(self, plaintext: bytes) -> bytes:
        return self.send.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self.recv.decrypt(ciphertext)


def prologue_from_negotiation(local_hello: bytes, remote_hello: bytes) -> bytes:
    """把 §4.5 的协商结果并入被认证的转录（Noise 的 prologue）。

    设计文档 §4.5 的实现约束 1 明确要求："协商结果必须并入被认证的握手转录
    （Noise 的 prologue），否则存在降级攻击面"。

    两端各自发出的 hello 字节相同、收到的也相同，因此拼接顺序必须**确定**：
    这里按"字典序小者在先"排序，双方算出的 prologue 一致，且都不依赖各自的角色。
    """
    first, second = sorted([local_hello, remote_hello])
    return cp.blake2s(b'group-mesh-negotiation-v1', first, second)
