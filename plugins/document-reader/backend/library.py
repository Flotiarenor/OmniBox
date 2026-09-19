"""library 分片：扫描文档目录、维护书架索引与阅读进度。

从 `main.py` 切出来。这一块只关心"有哪些书、各自多大、读到第几章"，不碰格式解析
（那是 `documents.py`）也不碰朗读。

缓存的两个关键约定（都是踩过坑才写下来的）：

* **缓存条目就是返回给前端的那个 dict**，键名是 `file_size`。比较"文件是否变更"时
  必须读同一个键，否则比较恒为"变了" —— 每次列书架都全量重扫，章节数被清零，
  书架上永远显示 "?"。
* **章节数解析过一次就留着**，只在文件变更时清零。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)


class LibraryMixin:
    """书架：文件扫描、缓存、进度读写。"""

    CACHE_FILE = '.document_cache.json'
    PROGRESS_FILE = '.document_progress.json'
    # 章节缓存键加入格式与编码、文档 id 改为完整文件名（含扩展名）后升版：
    # 旧版的章节/偏移缓存与新的键对不上，留着只会变成读不到的垃圾。
    # v4：TxtParser 的编码兜底修好后，同一本书解出来的正文长度会变（GBK 文件过去被
    # 按 utf-8 + ignore 解成了乱码），旧偏移切片到新正文上就是错位的章节。
    CACHE_VERSION = 4

    # 子类（main.py 的插件类）提供：_cache_dir / _roots / KIND_BY_EXT / _document_cache
    #   / _chapter_cache / _offset_cache / _full_content_cache / _doc_cache
    #   / _progress_cache / _extract_dir()
    _cache_dir: str
    _roots: List[str]

    # ===== 缓存与进度文件 =====

    def _load_cache(self) -> None:
        cache_file = self._cache_path(self.CACHE_FILE)
        if not os.path.exists(cache_file):
            return
        try:
            with open(cache_file, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            # 迁移过来的旧缓存文件里这个键叫 novels：认一下，省掉一次全量重解析
            self._document_cache = data.get('documents') or data.get('novels') or {}
            if data.get('parser_version') == self.CACHE_VERSION:
                self._chapter_cache = data.get('chapters', {})
                self._offset_cache = data.get('offsets', {})
            else:
                # 旧版偏移格式不可靠，丢弃章节缓存并在下次访问时重解析
                self._chapter_cache = {}
                self._offset_cache = {}
        except Exception:
            pass

    def _save_cache(self) -> None:
        try:
            with open(self._cache_path(self.CACHE_FILE), 'w', encoding='utf-8') as handle:
                json.dump({
                    'parser_version': self.CACHE_VERSION,
                    'documents': self._document_cache,
                    'chapters': self._chapter_cache,
                    'offsets': self._offset_cache,
                }, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_progress(self) -> Dict[str, Any]:
        if self._progress_cache is not None:
            return self._progress_cache
        self._progress_cache = {}
        progress_file = self._cache_path(self.PROGRESS_FILE)
        if os.path.exists(progress_file):
            try:
                with open(progress_file, 'r', encoding='utf-8') as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    self._progress_cache = data
            except Exception:
                self._progress_cache = {}
        return self._progress_cache

    def _save_progress(self, progress: Dict[str, Any]) -> None:
        self._progress_cache = progress
        try:
            with open(self._cache_path(self.PROGRESS_FILE), 'w', encoding='utf-8') as handle:
                json.dump(progress, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _drop_book_cache(self, document_id: str) -> None:
        """文件被改动/删除后清掉这本书的全部缓存，含 EPUB 解包出来的图片。"""
        prefix = document_id + ':'
        for cache in (self._chapter_cache, self._offset_cache,
                      self._full_content_cache, self._doc_cache):
            for key in [k for k in cache if k == document_id or k.startswith(prefix)]:
                cache.pop(key, None)
        import shutil
        shutil.rmtree(self._extract_dir(document_id), ignore_errors=True)

    # ===== 扫描 =====

    def _kind_of(self, filename: str) -> Optional[str]:
        return self.KIND_BY_EXT.get(os.path.splitext(filename)[1].lower())

    def _iter_documents(self) -> Iterator[Tuple[str, str, str, str, str]]:
        """递归产出 (document_id, 绝对路径, 所属根, 相对根的 posix 路径, kind)。

        - id 就是"相对根目录的路径"：主目录第一层的文件 id 仍然是文件名，老的阅读进度
          与缓存键不受影响；子目录里的文件是 `子目录/书.epub`。
        - 跳过以 `.` 开头的目录与文件：自己的 `.document_state` 就在里面。
        - 多个根目录下出现相同相对路径时加序号区分（否则后一个会覆盖前一个）。
        """
        taken: set = set()
        for root in self._roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if not name.startswith('.')]
                for filename in filenames:
                    if filename.startswith('.'):
                        continue
                    kind = self._kind_of(filename)
                    if not kind:
                        continue
                    abs_path = os.path.join(dirpath, filename)
                    rel = Path(abs_path).relative_to(root).as_posix()
                    document_id, index = rel, 2
                    while document_id in taken:
                        document_id = f'{rel}#{index}'
                        index += 1
                    taken.add(document_id)
                    yield document_id, abs_path, root, rel, kind

    def list_documents(self) -> Dict[str, Any]:
        """获取文档列表，检查文件修改时间。"""
        current_files: Dict[str, dict] = {}
        for document_id, abs_path, _root, _rel, _kind in self._iter_documents():
            try:
                stat = os.stat(abs_path)
            except OSError as e:
                log.warning(f'[{self.name}] 读取文件信息失败 {abs_path}: {e}')
                continue
            current_files[document_id] = {'mtime': stat.st_mtime, 'size': stat.st_size}

        needs_update = False
        for name, info in current_files.items():
            cached = self._document_cache.get(name)
            # 这里读的是 `file_size`（缓存条目本身就是返回给前端的那个 dict），
            # 不是上面临时算的 `size` —— 键名不一致时比较恒为"变了"。
            if not cached or \
               cached.get('mtime') != info['mtime'] or \
               cached.get('file_size') != info['size']:
                needs_update = True
                self._drop_book_cache(name)
                if cached:
                    cached['chapter_count'] = 0

        cached_ids = set(self._document_cache.keys())
        current_ids = set(current_files)
        if cached_ids != current_ids:
            needs_update = True
            for name in cached_ids - current_ids:
                self._drop_book_cache(name)

        if not needs_update and self._document_cache:
            return {'documents': list(self._document_cache.values())}

        # 重新扫描（按 id 排序：多根目录、递归之后 os.walk 的顺序不稳定）
        documents = []
        for document_id, abs_path, root, rel, kind in self._iter_documents():
            document_info = self._scan_document_meta(document_id, abs_path, root, rel, kind)
            if document_info:
                documents.append(document_info)
        documents.sort(key=lambda item: item['id'].lower())

        self._document_cache = {item['id']: item for item in documents}
        self._save_cache()
        return {'documents': documents}

    def _scan_document_meta(self, document_id: str, abs_path: str, root: str,
                            rel: str, kind: str) -> Optional[dict]:
        """扫描文档元信息，记录修改时间和大小。"""
        try:
            name_without_ext = os.path.splitext(os.path.basename(abs_path))[0]
            if kind == 'txt':
                # "书名-作者.txt" 这条约定只对 txt 生效：别的格式里 '-' 往往是文件名
                # 本身的一部分（2024年度-报告.docx），拿它拆作者只会拆出假作者。
                parts = name_without_ext.split('-')
                title = parts[0].strip()
                author = parts[1].strip() if len(parts) > 1 else ''
            else:
                title = name_without_ext.strip()
                author = ''

            stat = os.stat(abs_path)
            progress = self._load_progress()
            # 进度键在 3.3.0 从"去扩展名的文件名"改成完整文件名（不同格式可能同名）。
            # 旧键仍要读得出来，否则升级后所有旧的阅读进度看起来被清零。
            document_progress = progress.get(document_id) or progress.get(name_without_ext) or {}
            cached = self._document_cache.get(document_id) or {}

            return {
                'id': document_id,
                'title': title,
                'author': author,
                'kind': kind,
                'dir': posixpath.dirname(rel),
                'root': root,
                'file_path': abs_path,
                'file_size': stat.st_size,
                'mtime': stat.st_mtime,
                'chapter_count': cached.get('chapter_count') or 0,
                'last_read_time': document_progress.get('last_read_time', ''),
                'last_read_chapter': document_progress.get('last_read_chapter', 0),
                'progress': document_progress.get('progress', 0.0),
                'scroll_position': document_progress.get('scroll_position', 0.0),
                'encoding': document_progress.get('encoding', 'auto'),
            }
        except Exception as e:
            log.error(f'扫描文档失败 {abs_path}: {e}')
            return None

    def update_progress(self, document_id: str, chapter_index: int,
                        scroll_position: float = 0.0,
                        encoding: str = 'auto') -> Dict[str, Any]:
        """更新阅读进度（只记到章，见 docs/document-reader-design.md §4）。"""
        progress = self._load_progress()

        kind = (self._document_cache.get(document_id) or {}).get('kind', 'txt')
        chapters = self._chapter_cache.get(f'{document_id}:{kind}:{encoding}', [])
        total_chapters = len(chapters)
        if not isinstance(chapter_index, int) or chapter_index < 0:
            chapter_index = 0
        if total_chapters > 0:
            chapter_index = min(chapter_index, total_chapters - 1)
        overall = (chapter_index + scroll_position) / total_chapters if total_chapters > 0 else 0.0

        import time as _time
        progress[document_id] = {
            'last_read_time': _time.strftime('%Y-%m-%d %H:%M:%S'),
            'last_read_chapter': chapter_index,
            'scroll_position': scroll_position,
            'progress': round(overall, 4),
            'encoding': encoding,
        }
        self._save_progress(progress)

        if document_id in self._document_cache:
            self._document_cache[document_id].update({
                'last_read_time': progress[document_id]['last_read_time'],
                'last_read_chapter': chapter_index,
                'progress': overall,
                'encoding': encoding,
            })
        return {'success': True}

    # ===== 内部工具 =====

    @staticmethod
    def _book_key(document_id: str, encoding: str = '') -> str:
        return f'{document_id}:{encoding}' if encoding else document_id

    @staticmethod
    def _hash_id(value: str) -> str:
        return hashlib.md5(value.encode('utf-8')).hexdigest()
