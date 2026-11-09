"""共享根路径与用量缓存：规范形式、嵌套归属、根目录指纹、目录键归属与用量读写。

为什么单独一个文件：`shares.py` 已经承担挂载 / 移除 / 刷新声明三件事，单文件行数贴到
后端上限；纯函数不持有状态、不碰存储，拆出来由入口装配一次注入。`UsageCache` 是
"某个绝对目录在某个时刻的用量"这一块内存状态的唯一持有者，同样由入口注入依赖。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


class SharePaths:
    """共享根路径的纯函数集合（无状态，入口装配一次注入）。"""

    @staticmethod
    def norm(path: str) -> str:
        """路径比较用的规范形式：绝对化 + 大小写折叠（Windows 上两者等价）。"""
        return os.path.normcase(os.path.abspath(path)) if path else ''

    @staticmethod
    def prefix(root_path: str) -> str:
        """根路径的"目录前缀"形式：保证以分隔符结尾，盘符根目录本身也适用。"""
        return root_path if root_path.endswith(os.sep) else root_path + os.sep

    def share_of(self, path: str, paths: Dict[str, str]) -> Optional[str]:
        """绝对路径落在哪个共享根下：嵌套共享时归最内层（最长匹配）。"""
        target = self.norm(path)
        if not target:
            return None
        best: Optional[str] = None
        best_len = -1
        for share_id, root in paths.items():
            root_path = self.norm(root)
            if not root_path:
                continue
            if target != root_path and not target.startswith(self.prefix(root_path)):
                continue
            if len(root_path) > best_len:
                best, best_len = share_id, len(root_path)
        return best

    @staticmethod
    def missing_usage() -> Dict[str, Any]:
        """用量还没算过（或共享根不可用）时的占位值。"""
        return {'used_bytes': None, 'entries': None, 'truncated': False,
                'usage_at': None, 'usage_stale': True}

    @staticmethod
    def prefix_owner(dir_key: str, owners: Dict[str, str]) -> Optional[str]:
        """虚拟目录键归属的共享标识：按根前缀最长匹配，首根的空前缀兜底。

        `owners` 是引擎当前的"根前缀 → 共享标识"表（第一根前缀为空串，其余由引擎
        按目录名生成）。目录键与条目键同形（`前缀/根内相对路径`），因此前缀匹配即可
        还原它属于哪个共享根，不需要把目录键反推成物理路径。
        """
        key = (dir_key or '').strip('/')
        matched: Optional[str] = None
        for prefix in owners:
            if not prefix:
                if matched is None:
                    matched = ''
                continue
            if (key == prefix or key.startswith(prefix + '/')) and (
                    matched is None or len(prefix) > len(matched)):
                matched = prefix
        return owners.get(matched) if matched is not None else None

    @staticmethod
    def affected_shares(new_dirs: List[str], vanished_dirs: List[str],
                        owners: Optional[Dict[str, str]]) -> Tuple[List[str], bool]:
        """目录集合变化归属的共享标识，以及"有目录无法归属"。

        返回 `(受影响的共享标识列表, 是否出现无法归属的目录)`：`owners` 为 None 或某个
        目录键不在任何根前缀下时，调用方应保守处理（保留旧数字只标待更新），而不是把
        没走到的根清成占位。
        """
        affected: List[str] = []
        unowned = False
        for dir_key in list(new_dirs) + list(vanished_dirs):
            share_id = SharePaths.prefix_owner(str(dir_key), owners) if owners else None
            if share_id is None:
                unowned = True
            elif share_id not in affected:
                affected.append(share_id)
        return affected, unowned

    @classmethod
    def root_owners(cls, roots: List[str], prefixes: List[str],
                    paths: Dict[str, str]) -> Optional[Dict[str, str]]:
        """引擎当前根 / 前缀 → 共享标识；任何一根对不上就返回 None。

        `roots` 与 `paths` 是同一份共享清单的两种形态（有序物理路径 / 标识 → 路径），
        逐根用规范化路径把标识对回去，前缀表因此能和根一一对应。
        """
        by_root = {cls.norm(root): share_id for share_id, root in paths.items()}
        owners: Dict[str, str] = {}
        for index, root in enumerate(roots):
            share_id = by_root.get(cls.norm(str(root)))
            if share_id is None:
                return None
            prefix = prefixes[index] if index < len(prefixes) else ''
            owners[prefix] = share_id
        return owners

    def usage_matches(self, cached: Any, state: Dict[str, Any]) -> bool:
        """缓存里的用量是否属于当前位置（共享根换过路径时整份作废）。"""
        if not isinstance(cached, dict):
            return False
        return self.norm(str(cached.get('path') or '')) == self.norm(str(state.get('path') or ''))

    def usage_stamp_matches(self, cached: Any, state: Dict[str, Any]) -> bool:
        """用量算出时记下的根指纹是否仍与磁盘一致；没有记录过一律算不一致。"""
        if not isinstance(cached, dict):
            return False
        recorded = cached.get('root_stamp')
        if not isinstance(recorded, list):
            return False
        current = self.root_stamp(Path(str(state.get('path') or '')).expanduser())
        return current is not None and current == recorded

    @staticmethod
    def dirs_change_count(report: Any) -> int:
        """报告里的目录集合变化数（含被截断的部分），`refresh_share_roots` 的 `changed`。"""
        if not isinstance(report, dict):
            return 0
        return (len(report.get('new_dirs') or [])
                + int(report.get('new_dirs_truncated') or 0)
                + len(report.get('vanished_dirs') or [])
                + int(report.get('vanished_dirs_truncated') or 0))

    def usage_cache(self, storage: Any) -> 'UsageCache':
        """构造一份用量缓存：本文件同时是"路径规则"与"用量状态"的落点。"""
        return UsageCache(self, storage)

    @staticmethod
    def root_stamp(path: Path) -> Optional[List[int]]:
        """共享根目录的廉价指纹：目录 mtime 与大小。

        只 stat 根目录本身（一次系统调用），不遍历：首屏只读缓存时用它判断"用量还是
        按这个目录算的吗"。目录里直接子项增删会改 mtime，因此这一类变化立刻可见；
        更深层子目录里的变化它看不见 —— 被动同步保持廉价（仍按本指纹判断），权威结论
        由整趟校验给出：校验趟无条件重算全部共享根用量。
        """
        try:
            st = path.stat()
        except OSError:
            return None
        return [int(st.st_mtime_ns), int(st.st_size)]


class UsageCache:
    """共享根用量缓存：同步按根指纹判断，校验强制重算，目录变化按根作废。

    条目形状：`{share_id: {path, used_bytes, truncated, entries, at, root_stamp}}`。
    `stale` 是"数字要保留、但已确认磁盘在它之后变过"的根集合：与"丢缓存"分开 ——
    丢缓存下次显示占位，`stale` 只给旧数字加「待更新」，不虚构一个数字。
    """

    def __init__(self, paths: SharePaths, storage: Any) -> None:
        self._paths = paths
        self._storage = storage
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.stale: Set[str] = set()

    def fields(self, share_id: str, state: Dict[str, Any], *,
               scan: bool, force: bool = False) -> Dict[str, Any]:
        """用量字段：`scan` 为真时缺的/过期的/`force` 的现算，为假时只读缓存。

        `usage_stale` 为真表示"这份数字不是按当前目录指纹算出来的，或者已知磁盘在它
        之后变过"（路径换过、根目录 mtime/大小变了、目录变化无法归属、校验被取消）。
        """
        if not state.get('available'):
            return self._paths.missing_usage()
        cached = self.cache.get(share_id)
        known_moved = share_id in self.stale
        if not scan:
            if not isinstance(cached, dict) or 'used_bytes' not in cached \
                    or not self._paths.usage_matches(cached, state):
                return self._paths.missing_usage()
            return self._summary(cached, known_moved, state)
        if force or known_moved or not self._paths.usage_matches(cached, state) \
                or not self._paths.usage_stamp_matches(cached, state):
            cached = self.scan(share_id, state)
            known_moved = share_id in self.stale
        if not isinstance(cached, dict) or 'used_bytes' not in cached:
            reason = str((cached if isinstance(cached, dict) else {}).get('reason') or '')
            return {**self._paths.missing_usage(), 'reason': reason}
        return self._summary(cached, known_moved, state)

    def _summary(self, cached: Dict[str, Any], known_moved: bool,
                 state: Dict[str, Any]) -> Dict[str, Any]:
        return {'used_bytes': int(cached['used_bytes']),
                'entries': int(cached.get('entries') or 0),
                'truncated': bool(cached.get('truncated')),
                'usage_at': cached.get('at'),
                'usage_stale': known_moved
                or not self._paths.usage_stamp_matches(cached, state)}

    def scan(self, share_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """算一个共享根的用量与条目数，写进缓存（同一次刷新同一目录只走一遍）。"""
        target = Path(str(state['path'])).expanduser()
        try:
            used, truncated = self._storage.tree_usage(target, limit=state.get('max_bytes'))
        except OSError as e:
            return {'available': False, 'reason': f'扫描失败: {e}'}
        usage = {
            'path': state['path'],
            'used_bytes': used,
            'truncated': truncated,
            'entries': self._storage.count_entries(target),
            'at': int(time.time()),
            # 这次用量是"按哪个目录算的"：根目录指纹变了，用量就该标成过期。
            'root_stamp': self._paths.root_stamp(target),
        }
        self.cache[share_id] = usage
        self.stale.discard(share_id)
        return usage

    def invalidate(self, share_ids: List[str]) -> int:
        """按共享标识丢缓存，返回真正丢掉的条数（目录集合变化落在其下的根）。"""
        cleared = 0
        for share_id in share_ids:
            if self.cache.pop(share_id, None) is not None:
                cleared += 1
        return cleared

    def drop(self, share_id: str) -> None:
        self.cache.pop(share_id, None)
        self.stale.discard(share_id)

    def mark_stale(self, share_ids, known: Dict[str, str]) -> int:
        """保留数字、标记"已知磁盘变过"的根；返回新增条数。"""
        added = 0
        for share_id in share_ids or ():
            if share_id in known and share_id not in self.stale:
                self.stale.add(share_id)
                added += 1
        return added

    def clear(self) -> None:
        self.cache.clear()
        self.stale.clear()
