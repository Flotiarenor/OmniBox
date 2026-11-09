"""目录级扫描缓存与递归聚合（相册索引的条目存储）。

为什么单独一个组件：相册索引里存的是"每个目录一个条目"，而条目的生成有两条路径 ——
带 mtime 校验的扫描（列目录时用）与自底向上的递归聚合（建全集索引时用）。这两条路径
写的是**同一组字段**（`total_mtime` / `total_count` / `total_cover` /
`total_cover_mtime`），因此互为缓存：列一次目录不必再走整棵子树，反过来建索引也不
重扫已按需算过的目录。

把这份条目存储与"索引什么时候重建、TTL 多长、缓存版本是几"分开之后，索引组件的职责
就只剩生命周期，而这里只剩"一个目录的条目怎么算、怎么复用"。

状态自持：条目字典（虚拟路径 → 条目）与扫描未命中计数都在这里。
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


class DirEntryCache:
    """虚拟路径 → 目录条目（直接图片、封面、子目录、递归聚合结果）。"""

    def __init__(self, paths: Any, scanner: Any, settings: Any, naming: Any) -> None:
        self._paths = paths
        self._scanner = scanner
        self._settings = settings
        self._naming = naming
        self._dirs: Dict[str, Dict] = {}
        self._misses = 0

    # ===== 存储 =====

    @property
    def entries(self) -> Dict[str, Dict]:
        """当前全部条目（索引组件负责落盘，本组件只持有）。"""
        return self._dirs

    def replace(self, entries: Dict[str, Dict]) -> None:
        """整份替换（索引重建、启动时读回落盘索引）。"""
        self._dirs = entries

    def clear(self) -> None:
        """丢弃全部条目。"""
        self._dirs = {}

    @property
    def misses(self) -> int:
        """诊断用：上一次重建真正重扫了多少个目录。"""
        return self._misses

    def reset_misses(self) -> None:
        """重置重扫计数（重建开始前调用）。"""
        self._misses = 0

    def drop_nodes(self, items) -> None:
        """丢掉这些条目所属目录与其**所有祖先**的条目。

        祖先必须一起丢：`image_count` / 封面是自底向上聚合的，只丢子目录会让父目录
        留着旧的计数与封面。合成根（`''`）聚合整棵树，任何变化都要丢。
        """
        self._dirs.pop('', None)
        for item in items:
            rel = str(item.get('dir') or '').strip('/')
            while rel:
                self._dirs.pop(rel, None)
                rel = rel.rpartition('/')[0]

    # ===== 扫描（带 mtime 校验） =====

    def scan(self, rel_path: str, dir_path: Path, mtime: float) -> Dict:
        """带 mtime 校验的目录直扫（进程内缓存，键 = 虚拟路径）。

        与裸扫描的唯一区别是"mtime 没变就复用上次结果"。这个组件与索引共用同一份
        条目，因此索引重建时也顺带复用；反过来列目录写下的条目，重建时直接命中。

        mtime 精确比较：删除/移动后目录 mtime 只前进几毫秒，"相差小于半秒"的容差会
        把刚变过的目录当成没变，复用旧计数与旧封面条目。
        """
        pixiv = self._settings.pixiv_mode(rel_path)
        cached = self._dirs.get(rel_path)
        if cached and self._same_mtime(cached.get('mtime'), mtime) \
                and cached.get('pixiv') == pixiv:
            return cached
        entry = self._scanner.scan(dir_path, rel_path, pixiv)
        entry['pixiv'] = pixiv
        entry['mtime'] = mtime
        self._dirs[rel_path] = entry
        self._misses += 1
        return entry

    @staticmethod
    def _same_mtime(cached_mtime: Any, mtime: float) -> bool:
        """两次 `stat` 的目录 mtime 是否相同（不可比时视为不同，要求重扫）。"""
        try:
            return float(cached_mtime) == float(mtime)
        except (TypeError, ValueError):
            return False

    # ===== 递归聚合 =====

    def children_total(self, rel_path: str, dir_path: Path, mtime: float,
                       entry: Dict) -> Tuple[int, str, float]:
        """子目录聚合计数、代表封面与封面 mtime，复用缓存条目，重复调用不重扫磁盘。

        返回 (递归图片总数, 代表封面, 封面 mtime)。命中条件是该目录自身的 mtime 与
        上次算聚合时完全一致（`total_mtime`）—— 缓存命中时只 stat 一层子目录，不再
        走整棵子树。
        """
        if entry.get('total_count') is not None and entry.get('total_mtime') == mtime:
            return (entry.get('total_count', 0), entry.get('total_cover', '') or '',
                    float(entry.get('total_cover_mtime') or 0.0))

        children: List[str] = []
        try:
            with os.scandir(dir_path) as entries:
                for item in entries:
                    if item.name.startswith('.') or item.name == '.cache' or not item.is_dir():
                        continue
                    children.append(item.name)
        except OSError:
            children = list(entry.get('children') or [])

        sub_totals: Dict[str, Tuple[int, str, float, float]] = {}
        for name in children:
            sub_rel = f'{rel_path}/{name}' if rel_path else name
            sub_path = dir_path / name
            try:
                sub_mtime = sub_path.stat().st_mtime
            except OSError:
                continue
            sub_entry = self.scan(sub_rel, sub_path, sub_mtime)
            count = sub_entry.get('direct_count', 0)
            cover = sub_entry.get('direct_cover', '')
            cover_mtime = float(sub_entry.get('direct_cover_mtime') or 0.0)
            if not count:
                # 纯容器子目录：它自己的聚合缓存未失效时直接用，否则递归重算
                count, cover, cover_mtime = self.children_total(
                    sub_rel, sub_path, sub_mtime, sub_entry)
            sub_totals[name] = (count, cover, sub_mtime, cover_mtime)

        named = self._naming.sort([(name, sub_totals[name][1]) for name in sub_totals],
                                  lambda item: item[0], reverse=True)
        total = sum(sub_totals[name][0] for name in sub_totals)
        cover = next((cover for _name, cover in named if cover), '')
        cover_mtime = next((sub_totals[name][3] for name, sub_cover in named
                            if sub_cover), 0.0)
        entry['total_mtime'] = mtime
        entry['total_count'] = total
        entry['total_cover'] = cover
        entry['total_cover_mtime'] = cover_mtime
        return total, cover, cover_mtime

    def aggregate(self, dir_path: Path, rel_path: str) -> Tuple[int, str, float]:
        """递归统计目录下的图片总数、代表封面与封面 mtime（含更深层的子目录）。"""
        try:
            mtime = dir_path.stat().st_mtime
        except OSError:
            return 0, '', 0.0
        entry = self.scan(rel_path, dir_path, mtime)
        return self.children_total(rel_path, dir_path, mtime, entry)

    def cached_total(self, rel_path: str) -> Optional[Tuple[int, str, float]]:
        """索引里本目录的聚合结果（总数, 封面, 封面 mtime）；未算过或已失效返回 None。

        只查缓存、不碰磁盘：调用方用它决定"要不要当场展开子树"，因此这里绝不能触发
        扫描，否则等于把要避免的开销提前付掉。
        """
        entry = self._dirs.get(rel_path)
        if not isinstance(entry, dict):
            return None
        total = entry.get('total_count')
        cached_mtime = entry.get('total_mtime')
        scanned_mtime = entry.get('mtime')
        if total is None or cached_mtime is None or scanned_mtime is None:
            return None
        if not self._same_mtime(cached_mtime, scanned_mtime):
            return None
        return (total, entry.get('total_cover', '') or '',
                float(entry.get('total_cover_mtime') or 0.0))
