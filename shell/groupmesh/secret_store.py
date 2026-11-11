"""私钥文件的本地保护层。

私钥保护的要求是"设备私钥不导出，由操作系统提供的密钥存储保存"。本模块把
`principal.json` / `devices/<id>.json` 的**整个 JSON 内容**包一层保护，落盘格式：

```json
{
  "format": "cubby-secret-v1",
  "protector": "dpapi" | "keyring" | "passphrase",
  "data": "<base64>",
  ...
}
```

四种保护级别，按可用性自动选择：

| protector | 平台 / 条件 | 保护来源 | 明文落盘吗 |
| --- | --- | --- | --- |
| `dpapi` | Windows | 当前 Windows 用户的 DPAPI | 否 |
| `passphrase` | 设了 `CUBBY_SECRET_KEY` | scrypt(passphrase) + AES-256-GCM | 否 |
| `keyring` | 有可用 OS keyring（桌面 Linux / macOS） | 随机 32 字节密钥存 OS keyring，文件用 AES-256-GCM | 否 |
| `plain` | 以上都不可用 | 无 | **是**（保留旧行为并告警） |

几个刻意的取舍：

* **不静默降级读取**：文件声明用 `keyring` / `dpapi` 时，取不回密钥就报
  `SecretError`，绝不把密文当明文继续解析 —— 后者会生成一个"看起来正常"的空身份。
* **迁移是单向的**：`migrate_file()` 只把"未保护的旧文件"升级成当前可用的最强
  保护；已经保护的文件不会被降级重写（避免一次 keyring 故障把私钥写成明文）。
* **1.4.0 的信封不当作明文**：`LEGACY_FORMAT`（`omnibox-secret-v1`，DPAPI 旧熵
  `omnibox.group-mesh.secret.v1`）是上一代的受保护文件。`unprotect()` 命中它时
  直接报错要求先跑迁移脚本，**不回退成明文、也不重写文件** —— 若把它当明文，
  上层只会得到一句"字段 name 缺失"，而 `migrate_file()` 会把旧信封再封装一层，
  让本来还能被迁移脚本剥开的密钥多绕一层。
* **`plain` 不伪造安全感**：不写 `format` 信封，直接保留旧 JSON，`describe()`
  会如实报告 `active == "plain"`，插件状态页据此显示警告。
* **显式覆盖**：`CUBBY_SECRET_PROTECTOR` 可强制指定
  `dpapi|keyring|passphrase|plain`，用于排障与用例；`CUBBY_SECRET_KEY` 提供
  passphrase；`CUBBY_SECRET_NO_MIGRATE=1` 关闭自动迁移。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

FORMAT = 'cubby-secret-v1'
#: 1.4.0 的信封标识：2.0 不解这个格式（DPAPI 熵也从旧值换成了 `ENTROPY`），
#: 命中时明确报错要求先跑 Release 里的 1.4.0 → 2.0 迁移脚本。**不**把它当明文。
LEGACY_FORMAT = 'omnibox-secret-v1'
ENTROPY = b'cubby.group-mesh.secret.v1'
KEYRING_SERVICE = 'cubby-group-mesh'
_KEYRING_PROBE_USER = '__cubby_probe__'
PASSPHRASE_ENV = 'CUBBY_SECRET_KEY'
PROTECTOR_ENV = 'CUBBY_SECRET_PROTECTOR'
NO_MIGRATE_ENV = 'CUBBY_SECRET_NO_MIGRATE'

PROTECTORS = ('dpapi', 'keyring', 'passphrase', 'plain')

_MAX_PASSPHRASE_BYTES = 4096


class SecretError(Exception):
    """保护/解保护失败。调用方应把它转成"身份不可读"，而不是静默继续。"""


# 缓存 best_protector() 的结果：keyring 探测可能触发一次后端访问，不该每个文件都做。
_best_cache: Optional[str] = None
_keyring_cache: Optional[bool] = None


def reset_cache() -> None:
    """清空探测缓存（用例改环境变量 / monkeypatch 后需要）。"""
    global _best_cache, _keyring_cache
    _best_cache = None
    _keyring_cache = None


# ── 环境与可用性 ──────────────────────────────────────────────────────────

def _passphrase() -> Optional[bytes]:
    value = os.environ.get(PASSPHRASE_ENV) or ''
    if not value:
        return None
    raw = value.encode('utf-8')
    if len(raw) > _MAX_PASSPHRASE_BYTES:
        raise SecretError(f'{PASSPHRASE_ENV} 过长（>{_MAX_PASSPHRASE_BYTES} 字节）')
    return raw


def _forced_protector() -> Optional[str]:
    value = (os.environ.get(PROTECTOR_ENV) or '').strip().lower()
    if not value:
        return None
    if value not in PROTECTORS:
        raise SecretError(f'{PROTECTOR_ENV}={value!r} 非法，可选: {", ".join(PROTECTORS)}')
    return value


def _keyring_module():
    try:
        import keyring
    except Exception as e:  # ImportError / 后端初始化异常
        log.debug('[group-mesh] keyring 不可用: %s', e)
        return None
    return keyring


def keyring_available() -> bool:
    """OS keyring 是否可用（带一次 set/get/delete 往返探测，结果缓存）。"""
    global _keyring_cache
    if _keyring_cache is not None:
        return _keyring_cache
    module = _keyring_module()
    if module is None:
        _keyring_cache = False
        return False
    try:
        backend = module.get_keyring()
        name = f'{type(backend).__module__}.{type(backend).__name__}'.lower()
        if 'fail' in name:
            _keyring_cache = False
            return False
        module.set_password(KEYRING_SERVICE, _KEYRING_PROBE_USER, 'probe')
        ok = module.get_password(KEYRING_SERVICE, _KEYRING_PROBE_USER) == 'probe'
        try:
            module.delete_password(KEYRING_SERVICE, _KEYRING_PROBE_USER)
        except Exception:  # pragma: no cover - 删除失败不影响判定
            pass
        _keyring_cache = bool(ok)
    except Exception as e:
        log.debug('[group-mesh] keyring 探测失败: %s', e)
        _keyring_cache = False
    return _keyring_cache


def best_protector() -> str:
    """当前应使用的保护级别（可被环境变量覆盖）。"""
    global _best_cache
    forced = _forced_protector()
    if forced is not None:
        return forced
    if _best_cache is not None:
        return _best_cache
    if _passphrase() is not None:
        _best_cache = 'passphrase'
    elif sys.platform == 'win32':
        _best_cache = 'dpapi'
    elif keyring_available():
        _best_cache = 'keyring'
    else:
        _best_cache = 'plain'
    return _best_cache


def describe() -> Dict[str, Any]:
    """给插件状态页看的保护级别说明（不触发写操作）。"""
    try:
        active = best_protector()
        error = ''
    except SecretError as e:
        active, error = 'plain', str(e)
    return {
        'active': active,
        'protected': active != 'plain',
        'platform': sys.platform,
        'passphrase_env': PASSPHRASE_ENV,
        'passphrase_from_env': _passphrase() is not None,
        'keyring_available': bool(_keyring_cache) if _keyring_cache is not None else False,
        'warning': ('当前私钥按明文落盘：设置 ' + PASSPHRASE_ENV + ' 可启用口令保护，'
                    '或安装并登录桌面 keyring') if active == 'plain' else '',
        'error': error,
    }


# ── DPAPI（Windows）────────────────────────────────────────────────────────

def _dpapi_call(protect: bool, data: bytes) -> bytes:
    if sys.platform != 'win32':
        raise SecretError('DPAPI 只在 Windows 上可用')
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)  # type: ignore[attr-defined]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    def _blob(payload: bytes) -> DataBlob:
        buffer = ctypes.create_string_buffer(payload, len(payload))
        return DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    blob_in = _blob(data)
    blob_entropy = _blob(ENTROPY)
    blob_out = DataBlob()

    if protect:
        fn = crypt32.CryptProtectData
        descr = ctypes.c_wchar_p('cubby group-mesh secret')
    else:
        fn = crypt32.CryptUnprotectData
        descr = None
    fn.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p,
                   ctypes.POINTER(DataBlob), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    fn.restype = wintypes.BOOL

    ok = fn(ctypes.byref(blob_in), descr, ctypes.byref(blob_entropy),
            None, None, 0x1, ctypes.byref(blob_out))  # 0x1 = CRYPTPROTECT_UI_FORBIDDEN
    if not ok:
        raise SecretError(f'DPAPI {"加密" if protect else "解密"}失败: '
                          f'{ctypes.WinError(ctypes.get_last_error())}')
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ── AES-256-GCM（keyring / passphrase）─────────────────────────────────────

def _aesgcm_key_from_passphrase(passphrase: bytes, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    kdf = Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1)
    return kdf.derive(passphrase)


def _aesgcm_encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def _aesgcm_decrypt(key: bytes, payload: bytes, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(payload) < 13:
        raise SecretError('AES-GCM 载荷过短')
    nonce, ciphertext = payload[:12], payload[12:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as e:
        raise SecretError(f'AES-GCM 解密失败（口令不对或文件被篡改）: {e}') from e


def _aad(protector: str) -> bytes:
    return f'{FORMAT}|{protector}'.encode('ascii')


# ── 保护 / 解保护 ─────────────────────────────────────────────────────────

def protect(raw: bytes, protector: Optional[str] = None) -> bytes:
    """把原始 JSON 字节包成受保护的文件内容；`plain` 时原样返回。"""
    chosen = protector or best_protector()
    if chosen == 'plain':
        return raw
    if chosen == 'dpapi':
        blob = _dpapi_call(True, raw)
        envelope = {'format': FORMAT, 'protector': 'dpapi',
                    'data': base64.b64encode(blob).decode('ascii')}
    elif chosen == 'passphrase':
        passphrase = _passphrase()
        if passphrase is None:
            raise SecretError(f'{PROTECTOR_ENV}=passphrase 但未设置 {PASSPHRASE_ENV}')
        salt = secrets.token_bytes(16)
        key = _aesgcm_key_from_passphrase(passphrase, salt)
        envelope = {'format': FORMAT, 'protector': 'passphrase',
                    'salt': base64.b64encode(salt).decode('ascii'),
                    'data': base64.b64encode(_aesgcm_encrypt(key, raw, _aad('passphrase'))).decode('ascii')}
    elif chosen == 'keyring':
        key_id = secrets.token_hex(16)
        key = secrets.token_bytes(32)
        _keyring_store(key_id, key)
        envelope = {'format': FORMAT, 'protector': 'keyring', 'key_id': key_id,
                    'data': base64.b64encode(_aesgcm_encrypt(key, raw, _aad('keyring'))).decode('ascii')}
    else:
        raise SecretError(f'未知保护级别: {chosen!r}')
    return (json.dumps(envelope, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def envelope_format(raw: bytes) -> Optional[str]:
    """文件信封的 `format` 字段；不是 JSON 对象或没有字符串形 format 时返回 None。"""
    try:
        envelope = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(envelope, dict):
        return None
    value = envelope.get('format')
    return value if isinstance(value, str) else None


def is_legacy_envelope(raw: bytes) -> bool:
    """是否是 1.4.0 的密钥信封（`LEGACY_FORMAT`）。

    只用于"识别并拒绝"：2.0 不去解密它，也绝不对它做 `protect()` 重封装。
    """
    return envelope_format(raw) == LEGACY_FORMAT


def unprotect(raw: bytes) -> bytes:
    """解出原始 JSON 字节。旧版明文 JSON（没有 `format` 字段）原样返回。

    1.4.0 的受保护信封不算"旧版明文"：命中 `LEGACY_FORMAT` 时抛 `SecretError`，
    让调用方把"需要先跑迁移脚本"原样报给用户，而不是拿信封本身当 JSON 继续解析。
    """
    try:
        envelope = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw  # 非 JSON：交给上层报"文件损坏"，不在这一层猜
    if not isinstance(envelope, dict):
        return raw  # 顶层不是对象：同样交给上层报"文件损坏"
    file_format = envelope.get('format')
    if file_format == LEGACY_FORMAT:
        raise SecretError(
            '该文件是 1.4.0 的密钥信封（omnibox-secret-v1），2.0 不解这个格式：'
            '请先跑 Release 里的 1.4.0 → 2.0 迁移脚本；本次读取没有改动文件'
        )
    if file_format != FORMAT:
        return raw  # 迁移前的旧明文 JSON（没有 format 字段）

    protector = envelope.get('protector')
    data = envelope.get('data')
    if not isinstance(protector, str) or not isinstance(data, str):
        raise SecretError('受保护文件缺少 protector/data 字段')
    try:
        payload = base64.b64decode(data, validate=True)
    except Exception as e:
        raise SecretError(f'受保护文件的 base64 非法: {e}') from e

    if protector == 'dpapi':
        return _dpapi_call(False, payload)
    if protector == 'passphrase':
        passphrase = _passphrase()
        if passphrase is None:
            raise SecretError(f'该文件用口令保护，但未设置 {PASSPHRASE_ENV}')
        salt_b64 = envelope.get('salt')
        if not isinstance(salt_b64, str):
            raise SecretError('口令保护文件缺少 salt')
        try:
            salt = base64.b64decode(salt_b64, validate=True)
        except Exception as e:
            raise SecretError(f'salt 非法: {e}') from e
        key = _aesgcm_key_from_passphrase(passphrase, salt)
        return _aesgcm_decrypt(key, payload, _aad('passphrase'))
    if protector == 'keyring':
        key_id = envelope.get('key_id')
        if not isinstance(key_id, str):
            raise SecretError('keyring 保护文件缺少 key_id')
        key = _keyring_load(key_id)
        return _aesgcm_decrypt(key, payload, _aad('keyring'))
    raise SecretError(f'未知保护级别: {protector!r}')


def is_protected(raw: bytes) -> bool:
    try:
        envelope = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(envelope, dict) and envelope.get('format') == FORMAT


def migrate_file(path: Path) -> bool:
    """把未保护的旧文件升级成当前可用的最强保护；返回是否发生改写。

    只做"升级"：已经受保护、是 1.4.0 的旧信封（`LEGACY_FORMAT`，要由迁移脚本
    重封装）或当前只有 plain 可用时都不改。任何失败只告警，不影响调用方读取旧内容。
    """
    if os.environ.get(NO_MIGRATE_ENV):
        return False
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if is_protected(raw):
        return False
    if is_legacy_envelope(raw):
        # 绝不对旧信封做 protect()：那会把它当明文再包一层，迁移脚本要多剥一层。
        log.warning('[group-mesh] %s 是 1.4.0 的密钥信封，跳过保护升级（先跑迁移脚本）', path.name)
        return False
    try:
        chosen = best_protector()
    except SecretError:
        return False
    if chosen == 'plain':
        return False
    try:
        blob = protect(raw, chosen)
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_bytes(blob)
        os.replace(tmp, path)
        log.info('[group-mesh] 已把 %s 升级为 %s 保护', path.name, chosen)
        return True
    except Exception as e:
        log.warning('[group-mesh] 保护升级失败（保留原文件）%s: %s', path, e)
        return False


# ── keyring 存取 ──────────────────────────────────────────────────────────

def _keyring_store(key_id: str, key: bytes) -> None:
    module = _keyring_module()
    if module is None:
        raise SecretError('keyring 不可用')
    try:
        module.set_password(KEYRING_SERVICE, key_id,
                            base64.b64encode(key).decode('ascii'))
    except Exception as e:
        raise SecretError(f'写入 keyring 失败: {e}') from e


def _keyring_load(key_id: str) -> bytes:
    module = _keyring_module()
    if module is None:
        raise SecretError('该文件用 keyring 保护，但当前环境没有可用的 keyring')
    try:
        value = module.get_password(KEYRING_SERVICE, key_id)
    except Exception as e:
        raise SecretError(f'读取 keyring 失败: {e}') from e
    if not value:
        raise SecretError(f'keyring 里找不到密钥 {key_id}（换机器或换了用户？）')
    try:
        key = base64.b64decode(value, validate=True)
    except Exception as e:
        raise SecretError(f'keyring 中的密钥不是合法 base64: {e}') from e
    if len(key) != 32:
        raise SecretError(f'keyring 中的密钥长度不是 32 字节（实际 {len(key)}）')
    return key
