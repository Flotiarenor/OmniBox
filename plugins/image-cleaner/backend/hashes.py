"""逐图 dHash 指纹库：条目缓存、落盘时机与幽灵清理。

状态自持：指纹条目与"是否已从磁盘读过"都在这里，落盘由 `save()` 统一做。
写盘走 `shell.backend.json_cache.write_json`（同目录临时文件 + fsync + `os.replace`）：
直接 `open(w)` 在进程被杀时会留下半截 JSON，指纹库从此永久损坏，而表现只是"扫描变慢"。

**读图失败返回 `None`，与"哈希值恰好是 0"分开**：纯色/近纯色图的 dHash 本来就是 0，
用 0 表示失败会让它们永远进不了相似分组。失败同样**不写缓存**，否则一次读失败会被
缓存钉住（mtime 不变就永远不再重算）。
"""

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Set, cast

from shell.backend.json_cache import read_json, write_json

log = logging.getLogger(__name__)

#: 指纹库文件名：落在壳给出的插件缓存目录下（`PluginBase.get_cache_dir()`）。
DHASH_CACHE_FILE = 'dhash.json'


class DHashStore:
    """指纹条目的内存缓存、计算与原子落盘。"""

    def __init__(self, cache_dir: Callable[[], Path], version: int) -> None:
        self._cache_dir = cache_dir
        self._version = version
        self._entries: Dict[str, Any] = {}
        self._loaded = False
        #: 内存里是否有还没落盘的改动。引擎每趟都会调 `save()`（收尾钩子），
        #: 稳态下这一趟什么都没算，整份重写只会放大 I/O；只有真的改过条目才写。
        self._dirty = False

    # ===== 路径与缓存 =====

    def path(self) -> Path:
        """指纹库文件路径（每次现算：数据根可能被宿主换掉）。"""
        return self._cache_dir() / DHASH_CACHE_FILE

    @property
    def version(self) -> int:
        """条目版本标记（`const.DHASH_VERSION`）：改哈希算法或归一化规则时 +1。

        统一刷新规格的 `content_version` 也取这个值，两处判据因此只有一个来源。
        """
        return self._version

    @staticmethod
    def key_for_abs(abs_path: str) -> str:
        """条目的键：绝对路径的摘要（绝对路径不能直接当键，换盘符会整体失效）。"""
        return 'dhash:' + hashlib.md5(abs_path.encode()).hexdigest()

    @property
    def entries(self) -> Dict[str, Any]:
        """全部条目（首次访问时从磁盘读一次）。"""
        self._ensure_loaded()
        return self._entries

    def count(self) -> int:
        return len(self.entries)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        # 只接受 dict：文件被外部改成列表时按"没有缓存"处理，下一次扫描重算。
        data = read_json(self.path(), default=None)
        if isinstance(data, dict):
            self._entries = data
        self._loaded = True

    # ===== 计算 =====

    def get(self, abs_path: str, mtime: float) -> Optional[int]:
        """64-bit 差异哈希（dHash）；读图失败返回 None 且不写缓存。

        缓存条目带 `'v'`：旧版把"读图失败"也作为 hash 0 存进缓存，缺版本号的条目
        一律重算，避免那些 0 被当成合法哈希继续参与分组。
        """
        key = self.key_for_abs(abs_path)
        self._ensure_loaded()
        cached = self._entries.get(key)
        if cached and cached.get('v') == self._version and cached.get('mtime') == mtime:
            return int(cached.get('hash', 0))

        try:
            from PIL import Image
            with Image.open(abs_path) as img:
                small = img.convert('L').resize((9, 8))
            pixels = list(cast(Iterable[int], small.getdata()))
        except Exception:
            return None

        value = 0
        for row in range(8):
            for col in range(8):
                value <<= 1
                if pixels[row * 9 + col] > pixels[row * 9 + col + 1]:
                    value |= 1
        self._entries[key] = {'v': self._version, 'mtime': mtime, 'hash': value}
        self._dirty = True
        return value

    # ===== 落盘与清理 =====

    def save(self) -> None:
        """把当前条目整份原子落盘；失败只记日志（缓存写不进去不该打断扫描）。

        没有未落盘的改动时直接返回：引擎的收尾钩子每趟都会调它，包括"什么都没变"的
        被动同步 —— 那种情况重写一份几 MB 的 JSON 没有意义。
        """
        self._ensure_loaded()
        if not self._dirty:
            return
        path = self.path()
        try:
            written = write_json(path, self._entries, indent=None, create_parent=True)
        except OSError as error:      # 父目录建不出来（权限、路径失效）时同样只记日志
            log.error(f'保存 dHash 缓存失败: {path}: {error}')
            return
        if not written:
            log.error(f'保存 dHash 缓存失败: {path}')
            return
        self._dirty = False

    def drop(self, key: str) -> bool:
        """删掉单条指纹；返回是否真的删掉了。"""
        self._ensure_loaded()
        removed = self._entries.pop(key, None) is not None
        if removed:
            self._dirty = True
        return removed

    def prune(self, valid: Set[str]) -> int:
        """只保留 `valid` 里的条目，返回删除条数（校验趟的对账）。"""
        self._ensure_loaded()
        dropped = 0
        for key in [k for k in self._entries if k not in valid]:
            self._entries.pop(key, None)
            dropped += 1
        if dropped:
            self._dirty = True
        return dropped

    def clear(self) -> None:
        """丢弃全部指纹并删除落盘文件（统一刷新的逃生门）。"""
        self._entries = {}
        self._loaded = True
        # 文件已经删掉，没有需要落盘的内容；之后的 `get()` 会重新把脏标记打开。
        self._dirty = False
        try:
            path = self.path()
            if path.exists():
                path.unlink()
        except OSError as e:
            log.warning(f'删除 dHash 缓存失败: {e}')
