"""接入壳的统一刷新基建（同步 / 校验）。

壳负责去抖、单飞、增量短路、幽灵清理、进度与状态展示，这里只回答三个问题 —— 管哪些
文件、怎么算指纹、哪些条目需要重活。

本插件的失效判据（逐文件 mtime + size + id 集合）本来就是全仓最合理的一份，接入基建
换来的是：目录级短路（不再每次进书架都全树 stat 一遍）、统一的手动校验入口、以及解析
规则版本的整体失效（`content_version` = 缓存版本，取代"记得手动丢缓存"）。

状态自持：无（书架缓存在 Library，正文缓存在 DocumentContent）；组件只编排"丢哪一份"。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)


class FreshnessProvider:
    """向壳声明新鲜度规格，并实现它的钩子。"""

    def __init__(self, roots: Any, library: Any, content: Any,
                 kind_by_ext: Dict[str, str], cache_version: int) -> None:
        self._roots = roots
        self._library = library
        self._content = content
        self._kind_by_ext = kind_by_ext
        self._cache_version = cache_version

    # ===== 规格 =====

    def spec(self) -> Dict[str, Any]:
        return {
            'roots': lambda: [Path(root) for root in self._roots.roots],
            'include': tuple(sorted(self._kind_by_ext)),
            'key_of': lambda abs_path: self._library.document_key_for(abs_path),
            'derive': self.derive,
            'prune': self.prune,
            'on_verified': self.audit,
            'on_pass_end': self.pass_end,
            'rebuild': self.rebuild,
            'content_version': self._cache_version,
            'unit': '本',
            'min_sync_interval': 5.0,
            # 文档是几十到几百本量级：逐条 stat 换"文件被替换立刻反映"很划算。
            # 上千文件量级的插件应保持默认 False —— 实测 6000 张图库的整树同步
            # 从 0.11s 涨到 0.29s（真实磁盘冷缓存下差距更大），见 image-viewer。
            'stat_entries': True,
        }

    # ===== 钩子 =====

    def derive(self, items: Any) -> Dict[str, Any]:
        """新增/指纹变化：丢掉这本书的派生缓存，让它按需重解析。

        文档的派生数据（章节表、偏移、全文、EPUB 解包图）都是按需重算的，所以
        "重活"就是"丢旧结果"；`_document_cache` 里的元信息由下一趟
        `list_documents` 重建（它按 mtime/size 判定，不需要在这里重解析）。
        """
        dropped = 0
        for it in items:
            key = str(it.get('key') or '')
            if not key:
                continue
            if self._library.info(key) is not None:
                self._library.reset_chapter_count(key)   # 章节表已作废：书架暂时按体积显示
            self._library.drop_book(key)
            dropped += 1
        if dropped:
            self._library.mark_dirty()
        return {'count': dropped}

    def prune(self, keys: Any) -> int:
        """条目消失：移出书架并清掉它的全部缓存。"""
        dropped = 0
        for raw_key in keys:
            key = str(raw_key)
            if self._library.remove_document(key):
                dropped += 1
            self._library.drop_book(key)
            progress = self._library.load_progress()
            if isinstance(progress, dict):
                progress.pop(key, None)
        if dropped:
            self._library.mark_dirty()
            self._library.save_progress(self._library.load_progress())
        return dropped

    def audit(self, keys: Any) -> Dict[str, Any]:
        """全量校验后的对账：书架条目与磁盘一致（幽灵条目与孤儿缓存一次清干净）。

        `#N` 变体要保留：同相对路径出现在多个根时，插件按遍历顺序给第二份加
        `#2`，而指纹键只有"相对路径"这一个（见 `Library.document_key_for`）。只按
        `key in valid` 判断会把第二份当成幽灵，每跑一次校验就删一次。
        """
        valid = {str(key) for key in keys}
        stale = sorted(
            key for key in self._library.document_ids()
            if key not in valid and self._library.base_key(key) not in valid
        )
        for key in stale:
            self._library.remove_document(key)
            self._library.drop_book(key)
        if stale:
            self._library.mark_dirty()
        return {'dropped_books': len(stale), 'valid_books': len(valid)}

    def pass_end(self, report: Any, verified: bool) -> Dict[str, Any]:
        """整趟结束：缓存与进度有改动才落盘（缓存是单文件，不能每目录重写）。"""
        if self._library.is_dirty():
            self._library.save_if_dirty()
        return {'books': self._library.book_count(), 'verified': bool(verified)}

    def rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃派生缓存（章节表/偏移/全文/解包图 + 书架元信息）。

        **不动**阅读进度与书签（`.document_progress.json` / 书签文件）。
        """
        self._library.clear_derived()
        self._content.clear_caches()
        return {'kind': kind, 'cleared': True}
