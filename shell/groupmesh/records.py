"""记录的规范化序列化与签名。

设计文档里的四类记录（团体名单、共享项声明、注册记录、解散通告）都是
"一串字段 + 一个签名"。签名能跨机验证的前提是**两端算出的字节完全相同**，
因此这里把规范化收在一处，不让各模块各拼一份：

    * 任何 dict 的键一律按键名升序排列；
    * bytes 一律 base64（ASCII）编码 —— 二进制直接进 JSON 会撞上编码差异；
    * 分隔符固定 `(',', ':')`（无空格），`ensure_ascii=False`；
    * 参与签名的字段集合由调用方显式列出（不含 `sig` 本身）。

这样 Windows 与 Linux、不同 Python 版本、不同 JSON 实现算出的字节都一致。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import crypto_prims as cp


class RecordError(Exception):
    """记录结构非法或验签失败。协议层一律按"拒绝该记录"处理。"""


def encode_fields(fields: Dict[str, Any]) -> bytes:
    """把字段字典编码成用于签名的规范化字节。"""

    def norm(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            return {'__b64__': cp.b64(bytes(value))}
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        return value

    return json.dumps(norm(fields), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def decode_bytes(value: Any, field: str) -> bytes:
    """把规范化编码里的字节标记还原回 bytes。"""
    if isinstance(value, dict) and '__b64__' in value:
        try:
            return cp.b64d(value['__b64__'])
        except cp.CryptoError as e:
            raise RecordError(f'字段 {field} 的 base64 非法: {e}') from e
    raise RecordError(f'字段 {field} 不是合法的字节标记: {value!r}')


def pretty(fields: Dict[str, Any], indent: int = 2) -> str:
    """给人看的 JSON（不用于签名）。"""
    def norm(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            return cp.b64(bytes(value))
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        return value

    return json.dumps(norm(fields), indent=indent, ensure_ascii=False, sort_keys=True)


# ── 带签名的记录基类 ──────────────────────────────────────────────────────

class SignedRecord:
    """字段集合 + Ed25519 签名的基类。

    子类需要：
      * 定义 `FIELDS`：参与签名的字段名（顺序无关，序列化时按键名排序）；
      * 实现 `_to_fields()` / `_load_fields()`。
    """

    FIELDS: tuple = ()

    def _to_fields(self) -> Dict[str, Any]:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def sign(self, private_key: bytes) -> bytes:
        """对规范化字段签名并返回签名（不修改自身）。"""
        return cp.sign(private_key, encode_fields(self._to_fields()))

    def verify(self, public_key: bytes, signature: bytes) -> bool:
        """用给定公钥验证签名（不修改自身）。"""
        return cp.verify(public_key, encode_fields(self._to_fields()), signature)

    @staticmethod
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise RecordError(message)


# ── 通用校验助手 ──────────────────────────────────────────────────────────

def b64_field(data: Dict[str, Any], field: str, expect_len: Optional[int] = None) -> bytes:
    raw = data.get(field)
    if not isinstance(raw, str):
        raise RecordError(f'字段 {field} 缺失或不是字符串')
    value = cp.b64d(raw)
    if expect_len is not None and len(value) != expect_len:
        raise RecordError(f'字段 {field} 长度应为 {expect_len}，实际 {len(value)}')
    return value


def int_field(data: Dict[str, Any], field: str, minimum: int = 0) -> int:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RecordError(f'字段 {field} 缺失或不是整数')
    if value < minimum:
        raise RecordError(f'字段 {field} 不得小于 {minimum}，实际 {value}')
    return value


def str_field(data: Dict[str, Any], field: str, allow_empty: bool = False) -> str:
    value = data.get(field)
    if not isinstance(value, str):
        raise RecordError(f'字段 {field} 缺失或不是字符串')
    if not allow_empty and not value:
        raise RecordError(f'字段 {field} 不得为空')
    return value


def str_list_field(data: Dict[str, Any], field: str, minimum: int = 0) -> List[str]:
    value = data.get(field)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RecordError(f'字段 {field} 必须是字符串数组')
    if len(value) < minimum:
        raise RecordError(f'字段 {field} 至少需要 {minimum} 项')
    if len(set(value)) != len(value):
        raise RecordError(f'字段 {field} 含重复项')
    return list(value)


def b64_list_field(data: Dict[str, Any], field: str, minimum: int = 0,
                   expect_len: Optional[int] = None) -> List[bytes]:
    value = data.get(field)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RecordError(f'字段 {field} 必须是 base64 字符串数组')
    if len(value) < minimum:
        raise RecordError(f'字段 {field} 至少需要 {minimum} 项')
    decoded = [cp.b64d(v) for v in value]
    if expect_len is not None:
        for item in decoded:
            if len(item) != expect_len:
                raise RecordError(f'字段 {field} 的元素长度应为 {expect_len}，实际 {len(item)}')
    if len(set(decoded)) != len(decoded):
        raise RecordError(f'字段 {field} 含重复项')
    return decoded


def as_callable_check(fn: Callable[[], bool], message: str) -> None:
    """把布尔检查包成"失败即抛 RecordError"。"""
    if not fn():
        raise RecordError(message)


def require_keys(data: Dict[str, Any], keys: Iterable[str]) -> None:
    missing = [k for k in keys if k not in data]
    if missing:
        raise RecordError(f'缺少字段: {", ".join(sorted(missing))}')
