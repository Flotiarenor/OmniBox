"""书架：目录扫描、书架索引、章节缓存与阅读进度。

这一块只关心"有哪些书、各自多大、读到第几章"，不碰格式解析（那是 documents / epub /
parser 的事）也不碰朗读。

缓存的两个关键约定（都是踩过坑才写下来的）：

* **缓存条目就是返回给前端的那个 dict**，键名是 `file_size`。比较"文件是否变更"时
  必须读同一个键，否则比较恒为"变了" —— 每次列书架都全量重扫，章节数被清零，
  书架上永远显示 "?"。
* **章节数解析过一次就留着**，只在文件变更时清零。

状态自持：书架缓存、章节/偏移缓存、进度缓存与脏标记都在这里。正文对象与全文缓存
属于 `DocumentContent`，本书被丢弃时由 `drop_book` 通过注入的回调一起清掉。
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)


class Library:
    """书架：文件扫描、缓存、进度读写。"""

    def __init__(self, roots: Any, names: Any, kind_by_ext: Dict[str, str],
                 plugin_name: str) -> None:
        self._roots = roots
        self._names = names
        self._kind_by_ext = kind_by_ext
        self._plugin_name = plugin_name

        self._document_cache: Dict[str, Dict[str, Any]] = {}
        self._chapter_cache: Dict[str, List[dict]] = {}
        self._offset_cache: Dict[str, List[Any]] = {}
        self._progress_cache: Optional[Dict[str, Any]] = None
        self._cache_dirty = False        # 缓存有未落盘的改动（整趟结束/访问后写一次）
        # 正文对象与全文缓存归 DocumentContent：本书被丢弃时由它清自己的那份。
        # 装配顺序是 library → content，所以入口在构造完 content 后注入。
        self._derived_handler: Optional[Callable[[str], None]] = None
        self.load_cache()

    def set_derived_handler(self, handler: Callable[[str], None]) -> None:
        """注入"丢弃某本书的正文对象与全文缓存"的回调（由入口装配时调用）。"""
        self._derived_handler = handler

    # ===== 缓存与进度文件 =====

    def reset_for_new_root(self) -> None:
        """换根：全部按"根内相对路径"为键的缓存整体作废，再按新状态目录重读。"""
        self._document_cache = {}
        self._chapter_cache = {}
        self._offset_cache = {}
        self._progress_cache = None
        self._cache_dirty = False
        self.load_cache()

    def load_cache(self) -> None:
        cache_file = self._roots.cache_path(self._names.cache_file)
        if not cache_file.exists():
            return
        try:
            with open(cache_file, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            # 迁移过来的旧缓存文件里这个键叫 novels：认一下，省掉一次全量重解析
            self._document_cache = data.get('documents') or data.get('novels') or {}
            if data.get('parser_version') == self._names.cache_version:
                self._chapter_cache = data.get('chapters', {})
                self._offset_cache = data.get('offsets', {})
            else:
                # 旧版偏移格式不可靠，丢弃章节缓存并在下次访问时重解析
                self._chapter_cache = {}
                self._offset_cache = {}
        except Exception:
            pass

    def save_cache(self) -> None:
        try:
            with open(self._roots.cache_path(self._names.cache_file), 'w', encoding='utf-8') as handle:
                json.dump({
                    'parser_version': self._names.cache_version,
                    'documents': self._document_cache,
                    'chapters': self._chapter_cache,
                    'offsets': self._offset_cache,
                }, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def load_progress(self) -> Dict[str, Any]:
        if self._progress_cache is not None:
            return self._progress_cache
        self._progress_cache = {}
        progress_file = self._roots.cache_path(self._names.progress_file)
        if progress_file.exists():
            try:
                with open(progress_file, 'r', encoding='utf-8') as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    self._progress_cache = data
            except Exception:
                self._progress_cache = {}
        return self._progress_cache

    def save_progress(self, progress: Dict[str, Any]) -> None:
        self._progress_cache = progress
        try:
            with open(self._roots.cache_path(self._names.progress_file), 'w',
                      encoding='utf-8') as handle:
                json.dump(progress, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ===== 书架条目 =====

    def info(self, document_id: str) -> Optional[Dict[str, Any]]:
        """一本书的书架条目（不存在时返回 None）。"""
        return self._document_cache.get(document_id)

    def entries(self) -> List[Dict[str, Any]]:
        return list(self._document_cache.values())

    def document_ids(self) -> List[str]:
        return list(self._document_cache.keys())

    def book_count(self) -> int:
        return len(self._document_cache)

    def reset_chapter_count(self, document_id: str) -> None:
        cached = self._document_cache.get(document_id)
        if cached:
            cached['chapter_count'] = 0

    def remove_document(self, document_id: str) -> bool:
        """从书架移除一本（返回是否真的移除了）。"""
        return self._document_cache.pop(document_id, None) is not None

    def drop_book(self, document_id: str) -> None:
        """清掉这本书的全部缓存：章节/偏移、正文对象、全文，含 EPUB 解包出来的图片。"""
        prefix = document_id + ':'
        for cache in (self._chapter_cache, self._offset_cache):
            for key in [k for k in cache if k == document_id or k.startswith(prefix)]:
                cache.pop(key, None)
        if self._derived_handler is not None:
            self._derived_handler(document_id)
        shutil.rmtree(self._roots.extract_dir(document_id), ignore_errors=True)

    def clear_derived(self) -> None:
        """丢弃全部派生缓存（章节表 / 偏移 / 书架元信息）。"""
        self._chapter_cache.clear()
        self._offset_cache.clear()
        self._document_cache.clear()
        self._cache_dirty = True

    # ===== 章节缓存 =====

    def chapters_for(self, key: str) -> Optional[List[dict]]:
        """已解析的章节表；没解析过返回 None（与"解析出来就是空"区分开）。"""
        return self._chapter_cache.get(key)

    def offsets_for(self, key: str) -> Optional[List[Any]]:
        return self._offset_cache.get(key)

    def store_chapters(self, document_id: str, key: str, chapters: List[dict],
                       offsets: Optional[List[Any]]) -> None:
        """记下章节表与偏移表，并把章节数回写书架条目。

        章节数必须**先**写回 `_document_cache` 再落盘：反过来的话磁盘上永远是旧值
        （0 或上一次的），重启后 `list_documents` 命中早返回分支就再也修不回来 ——
        书架那一行"N 章"会退化成文件大小。
        """
        self._chapter_cache[key] = chapters
        if offsets is not None:
            self._offset_cache[key] = offsets
        cached = self._document_cache.get(document_id)
        if cached is not None:
            cached['chapter_count'] = len(chapters)
        self.save_cache()

    # ===== 扫描 =====

    def kind_of(self, filename: str) -> Optional[str]:
        return self._kind_by_ext.get(os.path.splitext(filename)[1].lower())

    def iter_documents(self) -> Iterator[Tuple[str, str, str, str, str]]:
        """递归产出 (document_id, 绝对路径, 所属根, 相对根的 posix 路径, kind)。

        - id 就是"相对根目录的路径"：主目录第一层的文件 id 仍然是文件名，老的阅读进度
          与缓存键不受影响；子目录里的文件是 `子目录/书.epub`。
        - 跳过以 `.` 开头的目录与文件：自己的 `.document_state` 就在里面。
        - 多个根目录下出现相同相对路径时加序号区分（否则后一个会覆盖前一个）。
        """
        taken: set = set()
        for root in self._roots.roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [name for name in dirnames if not name.startswith('.')]
                for filename in filenames:
                    if filename.startswith('.'):
                        continue
                    kind = self.kind_of(filename)
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
        for document_id, abs_path, _root, _rel, _kind in self.iter_documents():
            try:
                stat = os.stat(abs_path)
            except OSError as e:
                log.warning(f'[{self._plugin_name}] 读取文件信息失败 {abs_path}: {e}')
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
                self.drop_book(name)
                if cached:
                    cached['chapter_count'] = 0

        cached_ids = set(self._document_cache.keys())
        current_ids = set(current_files)
        if cached_ids != current_ids:
            needs_update = True
            for name in cached_ids - current_ids:
                self.drop_book(name)

        if not needs_update and self._document_cache:
            return {'documents': list(self._document_cache.values())}

        # 重新扫描（按 id 排序：多根目录、递归之后 os.walk 的顺序不稳定）
        documents = []
        for document_id, abs_path, root, rel, kind in self.iter_documents():
            document_info = self.scan_document_meta(document_id, abs_path, root, rel, kind)
            if document_info:
                documents.append(document_info)
        documents.sort(key=lambda item: item['id'].lower())

        self._document_cache = {item['id']: item for item in documents}
        self.save_cache()
        return {'documents': documents}

    def scan_document_meta(self, document_id: str, abs_path: str, root: str,
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
            progress = self.load_progress()
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
        """更新阅读进度：章号 + 章内位置（0~1），前端 `openDocument` 按它还原。"""
        progress = self.load_progress()

        kind = (self._document_cache.get(document_id) or {}).get('kind', 'txt')
        chapters = self._chapter_cache.get(f'{document_id}:{kind}:{encoding}', [])
        total_chapters = len(chapters)
        if not isinstance(chapter_index, int) or chapter_index < 0:
            chapter_index = 0
        if total_chapters > 0:
            chapter_index = min(chapter_index, total_chapters - 1)
        overall = (chapter_index + scroll_position) / total_chapters if total_chapters > 0 else 0.0

        progress[document_id] = {
            'last_read_time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'last_read_chapter': chapter_index,
            'scroll_position': scroll_position,
            'progress': round(overall, 4),
            'encoding': encoding,
        }
        self.save_progress(progress)

        if document_id in self._document_cache:
            self._document_cache[document_id].update({
                'last_read_time': progress[document_id]['last_read_time'],
                'last_read_chapter': chapter_index,
                # `scroll_position` 必须一起回写：文件没变时 `list_documents` 走早返回
                # 分支、只认这份缓存，漏掉它列表里的章内位置就永远是 0（进度文件里
                # 明明存着），前端拿到的记录等于没有。
                'scroll_position': scroll_position,
                'progress': overall,
                'encoding': encoding,
            })
            # 书架条目（含进度与"最近阅读"排序）也要落盘：只写 `.document_progress.json`
            # 的话，重启后 `list_documents` 会命中早返回分支，把旧进度当现状返回，
            # 直到文件本身变化触发一次全量重扫为止。
            self.save_cache()
        return {'success': True}

    # ===== 脏标记（整趟改写后落盘一次） =====

    def mark_dirty(self) -> None:
        self._cache_dirty = True

    def is_dirty(self) -> bool:
        return self._cache_dirty

    def save_if_dirty(self) -> None:
        if self._cache_dirty:
            self.save_cache()
            self._cache_dirty = False

    # ===== 统一刷新基线的辅助 =====

    def document_key_for(self, abs_path: str) -> str:
        """条目键 = 相对根的 posix 路径（与缓存键、进度键同形）。

        多根目录下相对路径撞车时，`iter_documents` 会给第二个加 `#2` 后缀（按遍历
        先后编号）。指纹库里不复制这套编号 —— 编号依赖遍历顺序，两套遍历不可能保证
        同序。代价是撞车的那一对共用一个指纹键：基建只驱动其中一份的缓存失效，
        另一份仍由插件自己的 mtime/size 判定兜住。
        """
        target = Path(abs_path)
        for root in self._roots.roots:
            try:
                return target.relative_to(root).as_posix()
            except ValueError:
                continue
        return target.name

    @staticmethod
    def base_key(key: str) -> str:
        """去掉 `iter_documents` 给同相对路径加的 `#N` 后缀。"""
        head, sep, tail = key.rpartition('#')
        return head if sep and tail.isdigit() else key
