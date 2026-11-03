"""统一刷新基建的规格声明与钩子。

书架卡片 = 顶层漫画目录的派生数据（含页数、封面、"里面有图片吗"的两次判断）。
旧实现没有可用缓存，`manga_get_state` + `manga_list` 各扫一遍全树，切一次视图
就是两遍。接入基建后：目录级短路判定"没变"时连 `derive` 都不会被调用，
列表只在磁盘真变时重建。

本组件只做声明与委派：扫描与缓存仍归 `Library`，这里不持任何状态。
"""

from typing import Any, Dict, List


class ShelfFreshness:
    """对外声明书架怎么同步、怎么校验；钩子一律转给 `Library`。"""

    def __init__(self, paths: Any, library: Any,
                 image_exts: Any, content_version: int) -> None:
        self._paths = paths
        self._library = library
        self._image_exts = image_exts
        self._content_version = content_version

    def spec(self) -> Dict[str, Any]:
        return {
            'roots': lambda: [self._paths.root],
            'include': tuple(sorted(self._image_exts)),
            'derive': self.derive,
            'prune': self.prune,
            'on_verified': self.audit,
            'rebuild': self.rebuild,
            # 扫描规则（收录哪些目录、封面怎么挑）变更时改它：整体作废书架缓存
            'content_version': self._content_version,
            'unit': '部',
            'min_sync_interval': 5.0,
            # 与 image-viewer 同一取舍：逐条 stat 会把"进一次书架"变成"给整库每页
            # stat 一次"（实测 6000 张的整树同步从 0.11s 涨到 0.29s，真实磁盘冷缓存
            # 下差距更大）。原地替换单页由**校验**负责（audit 逐项比对指纹），
            # 目录 mtime + 纳入索引的名字个数足以发现新增/删除/改名。
            'stat_entries': False,
        }

    def derive(self, items) -> Dict[str, Any]:
        """条目新增/指纹变化：作废受影响漫画的书架卡片。"""
        folders = {str(item.get('key') or '').split('/', 1)[0] for item in items}
        folders.discard('')
        if not folders:
            return {'count': 0}
        self._library.reset()
        return {'count': len(items), 'folders': len(folders)}

    def prune(self, keys) -> int:
        """条目消失：整本漫画的图片都没了（或删了几页）→ 书架重建。"""
        if not keys:
            return 0
        self._library.reset()
        return len(keys)

    def audit(self, keys) -> Dict[str, Any]:
        """全量校验后的对账：书架只保留磁盘上还有图片的漫画。"""
        valid_folders = {str(key).split('/', 1)[0] for key in keys}
        stale = self._library.retain(valid_folders)
        return {'dropped_books': len(stale), 'valid_folders': len(valid_folders)}

    def rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃书架缓存并重建（不动收藏与最近阅读）。"""
        self._library.reset()
        shelf: List[Dict] = self._library.list_manga()
        return {'kind': kind, 'books': len(shelf)}
