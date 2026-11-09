"""扫描结果缓存：按模式存分组、stale 标记与失效文件过滤。

状态自持：结果缓存的内存过期标记与落盘都在这里。写盘走
`shell.backend.json_cache.write_json`（同目录临时文件 + fsync + `os.replace`）：
直接 `open(w)` 在进程被杀时会留下半截 JSON，缓存从此永久损坏，而表现只是"打开变慢"。

`stale` 只在内存里标记、由 `persist_stale()` 统一落盘：结果缓存是单文件，不能每处理
一个目录就整体重写一遍。

结果缓存是"读整份 → 改一个键 → 写整份"，而后台扫描任务与引擎的收尾钩子会并发写它；
两处共用一把锁，避免后写者用自己读到的旧快照把前一位的写入覆盖掉。
"""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Set, Tuple

from shell.backend.json_cache import read_json, write_json

log = logging.getLogger(__name__)

#: 结果缓存文件名：与指纹库同目录（壳给出的插件缓存目录）。
SCAN_CACHE_FILE = 'scan_cache.json'


class ScanResults:
    """结果缓存的读写与失效判定。"""

    def __init__(self, cache_dir: Callable[[], Path], host: Any,
                 modes: Tuple[str, ...]) -> None:
        self._cache_dir = cache_dir
        self._host = host
        self._modes = modes
        #: 结果缓存里哪些 mode 已经过期（内存标记，落盘由 persist_stale() 统一做）
        self._stale: Set[str] = set()
        #: 串行化"读整份 → 改 → 写整份"的两条写路径（后台扫描任务与引擎收尾钩子）
        self._write_lock = threading.Lock()

    # ===== 路径与读写 =====

    def path(self) -> Path:
        return self._cache_dir() / SCAN_CACHE_FILE

    def load(self) -> Dict[str, Any]:
        """读结果缓存；缺失或不是 dict 时返回空 dict。"""
        data = read_json(self.path(), default=None)
        if isinstance(data, dict):
            return data
        return {}

    def save(self, cache: Dict[str, Any]) -> None:
        """整份原子落盘；失败只记日志（缓存写不进去不该打断扫描）。"""
        path = self.path()
        try:
            written = write_json(path, cache, indent=None, create_parent=True)
        except OSError as error:      # 父目录建不出来（权限、路径失效）时同样只记日志
            log.error(f'保存扫描结果缓存失败: {path}: {error}')
            return
        if not written:
            log.error(f'保存扫描结果缓存失败: {path}')

    def save_result(self, mode: str, groups: list, scanned: int) -> None:
        with self._write_lock:
            cache = self.load()
            cache[mode] = {
                'groups': groups,
                'scanned': scanned,
                'saved_at': time.time(),
                'stale': False,
            }
            self.save(cache)
            self._stale.discard(mode)

    # ===== 过期标记 =====

    def mark_stale(self, *modes: str) -> None:
        """结果缓存作废（磁盘变了、阈值变了、条目集合变了）。

        只在内存里标记，落盘由 `persist_stale()` 统一做。
        """
        self._stale.update(modes or self._modes)

    def clear_stale(self) -> None:
        self._stale.clear()

    def persist_stale(self) -> None:
        with self._write_lock:
            if not self._stale:
                return
            cache = self.load()
            changed = False
            for mode in self._stale:
                item = cache.get(mode)
                if isinstance(item, dict) and not item.get('stale'):
                    item['stale'] = True
                    cache[mode] = item
                    changed = True
            if changed:
                self.save(cache)
            self._stale.clear()

    # ===== 读取（含失效文件过滤） =====

    def get_cached_scan(self, mode: str) -> Dict:
        """返回上次扫描结果；没有缓存时返回空结果。

        `stale` = "这份结果算出来之后，磁盘（或阈值）又变过"：界面据此提示
        「点校验」，而不是把过期分组当成现状让人去删文件。返回前还会剔除
        已经不存在的文件：文件被移动/删除后，旧缓存里的路径会让缩略图请求
        变成 404；这类组同样标记 stale，但不改动缓存本身。
        """
        if mode not in self._modes:
            return {'groups': [], 'scanned': 0, 'cached': False, 'stale': False}
        cache = self.load()
        item = cache.get(mode)
        if not item or not isinstance(item, dict):
            return {'groups': [], 'scanned': 0, 'cached': False, 'stale': False}
        groups, dropped = self._drop_missing_cached_files(item.get('groups', []))
        return {
            'groups': groups,
            'scanned': item.get('scanned', 0),
            'cached': True,
            'stale': bool(item.get('stale')) or mode in self._stale or dropped,
            'saved_at': item.get('saved_at') or 0,
        }

    def _cached_file_exists(self, rel: str, dirs: Dict[str, Set[str]]) -> bool:
        """缓存里的相对路径当前是否还存在。

        结果缓存保存的是扫描当时的分组；用户移动 / 删除图片后，缓存仍会指向
        旧路径，打开面板时宿主 `/thumbs` 只能返回 404，整屏缩略图都会裂。
        这里按父目录一次 `scandir` 建立名字集合，避免为同一目录的每张图各做
        一次 `stat`；只读判断，不删除文件、不改缓存。
        """
        if not rel:
            return False
        path = Path(rel)
        if path.is_absolute() or '..' in path.parts:
            return False
        abs_path = self._host.abs_for_rel(rel)
        if not abs_path:
            return False
        source = Path(abs_path)
        parent = source.parent
        key = str(parent)
        names = dirs.get(key)
        if names is None:
            try:
                names = {
                    os.path.normcase(entry.name)
                    for entry in os.scandir(parent)
                    if entry.is_file()
                }
            except OSError:
                # 目录读不了（权限、超长路径等）时退回单文件判断，
                # 不要让整个分组被误判为不存在。
                try:
                    return source.is_file()
                except OSError:
                    return False
            dirs[key] = names
        return os.path.normcase(source.name) in names

    def _drop_missing_cached_files(self, groups: list) -> Tuple[list, bool]:
        """过滤掉结果缓存中已经不存在的文件；返回 (groups, 是否变化)。

        组内少于两张的组没有清理意义，一并丢弃。只读：缓存文件仍保留原样，
        下一次校验（统一刷新）会写入新的结果；这里通过 stale 告诉界面
        "缓存看到了磁盘变化"。
        """
        if not isinstance(groups, list):
            return [], True
        dirs: Dict[str, Set[str]] = {}
        cleaned: List[Any] = []
        changed = False
        for group in groups:
            if not isinstance(group, dict):
                changed = True
                continue
            files = group.get('files')
            if not isinstance(files, list):
                changed = True
                continue
            kept = [
                rel for rel in files
                if isinstance(rel, str) and self._cached_file_exists(rel, dirs)
            ]
            if len(kept) < 2:
                changed = True
                continue
            if len(kept) != len(files):
                group = dict(group)
                group['files'] = kept
                changed = True
            cleaned.append(group)
        return cleaned, changed
