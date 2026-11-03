"""书签的读写与增删。

书签是**唯一需要字级位置的状态**（阅读进度只记到章）：
`char` 是相对本章开头的字符偏移，坐标系是 `.chapter-content` 的 `textContent`；
`snippet` 是定位校验用的原文片段 —— 解析器换了输出、正文节点漂移时，按 char 直接
定位会静默落到隔壁段落，有 snippet 才能在章内重新搜回来。

状态自持：书签缓存在这里，落盘路径由入口注入（`cache_path` 闭包 + 文件名）。
"文档是否存在"也由入口注入，书签组件不持有书架。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


class Marks:
    """书签存储：`{文档 id: [{chapter, char, snippet, label, time}]}`，与进度文件并列。"""

    def __init__(self, cache_path: Callable[[str], Path], file_name: str,
                 document_exists: Callable[[str], bool], plugin_name: str) -> None:
        self._cache_path = cache_path
        self._file_name = file_name
        self._document_exists = document_exists
        self._plugin_name = plugin_name
        self._marks_cache: Optional[Dict[str, Any]] = None

    def load_marks(self) -> Dict[str, Any]:
        if self._marks_cache is not None:
            return self._marks_cache
        self._marks_cache = {}
        path = self._cache_path(self._file_name)
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    self._marks_cache = data
            except Exception:
                self._marks_cache = {}
        return self._marks_cache

    def save_marks(self, marks: Dict[str, Any]) -> None:
        self._marks_cache = marks
        try:
            with open(self._cache_path(self._file_name), 'w', encoding='utf-8') as handle:
                json.dump(marks, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def reset(self) -> None:
        """换根后丢弃内存里的书签（下次访问按新路径重读）。"""
        self._marks_cache = None

    def marks_list(self, document_id: str = '') -> Dict[str, Any]:
        """列出书签。给 id 就只列那本书的，不给就列全部（左栏「书签」用）。"""
        marks = self.load_marks()
        if document_id:
            return {'marks': marks.get(document_id) or []}
        return {'marks': [dict(item, document_id=doc_id)
                          for doc_id, items in marks.items() for item in (items or [])]}

    def marks_add(self, document_id: str, chapter: int, char: int = 0,
                  snippet: str = '', label: str = '') -> Dict[str, Any]:
        """加书签。同一位置（章 + 字）重复加不会产生第二条。"""
        if not self._document_exists(document_id):
            return {'success': False, 'error': '文档不存在'}
        try:
            chapter_index = max(0, int(chapter))
            char_offset = max(0, int(char))
        except (TypeError, ValueError):
            return {'success': False, 'error': '位置参数无效'}

        marks = self.load_marks()
        items: List[dict] = [item for item in (marks.get(document_id) or [])
                             if not (int(item.get('chapter', -1)) == chapter_index
                                     and int(item.get('char', -1)) == char_offset)]
        items.append({
            'chapter': chapter_index,
            'char': char_offset,
            'snippet': str(snippet or '')[:60],
            'label': str(label or '')[:40],
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        })
        items.sort(key=lambda item: (item['chapter'], item['char']))
        marks[document_id] = items
        self.save_marks(marks)
        return {'success': True, 'marks': items}

    def marks_remove(self, document_id: str, chapter: int, char: int = 0) -> Dict[str, Any]:
        marks = self.load_marks()
        kept = [item for item in (marks.get(document_id) or [])
                if not (int(item.get('chapter', -1)) == int(chapter)
                        and int(item.get('char', -1)) == int(char))]
        if kept:
            marks[document_id] = kept
        else:
            marks.pop(document_id, None)
        self.save_marks(marks)
        return {'success': True, 'marks': marks.get(document_id) or []}
