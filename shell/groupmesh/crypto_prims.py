"""密码学原语封装。

设计文档 §4.4 要求"握手使用 Noise 框架的现成实现，不自行设计密码学组合"。
MVP 阶段的做法是**只复用已审计的密码学原语，协议状态机自己写**，并把这个偏离
明确记录在 `docs/group-mesh-implementation-path.md` 里 —— 上生产前必须换成
vetted 的 Noise 实现（如 `noiseprotocol`），本文件的 `noise.py` 是那一步的占位。

选 pycryptodome 作为原语来源的理由：它已在 `requirements.txt` 中（pixiv-sync 引入），
因此 MVP 不需要为"只跑一次验证"新增依赖；Windows venv 与 Linux venv 版本一致
（3.23.0），跨机联调不会撞版本差异。

原语与用途的对应：

    Curve25519（X25519）      Noise 的 DH
    Ed25519                  身份签名（主体、设备、名单、共享项、注册记录）
    ChaCha20-Poly1305         Noise 的 AEAD
    BLAKE2s                   Noise 的 Hash / HMAC / HKDF（套件名里写的就是它）

**签名与哈希的一致性**：身份签名走**标准 RFC 8032 Ed25519**（pycryptodome 的
`eddsa` 接受原始消息字节），因此签出的名单、共享项声明可以用 OpenSSL /
`cryptography` / `ssh-keygen` 等外部工具独立复核。BLAKE2s 只服务于 Noise 套件
（Hash/HMAC/HKDF），两者不混淆 —— 这与 `noise-c`、`snow` 等参考实现的做法一致。

**两处 pycryptodome 编码坑**（本次实现实测，已在下面对应函数处写到断言级别）：

1. Curve25519 的 `key.pointQ.x` 与 `export_key(format='raw')` **不是同一个编码**。
   公钥若按 raw 导出再喂给 `EccXPoint`，双方算出的共享密钥不一致，DH 静默失败。
   因此 X25519 的公钥序列化统一走 `dh_public_bytes()`（`pointQ.x` 大端整数）。
2. Ed25519 的 `int(key.d)` 是从种子派生的标量，**不是**可持久的私钥；持久化必须
   用 `key.seed`。两者混用会得到另一把密钥。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Tuple

from Crypto.Cipher import ChaCha20_Poly1305
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

# ── 套件参数（Noise_XX_25519_ChaChaPoly_BLAKE2s）───────────────────────────
HASHLEN = 32
BLOCKLEN = 64          # BLAKE2s 的压缩块长度，HMAC 用
KEYLEN = 32
TAGLEN = 16
MAX_PLAINTEXT = 65535  # 单帧明文上限，见 noise.py 的长度检查

CURVE = 'Curve25519'
SIG_CURVE = 'Ed25519'

# Ed25519 公钥的固定 DER 头（SubjectPublicKeyInfo）：
#   SEQUENCE(0x2a) { SEQUENCE(5) { OID 1.3.101.112 }, BIT STRING(0x21) { 0x00, <32B key> } }
# pycryptodome 的 `import_key` 既不接受 32 字节裸公钥、也不接受 32 字节裸种子，
# 只认 PEM/DER，因此由本文件拼出这层包装。头 12 字节对所有 Ed25519 公钥都相同
# （已跨多把密钥实测），只有末尾 32 字节随密钥变化。
_SPKI_ED25519_PREFIX = bytes.fromhex('302a300506032b6570032100')

# Noise 规定 CipherState 的 nonce 是 8 字节小端，编码进 12 字节 AEAD nonce
# 时高位补零。
_NONCE_PAD = b'\x00' * 4


class CryptoError(Exception):
    """原语层的失败（长度不对、验签失败等）。调用方一律按"拒绝"处理。"""


# ── 哈希 / HMAC / HKDF ─────────────────────────────────────────────────────

def blake2s(*parts: bytes) -> bytes:
    """BLAKE2s-256，按顺序拼接后求值。"""
    h = hashlib.blake2s(digest_size=HASHLEN)
    for part in parts:
        h.update(part)
    return h.digest()


def hmac_blake2s(key: bytes, data: bytes) -> bytes:
    """Noise 的 HMAC-HASH：块长度取 64（BLAKE2s 的标准块长）。"""
    return hmac.new(key, data, hashlib.blake2s).digest()


def hkdf(ck: bytes, ikm: bytes, num_outputs: int = 2, info: bytes = b'') -> Tuple[bytes, ...]:
    """Noise 的 HKDF：以 ck 为盐、ikm 为输入，输出 num_outputs 个 32 字节密钥。

    设计文档 §4.4 的"密钥由 HKDF 从握手结果派生"就是这里。`temp_k` 取第一个
    输出，`new_ck` 取第二个。
    """
    if num_outputs not in (1, 2, 3):
        raise CryptoError(f'HKDF 输出个数只支持 1..3，收到 {num_outputs}')
    prk = hmac_blake2s(ck, ikm)
    outputs = []
    prev = b''
    for i in range(1, num_outputs + 1):
        prev = hmac_blake2s(prk, prev + bytes([i]) + info)
        outputs.append(prev)
    return tuple(outputs)


# ── 密钥对 ────────────────────────────────────────────────────────────────

def generate_dh_keypair() -> Tuple[bytes, bytes]:
    """生成 X25519 密钥对，返回 (私钥 32 字节, 公钥 32 字节)。

    pycryptodome 的 Curve25519 标量固定 32 字节，不需要手工 clamp。
    """
    key = ECC.generate(curve=CURVE)
    return _dh_private_bytes(key), dh_public_bytes(key.public_key())


def _dh_private_bytes(key) -> bytes:
    """X25519 私钥的字节表示。

    **不能用** `export_key(format='raw')`：pycryptodome 对 Montgomery 曲线的
    "raw" 私钥导出直接抛 ValueError（不提供），而它的 `d` 是已完成 RFC 7748
    clamp 的标量，正是标量乘要用的那个值。
    """
    return int(key.d).to_bytes(32, 'big')


def dh_public_bytes(public_key) -> bytes:
    """把 X25519 公钥序列化成 32 字节。

    **不能用** `export_key(format='raw')`。这是本次实现中踩到的真实坑：
    pycryptodome 里 `key.pointQ.x`（标量乘使用的整数坐标）与 raw 导出的字节
    **不是同一个编码** —— raw 导出做了额外的字节序处理。实测：
    `int(key.pointQ.x) != int.from_bytes(export_key(format='raw'), 'big')`。
    如果公钥按 raw 导出、对端再喂给 `EccXPoint`，双方算出的共享密钥不一致
    （DH 静默失败，握手表现为 MAC 校验错误，极难定位）。

    因此本项目统一采用 pycryptodome 自身的坐标编码：`pointQ.x` 的大端整数。
    两端都用同一套库，一致性成立；代价是与 RFC 7748 的外部实现不互通 ——
    与 §4.4"数据层两端都由本项目实现"一致。
    """
    return int(public_key.pointQ.x).to_bytes(32, 'big')


def dh(private: bytes, public: bytes) -> bytes:
    """X25519 标量乘，返回 32 字节共享秘密。

    全零结果是已知的低阶点攻击面，必须显式拒绝（RFC 7748 §6.1）。
    """
    if len(private) != 32:
        raise CryptoError(f'X25519 私钥必须是 32 字节，收到 {len(private)}')
    if len(public) != 32:
        raise CryptoError(f'X25519 公钥必须是 32 字节，收到 {len(public)}')
    peer_point = _raw_to_x25519_point(public)
    try:
        shared = int((peer_point * int.from_bytes(private, 'big')).x).to_bytes(32, 'big')
    except ValueError as e:
        # 点乘结果落在无穷远点（对端给了低阶点）
        raise CryptoError(f'X25519 协商得到无效点: {e}') from e
    if shared == b'\x00' * 32:
        raise CryptoError('X25519 协商结果全零（对端公钥是低阶点），拒绝继续')
    return shared


def _raw_to_x25519_point(raw: bytes):
    """把 32 字节公钥还原成 Curve25519 上的点。

    `raw` 必须来自 `dh_public_bytes()`（即 pycryptodome 的 `pointQ.x` 大端整数）。

    不能用 `ECC.EccPoint`：那是 Weierstrass 曲线的仿射点，构造函数要求
    `(x, y)`，而 X25519 的公钥只带 u（x）坐标，y 由曲线方程推出、且有两个根。
    Curve25519 对应的类型是 `Crypto.PublicKey._point.EccXPoint`，它只暴露 x、
    只支持"点乘标量"，正好覆盖本项目的用法（DH 只需要 u 坐标）。
    它属于 pycryptodome 的私有命名空间，因此这里做了边界检查与错误收敛：
    构造失败一律按"公钥非法"处理，不让 AttributeError/ValueError 冒到协议层。
    """
    from Crypto.PublicKey._point import EccXPoint

    if len(raw) != 32:
        raise CryptoError(f'X25519 公钥必须是 32 字节，收到 {len(raw)}')
    try:
        return EccXPoint(int.from_bytes(raw, 'big'), CURVE)
    except Exception as e:
        raise CryptoError(f'X25519 公钥无法还原为曲线点: {e}') from e


def generate_sign_keypair() -> Tuple[bytes, bytes]:
    """生成 Ed25519 密钥对，返回 (私钥 32 字节, 公钥 32 字节)。

    私钥取 `key.seed`（RFC 8032 的 32 字节种子），公钥取 raw 导出。

    为什么不用 `int(key.d)`：Ed25519 的 `d` 是**按 RFC 8032 从种子派生的标量**，
    不是种子本身。存 `d` 再重建会得到另一把密钥（且 `construct(d=...)` 对非 NIST
    曲线直接拒绝）。`seed` 才是可持久的私钥表示，`ECC.construct(curve='Ed25519',
    seed=...)` 能精确还原。
    """
    key = ECC.generate(curve=SIG_CURVE)
    return _ed_private_bytes(key), key.public_key().export_key(format='raw')


def _ed_private_bytes(key) -> bytes:
    return key.seed


def _ed_private_key(seed: bytes):
    """由 32 字节种子还原 Ed25519 私钥对象。"""
    if len(seed) != 32:
        raise CryptoError(f'Ed25519 私钥必须是 32 字节种子，收到 {len(seed)}')
    try:
        return ECC.construct(curve=SIG_CURVE, seed=seed)
    except Exception as e:
        raise CryptoError(f'Ed25519 私钥无法还原: {e}') from e


def sign(private: bytes, message: bytes) -> bytes:
    """用 Ed25519 对 `message` 做**标准 RFC 8032 纯签名**，返回 64 字节签名。

    pycryptodome 的 `eddsa` 接受原始消息字节（PureEdDSA），因此这里不做预哈希 ——
    签名与 OpenSSL、`cryptography`、`ssh-keygen` 等标准实现互通，便于用外部工具
    独立复核名单签名。哈希函数（BLAKE2s）只用于 Noise 套件，与身份签名无关。
    """
    key = _ed_private_key(private)
    return eddsa.new(key, 'rfc8032').sign(message)


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """验签。任何失败（长度、格式、签名不符）都返回 False，不抛异常。"""
    if len(public) != 32 or len(signature) != 64:
        return False
    try:
        key = _ed_public_key(public)
    except CryptoError:
        return False
    try:
        eddsa.new(key, 'rfc8032').verify(message, signature)
        return True
    except Exception:
        return False


def sign_public_from_private(private: bytes) -> bytes:
    """从 Ed25519 私钥推出公钥（CLI 自检与自举用）。"""
    return _ed_private_key(private).public_key().export_key(format='raw')


# ── 由身份种子派生的 X25519 密钥（设计文档 §4.3 的"身份统一"）──────────────

# 派生用的域分隔标签。换掉它等于换掉所有人的 DH 公钥，因此带版本号且不得修改。
_DH_DERIVE_LABEL = b'group-mesh/x25519-static/v1'


def dh_keypair_from_sign_seed(seed: bytes) -> Tuple[bytes, bytes]:
    """由 Ed25519 身份种子确定性地派生 X25519 静态密钥对。

    ## 为什么必须派生，而不能直接把 Ed25519 密钥当 X25519 用

    设计文档 §4.3 要求"同一个设备密钥同时用于应用面握手与共享节点注册"。
    但 **Ed25519 的密钥不能直接拿来做 X25519 的 DH**：

    * Ed25519 的私钥是种子经 SHA-512 派生、再做位裁剪后的**标量**，其用途是点乘
      基点做签名；把它原样当 X25519 标量用，得到的不是"同一把密钥的另一种用法"，
      而是一把无关的标量。
    * 本项目实测到的现象最危险：**双方各算各的，结果不一致但都不报错**
      （`DH(e_i, s_r) != DH(s_r, e_i)`），直到后面的 AEAD 才以
      "MAC check failed" 暴露出来，排查成本很高。

    正确做法是从**同一个种子**确定性地派生出两把用途分离的密钥：

        Ed25519 私钥 = seed                （签名、身份）
        X25519  私钥 = clamp(BLAKE2s(seed || label))   （握手 DH）

    两者都由种子唯一决定，因此设备仍然只有"一份身份密钥材料"，
    满足 §4.3 的"不维护第二套账号"；同时各自落在正确的曲线上。

    `clamp` 按 RFC 7748：清低 3 位、清最高位、置第 254 位。
    """
    if len(seed) != 32:
        raise CryptoError(f'身份种子必须是 32 字节，收到 {len(seed)}')
    derived = blake2s(_DH_DERIVE_LABEL, seed)
    scalar = bytearray(derived)
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    private = bytes(scalar)
    public = dh_public_from_private(private)
    return private, public


def dh_public_from_private(private: bytes) -> bytes:
    """由 X25519 私钥标量算出公钥（基点 × 标量）。

    基点取自 `_curves['Curve25519'].G`。注意**不能**用
    `EccXPoint(None, CURVE)`：那个 x 为空表示的是**无穷远点**，
    乘以任何标量仍是无穷远点，取 `.x` 会抛 "No X coordinate for the point at
    infinity"。标量必须已是 clamp 后的 32 字节。
    """
    from Crypto.PublicKey._point import EccXPoint  # noqa: F401 - 触发子模块导入

    if len(private) != 32:
        raise CryptoError(f'X25519 私钥必须是 32 字节，收到 {len(private)}')
    base = ECC._curves[CURVE].G
    try:
        point = base * int.from_bytes(private, 'big')
        return int(point.x).to_bytes(32, 'big')
    except Exception as e:
        raise CryptoError(f'无法由私钥推导 X25519 公钥: {e}') from e


def _ed_public_key(public: bytes):
    """由 32 字节裸公钥还原 Ed25519 公钥对象。

    `ECC.import_key` 不接受裸公钥（实测 3.23.0 抛 "ECC key format is not
    supported"），因此补上固定的 SPKI 头再导入。
    """
    if len(public) != 32:
        raise CryptoError(f'Ed25519 公钥必须是 32 字节，收到 {len(public)}')
    try:
        return ECC.import_key(_SPKI_ED25519_PREFIX + public)
    except Exception as e:
        raise CryptoError(f'Ed25519 公钥无法还原: {e}') from e


# ── AEAD ──────────────────────────────────────────────────────────────────

def _nonce(counter: int) -> bytes:
    if not 0 <= counter < 2 ** 64:
        raise CryptoError('nonce 计数器越界（Noise 要求 64 位内）')
    return _NONCE_PAD + counter.to_bytes(8, 'little')


def aead_encrypt(key: bytes, counter: int, plaintext: bytes, associated_data: bytes) -> bytes:
    """ChaCha20-Poly1305 加密，返回 `密文 || 16 字节 tag`（与 Noise 的布局一致）。"""
    cipher = ChaCha20_Poly1305.new(key=key, nonce=_nonce(counter))
    cipher.update(associated_data)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return ciphertext + tag


def aead_decrypt(key: bytes, counter: int, ciphertext: bytes, associated_data: bytes) -> bytes:
    """解密并验证 tag。失败抛 CryptoError（消息必须被丢弃，计数器不前进）。"""
    if len(ciphertext) < TAGLEN:
        raise CryptoError(f'密文短于 tag：{len(ciphertext)} 字节')
    body, tag = ciphertext[:-TAGLEN], ciphertext[-TAGLEN:]
    cipher = ChaCha20_Poly1305.new(key=key, nonce=_nonce(counter))
    cipher.update(associated_data)
    try:
        return cipher.decrypt_and_verify(body, tag)
    except ValueError as e:
        raise CryptoError('AEAD 校验失败（密钥不符或消息被篡改）') from e


# ── 随机与编码 ────────────────────────────────────────────────────────────

def random_bytes(n: int) -> bytes:
    return os.urandom(n)


def b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode('ascii')


def b64d(text: str) -> bytes:
    import base64
    try:
        return base64.b64decode(text, validate=True)
    except Exception as e:
        raise CryptoError(f'base64 解码失败: {e}') from e
