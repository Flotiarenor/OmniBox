"""Noise_XX_25519_ChaChaPoly_BLAKE2s 握手与传输态。

## 与设计文档的关系

设计文档 §4.4 选了 `Noise_XX`，理由是"两端均持有名单，不需要预先知道对方的静态
密钥，握手过程中双方互相认证并交换静态公钥"。XX 的消息模式（Noise 规范 §8.3）是：

    -> e
    <- e, ee, s, es
    -> s, se

握手结束后，双方都已知晓对方的静态公钥，并各自把它与**团体名单**比对 ——
这是 Noise 不做、而我们必须在应用层做的部分（见 transport.py 的
`authorize_peer`）。

## 实现边界（必须如实标注）

Noise 的参考实现（`noise-c`、`snow`、`noiseprotocol`）是"现成实现"，
设计文档 §4.4 的本意是用它们。本 MVP 的做法是**只复用已审计的密码学原语**
（pycryptodome 的 X25519 / ChaCha20-Poly1305 / BLAKE2s），按规范自己写了这个
状态机。这是刻意的偏离，原因是仓库既有依赖里没有 Noise 实现，而验证内核的
目的是尽快把协议跑通、把接口试出来。

**上生产前必须替换**：`docs/group-mesh-implementation-path.md` 的 P0 阶段把
"换 vetted Noise 实现并通过 Noise 官方测试向量"列为本模块的前置条件。
在替换之前，本文件提供的安全性来自"primitives 正确 + 状态机照着规范写"，
而不是来自经过长期审查的协议实现 —— 这个区别不应被含糊掉。

## 已实现的规范细节

* `h = HASH(protocol_name)`，`ck = h`，`h = HASH(h || prologue)`；
* 每条握手消息以 `h` 作为关联数据（AEAD 的 AD）；
* XX 的三个 `e`/`s` 公钥均以明文入消息，但**都混入 `h`**，因此任何篡改都会
  让后续 AEAD 校验失败；
* `ee`/`es`/`se` 分别做 `MixKey(DH(...))`，密钥材料只由 HKDF 派生；
* `Split()` 后 initiator 用 `(k1, k2)`、responder 用 `(k2, k1)`；
* prologue 用于绑定"协商结果"（§4.5 的明文协商字段），防止降级攻击。
"""

from __future__ import annotations

from typing import Optional, Tuple, cast

from . import crypto_prims as cp

# 协议名固定，Noise 规范要求它参与初始哈希
PROTOCOL_NAME = b'Noise_XX_25519_ChaChaPoly_BLAKE2s'

# 握手阶段单条消息的明文上限。规范 §3 规定所有 Noise 消息不超过 65535 字节。
MAX_HANDSHAKE_PAYLOAD = 65535 - 3 * (32 + cp.TAGLEN)
# 传输阶段单帧明文上限。设计文档 §4.5 要求"解析必须做长度与取值边界检查"。
MAX_TRANSPORT_FRAME = 1 << 20  # 1 MiB

ROLE_INITIATOR = 'initiator'
ROLE_RESPONDER = 'responder'


class NoiseError(Exception):
    """握手或传输态的失败。调用方一律断开连接。"""


class CipherState:
    """Noise 的 CipherState：一个密钥 + 单调递增的 nonce 计数器。"""

    __slots__ = ('counter', 'key')

    def __init__(self, key: Optional[bytes] = None) -> None:
        self.key = key
        self.counter = 0

    @property
    def active(self) -> bool:
        return self.key is not None

    def encrypt(self, plaintext: bytes, associated_data: bytes = b'') -> bytes:
        if self.key is None:
            # Noise 规范：`k` 未设置时负载**原样发送**（XX 的第一条消息正是这种
            # 状态，此时还没有任何 DH）。MixHash 由 HandshakeState 负责 ——
            # CipherState 不该知道握手哈希的存在。
            return plaintext
        if len(plaintext) > MAX_TRANSPORT_FRAME:
            raise NoiseError(f'单帧明文超过上限（{len(plaintext)} > {MAX_TRANSPORT_FRAME}）')
        if self.counter >= 2 ** 64 - 1:
            raise NoiseError('nonce 计数器耗尽，必须重新握手')
        output = cp.aead_encrypt(self.key, self.counter, plaintext, associated_data)
        self.counter += 1
        return output

    def decrypt(self, ciphertext: bytes, associated_data: bytes = b'') -> bytes:
        if self.key is None:
            return ciphertext
        if len(ciphertext) > MAX_TRANSPORT_FRAME + cp.TAGLEN:
            raise NoiseError(f'单帧密文超过上限（{len(ciphertext)}）')
        if self.counter >= 2 ** 64 - 1:
            raise NoiseError('nonce 计数器耗尽，必须重新握手')
        plaintext = cp.aead_decrypt(self.key, self.counter, ciphertext, associated_data)
        # 只有解密成功才推进计数器：失败的消息必须被丢弃且不改变状态，
        # 否则一条伪造消息就能把双方 nonce 永久错开。
        self.counter += 1
        return plaintext


class HandshakeState:
    """Noise_XX 握手状态机。

    `local_static_*` 是 Noise 意义上的静态密钥对，也就是 **X25519** 密钥对，
    由设备身份种子派生（见 `crypto_prims.dh_keypair_from_sign_seed`）。
    Ed25519 身份密钥不参与握手 —— 握手认证的是"这把 DH 静态公钥确实属于对端"，
    再由应用层把该公钥映射回主体（见 transport.authorize_peer）。
    """

    def __init__(self, role: str, local_static_private: bytes, local_static_public: bytes,
                 prologue: bytes = b'') -> None:
        if role not in (ROLE_INITIATOR, ROLE_RESPONDER):
            raise NoiseError(f'未知角色: {role}')
        if len(local_static_private) != 32 or len(local_static_public) != 32:
            raise NoiseError('静态密钥必须是 32 字节的 X25519 密钥对')
        self.role = role
        self.local_static_private = local_static_private
        self.local_static_public = local_static_public
        self.remote_static_public: Optional[bytes] = None

        self.h = cp.blake2s(PROTOCOL_NAME)
        self.ck = self.h
        self.h = cp.blake2s(self.h + prologue)

        self.symmetric = CipherState()
        self.ephemeral_private: Optional[bytes] = None
        self.ephemeral_public: Optional[bytes] = None

        self.message_index = 0
        self.complete = False
        self._send_cipher: Optional[CipherState] = None
        self._recv_cipher: Optional[CipherState] = None

    # ── 状态混合 ──────────────────────────────────────────────────────────

    def _mix_hash(self, data: bytes) -> None:
        self.h = cp.blake2s(self.h + data)

    def _mix_key(self, input_key_material: bytes) -> None:
        self.ck, temp_k = cp.hkdf(self.ck, input_key_material, 2)
        self.symmetric = CipherState(temp_k)

    def _mix_key_and_hash(self, input_key_material: bytes) -> None:
        # XX 不使用 PSK，保留以完整对应规范
        self.ck, temp_h, temp_k = cp.hkdf(self.ck, input_key_material, 3)
        self._mix_hash(temp_h)
        self.symmetric = CipherState(temp_k)

    def _encrypt_and_hash(self, plaintext: bytes) -> bytes:
        ciphertext = self.symmetric.encrypt(plaintext, self.h)
        self._mix_hash(ciphertext)
        return ciphertext

    def _decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        plaintext = self.symmetric.decrypt(ciphertext, self.h)
        self._mix_hash(ciphertext)
        return plaintext

    # ── XX 消息构造与解析 ─────────────────────────────────────────────────

    def write_message(self, payload: bytes = b'') -> bytes:
        """构造下一条本方发出的握手消息。"""
        if self.complete:
            raise NoiseError('握手已完成，不能再写握手消息')
        if len(payload) > MAX_HANDSHAKE_PAYLOAD:
            raise NoiseError(f'握手负载超过上限（{len(payload)} > {MAX_HANDSHAKE_PAYLOAD}）')

        index = self.message_index
        buffer = b''

        # XX 的三个 token 组，按规范 §7.5（"es"/"se" 的第一个字母指**发起方**的
        # 密钥类型，第二个指响应方的）：
        #
        #   消息 1  ->  e
        #   消息 2  <-  e, ee, s, es
        #   消息 3  ->  s, se
        #
        # 本项目实现时在"消息 2 的第二个 DH 是哪个"上取错过密钥，症状是双方 ck
        # 在此处分叉、随后 AEAD 校验失败（对端只看到 "MAC check failed"）。
        # 关键约束：`es` 需要发起方的**静态**公钥，而它在消息 3 才发出，
        # 因此 `es` 在响应方这一侧只能在处理消息 3 时结算。
        if index == 0:
            # -> e
            self.ephemeral_private, self.ephemeral_public = cp.generate_dh_keypair()
            buffer += self.ephemeral_public
            self._mix_hash(self.ephemeral_public)
            buffer += self._encrypt_and_hash(payload)
        elif index == 1:
            # <- e, ee, s, es
            self.ephemeral_private, self.ephemeral_public = cp.generate_dh_keypair()
            buffer += self.ephemeral_public
            self._mix_hash(self.ephemeral_public)
            # ee：Call MixKey(DH(e, re)) —— 双方临时密钥
            self._mix_key(cp.dh(self.ephemeral_private, self.remote_ephemeral()))
            # s：Appends EncryptAndHash(s.public_key)。这一步用的是上面 ee 派生的 k，
            # 其 nonce 从 0 开始。
            buffer += self._encrypt_and_hash(self.local_static_public)
            # es：Call MixKey(DH(s, re)) —— 发起方静态 × 响应方临时。
            #
            # 关键顺序约束（规范 §5.3）：token 全部处理完之后才追加负载，
            # 因此**负载用的是 es 派生出的 k**，而不是 ee 的。
            # 本项目早期实现把负载放在 es 之前，双方 ck 在此处分叉，
            # 症状是对端只报 "MAC check failed"。
            self._mix_key(cp.dh(self.local_static_private, self.remote_ephemeral()))
            # payload：新的 k、nonce 从 0 重新开始
            buffer += self._encrypt_and_hash(payload)
        elif index == 2:
            # -> s, se
            # s：用上一条消息 es 派生出的 k，nonce 接着用（发起方在消息 2 里已用掉 0）
            buffer += self._encrypt_and_hash(self.local_static_public)
            # se：规范 §5.3 对 token 的两种取值是
            #        发起方 Call MixKey(DH(s, re))、响应方 Call MixKey(DH(e, rs))
            #     即：发起方用**自己的静态** × 对端临时。本项目曾在这里误写成
            #     DH(e, re)（重复了一次 es），使双方 ck 在消息 3 处分叉。
            self._mix_key(cp.dh(self.local_static_private, self.remote_ephemeral()))
            buffer += self._encrypt_and_hash(payload)
        else:
            raise NoiseError(f'XX 只有 3 条握手消息，越界: {index}')

        self.message_index += 1
        return buffer

    def read_message(self, message: bytes) -> bytes:
        """解析并校验对端发来的握手消息，返回其中的负载。"""
        if self.complete:
            raise NoiseError('握手已完成，不能再读握手消息')
        if not message:
            raise NoiseError('握手消息为空')
        if len(message) > MAX_HANDSHAKE_PAYLOAD + 3 * (32 + cp.TAGLEN):
            raise NoiseError(f'握手消息过长（{len(message)} 字节），拒绝解析')

        index = self.message_index
        offset = 0

        # 此处临时私钥必然已生成（在 write_message 的第 1 条消息里 step0 做过），
        # 但字段声明是 Optional，pyright 因此报 "Argument of type bytes | None cannot
        # be assigned to parameter private"（本文件 3 处）。用 cast 把"运行时已知"
        # 告诉类型检查，不改运行逻辑（写成断言反而会在类型上留下运行时判断）。
        ephemeral_private = cast(bytes, self.ephemeral_private)

        def take(n: int, what: str) -> bytes:
            nonlocal offset
            if offset + n > len(message):
                raise NoiseError(f'握手消息在读取{what}时提前结束')
            chunk = message[offset:offset + n]
            offset += n
            return chunk

        if index == 0:
            # -> e   （本方是 responder）
            peer_e = take(32, '对端临时公钥')
            self._mix_hash(peer_e)
            self._set_remote_ephemeral(peer_e)
            payload = self._decrypt_and_hash(message[offset:])
            offset = len(message)
        elif index == 1:
            # <- e, ee, s, es   （本方是 initiator）
            peer_e = take(32, '对端临时公钥')
            self._mix_hash(peer_e)
            self._set_remote_ephemeral(peer_e)
            # ee：Call MixKey(DH(e, re))
            self._mix_key(cp.dh(ephemeral_private, peer_e))
            # s：Sets rs to DecryptAndHash(下一个 DHLEN+16 字节)，用 ee 的 k、nonce=0
            remote_static = self._decrypt_and_hash(take(32 + cp.TAGLEN, '对端静态公钥'))
            if len(remote_static) != 32:
                raise NoiseError('对端静态公钥长度非法')
            self.remote_static_public = remote_static
            # es：Call MixKey(DH(e, rs)) —— 本方临时 × 对端静态
            self._mix_key(cp.dh(ephemeral_private, remote_static))
            # 负载：DecryptAndHash(剩余字节)，用 es 的新 k、nonce 从 0 开始
            payload = self._decrypt_and_hash(message[offset:])
            offset = len(message)
        elif index == 2:
            # -> s, se   （本方是 responder）
            # s：用的还是消息 2 里 es 派生出的那个 k。**nonce 不重置** ——
            # 响应方在消息 2 解密响应方静态公钥时已经用掉了该 k 的 nonce 0，
            # 因此这里的静态公钥用 nonce 1（发送侧的 EncryptAndHash 同理）。
            remote_static = self._decrypt_and_hash(take(32 + cp.TAGLEN, '对端静态公钥'))
            if len(remote_static) != 32:
                raise NoiseError('对端静态公钥长度非法')
            self.remote_static_public = remote_static
            # se：Call MixKey(DH(e, rs)) —— 本方临时 × 对端静态（= 对端的 DH(s, re)）
            self._mix_key(cp.dh(ephemeral_private, remote_static))
            payload = self._decrypt_and_hash(message[offset:])
            offset = len(message)
        else:
            raise NoiseError(f'XX 只有 3 条握手消息，越界: {index}')

        if offset != len(message):
            raise NoiseError('握手消息尾部有多余字节')
        self.message_index += 1
        return payload

    def _set_remote_ephemeral(self, peer_e: bytes) -> None:
        self._remote_ephemeral = peer_e

    def remote_ephemeral(self) -> bytes:
        """取对端临时公钥（`ee` 需要）。"""
        value = getattr(self, '_remote_ephemeral', None)
        if value is None:
            raise NoiseError('尚未收到对端临时公钥（消息顺序错误）')
        return value

    def remote_static_public_e(self) -> bytes:
        """取对端**静态**公钥（`es` / `se` 需要）。"""
        if self.remote_static_public is None:
            raise NoiseError('尚未获得对端静态公钥（消息顺序错误）')
        return self.remote_static_public

    # ── 完成与分裂 ────────────────────────────────────────────────────────

    def split(self) -> Tuple[CipherState, CipherState]:
        """握手完成后派生传输密钥，返回 (发送用, 接收用)。"""
        if self.message_index != 3:
            raise NoiseError(f'握手尚未完成（已处理 {self.message_index}/3 条消息）')
        k1, k2 = cp.hkdf(self.ck, b'', 2)
        if self.role == ROLE_INITIATOR:
            send, recv = CipherState(k1), CipherState(k2)
        else:
            send, recv = CipherState(k2), CipherState(k1)
        self.complete = True
        self._send_cipher = send
        self._recv_cipher = recv
        return send, recv

    @property
    def handshake_hash(self) -> bytes:
        """握手转录哈希，可用于日志/审计中标识这次会话。"""
        return self.h


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
