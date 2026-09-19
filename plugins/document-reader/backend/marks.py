"""bookmarks 分片：书签的读写与增删。

从 `main.py` 切出来的，与 `documents.py`（格式解析）、`tts_engine.py`（朗读引擎）同一层：
按职责分片、由 `main.py` 用 `load_sibling` 加载成 mixin。

书签是**唯一需要字级位置的状态**（阅读进度只记到章）：
`char` 是相对本章开头的字符偏移，坐标系是 `.chapter-content` 的 `textContent`；
`snippet` 是定位校验用的原文片段 —— 解析器换了输出、正文节点漂移时，按 char 直接
定位会静默落到隔壁段落，有 snippet 才能在章内重新搜回来。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


class MarksMixin:
    """书签存储：`{文档 id: [{chapter, char, snippet, label, time}]}`，与进度文件并列。"""

    MARKS_FILE = '.document_marks.json'

    # 子类（main.py 的插件类）提供：_cache_path / _document_cache
    _marks_cache: Optional[Dict[str, Any]]

    def _load_marks(self) -> Dict[str, Any]:
        if self._marks_cache is not None:
            return self._marks_cache
        self._marks_cache = {}
        path = self._cache_path(self.MARKS_FILE)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    self._marks_cache = data
            except Exception:
                self._marks_cache = {}
        return self._marks_cache

    def _save_marks(self, marks: Dict[str, Any]) -> None:
        self._marks_cache = marks
        try:
            with open(self._cache_path(self.MARKS_FILE), 'w', encoding='utf-8') as handle:
                json.dump(marks, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def marks_list(self, document_id: str = '') -> Dict[str, Any]:
        """列出书签。给 id 就只列那本书的，不给就列全部（左栏「书签」用）。"""
        marks = self._load_marks()
        if document_id:
            return {'marks': marks.get(document_id) or []}
        return {'marks': [dict(item, document_id=doc_id)
                          for doc_id, items in marks.items() for item in (items or [])]}

    def marks_add(self, document_id: str, chapter: int, char: int = 0,
                  snippet: str = '', label: str = '') -> Dict[str, Any]:
        """加书签。同一位置（章 + 字）重复加不会产生第二条。"""
        if document_id not in self._document_cache:
            return {'success': False, 'error': '文档不存在'}
        try:
            chapter_index = max(0, int(chapter))
            char_offset = max(0, int(char))
        except (TypeError, ValueError):
            return {'success': False, 'error': '位置参数无效'}

        marks = self._load_marks()
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
        self._save_marks(marks)
        return {'success': True, 'marks': items}

    def marks_remove(self, document_id: str, chapter: int, char: int = 0) -> Dict[str, Any]:
        marks = self._load_marks()
        kept = [item for item in (marks.get(document_id) or [])
                if not (int(item.get('chapter', -1)) == int(chapter)
                        and int(item.get('char', -1)) == int(char))]
        if kept:
            marks[document_id] = kept
        else:
            marks.pop(document_id, None)
        self._save_marks(marks)
        return {'success': True, 'marks': marks.get(document_id) or []}
