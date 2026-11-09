"""正文读取：章节表、单章正文、全文、封面与"用系统程序打开"。

按格式分派：txt 走 `TxtParser` 的章节偏移切片；md / epub 走 `documents.open_document`
产出的统一文档对象（`.chapters()` / `.chapter_html(index)` / `.cover()`），HTML 由
白名单转换器产出。pdf 与其余格式不在阅读器内渲染，只交给系统默认程序。

状态自持：格式文档对象缓存（上限 3 本）与全文缓存都在这里；章节表、偏移表与书架
元信息归 `Library`（它是这些缓存与磁盘文件的唯一归属）。书被丢弃时 `Library` 通过
注入的回调调用 `drop_book_cache`，正文对象与全文不会留在内存里。

组件形态：本模块不 import 兄弟模块；`TxtParser`、`open_document`、EPUB 工厂、图片
URL 与系统打开函数全部由入口注入。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, cast

log = logging.getLogger(__name__)


class DocumentContent:
    """按格式读取章节与正文；缓存按文档 + 格式 + 编码隔离。"""

    def __init__(self, library: Any, roots: Any, parser: Any, docs: Any,
                 epub_cls: Any, markdown_cls: Any, image_url: Callable[[str], str],
                 open_with_system: Callable[[Path], None],
                 in_app_kinds: Any, plugin_name: str) -> None:
        self._library = library
        self._roots = roots
        self._parser = parser
        self._docs = docs
        self._epub_cls = epub_cls
        self._markdown_cls = markdown_cls
        self._image_url = image_url
        self._open_with_system = open_with_system
        self._in_app_kinds = in_app_kinds
        self._plugin_name = plugin_name
        self._doc_cache: Dict[str, Any] = {}
        self._full_content_cache: Dict[str, str] = {}

    # ===== 缓存管理 =====

    def drop_book_cache(self, document_id: str) -> None:
        """丢掉这本书的正文对象与全文缓存（书被改动/删除时由 Library 回调）。"""
        prefix = document_id + ':'
        for cache in (self._doc_cache, self._full_content_cache):
            for key in [k for k in cache if k == document_id or k.startswith(prefix)]:
                cache.pop(key, None)

    def clear_caches(self) -> None:
        """丢弃全部正文对象与全文缓存（刷新基建的"逃生门"）。"""
        self._doc_cache.clear()
        self._full_content_cache.clear()

    # ===== 文档对象 =====

    def _document(self, document_id: str, encoding: str = 'auto') -> Any:
        """构造并缓存格式文档对象（EPUB 的解包目录与图片 URL 在这里注入）。"""
        key = f"{document_id}:{encoding}"
        doc = self._doc_cache.get(key)
        if doc is not None:
            return doc

        document_info = self._library.info(document_id)
        if document_info is None:
            raise self._docs.DocError('文档不存在')
        doc = self._docs.open_document(
            document_info['file_path'],
            document_info['kind'],
            read_text=self._parser.TxtParser.read_full_content,
            epub_cls=self._epub_cls,
            markdown_cls=self._markdown_cls,
            encoding=encoding,
            root_dir=document_info.get('root') or str(self._roots.data_root()),
            extract_dir=str(self._roots.extract_dir(document_id)),
            image_url=self._image_url,
        )
        if len(self._doc_cache) >= 3:
            self._doc_cache.pop(next(iter(self._doc_cache)), None)
        self._doc_cache[key] = doc
        return doc

    # ===== 章节与正文 =====

    def get_chapters(self, document_id: str, encoding: str = 'auto') -> Dict[str, Any]:
        """获取章节目录（缓存按文档 + 格式 + 编码隔离）。"""
        document_info = self._library.info(document_id)
        if not document_info:
            return {'error': '文档不存在', 'chapters': []}

        kind = document_info['kind']
        if kind not in self._in_app_kinds:
            # pdf / 其他格式没有章节模型，前端据此直接进"系统程序打开"分支
            return {'chapters': []}

        key = f"{document_id}:{kind}:{encoding}"
        cached = self._library.chapters_for(key)
        if cached is not None:
            return {'chapters': cached}

        try:
            if kind == 'txt':
                chapters, offsets = self._parser.TxtParser.parse_txt(
                    document_info['file_path'], encoding)
            else:
                chapters, offsets = self._document(document_id, encoding).chapters(), None
            self._library.store_chapters(document_id, key, chapters, offsets)
            return {'chapters': chapters}
        except Exception as e:
            return {'error': f'解析章节失败: {e}', 'chapters': []}

    def document_cover(self, document_id: str) -> Dict[str, Any]:
        """取文档封面图 URL（只有 EPUB 可能有）。

        返回 `{'cover': ''}` 是正常结果，不是错误 —— 表示"这本书没有封面图"，
        前端据此画生成式封面（外框 + 书脊 + 四条按书名哈希上色的文字条）。
        封面由 `epub.py` 解包到 `.document_state/extract/<哈希>/cover.*`，
        而 `_image_url` 用的是绝对路径，多根目录下也不会 404。
        """
        document_info = self._library.info(document_id)
        if not document_info or document_info['kind'] != 'epub':
            return {'cover': ''}
        try:
            cover = self._document(document_id).cover()
        except Exception as e:
            log.warning(f'[{self._plugin_name}] 读取封面失败 {document_id}: {e}')
            return {'cover': ''}
        return {'cover': cover or ''}

    def get_full_content(self, document_id: str, encoding: str = 'auto') -> Dict[str, Any]:
        """获取纯文本文档完整内容（缓存按文档 + 编码隔离）。"""
        key = f"{document_id}:{encoding}"
        if key in self._full_content_cache:
            return {'content': self._full_content_cache[key]}

        document_info = self._library.info(document_id)
        if not document_info:
            return {'error': '文档不存在', 'content': ''}

        try:
            content = self._parser.TxtParser.read_full_content(
                document_info['file_path'], encoding)
            if key not in self._full_content_cache and len(self._full_content_cache) >= 3:
                for old_id in list(self._full_content_cache.keys()):
                    self._full_content_cache.pop(old_id, None)
                    break
            self._full_content_cache[key] = content
            return {'content': content}
        except Exception as e:
            return {'error': f'读取文档失败: {e}', 'content': ''}

    def get_content(self, document_id: str, chapter_index: int,
                    encoding: str = 'auto') -> Dict[str, Any]:
        """获取指定章节内容。

        返回的 `format` 决定前端怎么渲染：
          * `text` —— 纯文本，前端转义成段落（txt 的历史行为，保持不变）；
          * `html` —— 后端白名单转换器产出的片段，可直接插入 `.chapter-content`。
        """
        document_info = self._library.info(document_id)
        if not document_info:
            return {'error': '文档不存在', 'content': ''}

        kind = document_info['kind']
        if kind in ('md', 'epub'):
            try:
                html = self._document(document_id, encoding).chapter_html(chapter_index)
            except Exception as e:
                return {'error': f'读取章节失败: {e}', 'content': ''}
            return {'content': html, 'format': 'html'}
        if kind != 'txt':
            return {'error': '该格式不支持在阅读器内打开', 'content': ''}

        content_result = self.get_full_content(document_id, encoding)
        if content_result.get('error'):
            return content_result

        full_content = content_result['content']
        key = f"{document_id}:{kind}:{encoding}"

        offsets = self._library.offsets_for(key)
        if offsets is None:
            chapters_result = self.get_chapters(document_id, encoding)
            if chapters_result.get('error'):
                return {'error': chapters_result['error'], 'content': ''}
            offsets = self._library.offsets_for(key)

        if not offsets or not isinstance(chapter_index, int) or chapter_index < 0 \
                or chapter_index >= len(offsets):
            return {'error': '章节索引无效', 'content': ''}

        entry = offsets[chapter_index]
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            start_offset = int(entry[0])
            end_offset = int(entry[1])
        else:
            # 兼容旧版单偏移缓存
            legacy_entry = cast(Any, entry)
            start_offset = int(legacy_entry)
            end_offset = (int(offsets[chapter_index + 1])
                          if chapter_index + 1 < len(offsets) else len(full_content))

        chapter_content = full_content[start_offset:end_offset]
        return {'content': chapter_content, 'format': 'text'}

    # ===== 外部打开 =====

    def open_external(self, document_id: str) -> Dict[str, Any]:
        """用系统默认程序打开文档（阅读器内不渲染的格式：pdf/docx/mobi…）。"""
        document_info = self._library.info(document_id)
        if not document_info:
            return {'error': '文档不存在'}
        try:
            path = Path(document_info['file_path']).resolve()
            roots = [Path(root).resolve() for root in self._roots.roots]
        except (OSError, ValueError) as e:
            return {'error': f'路径无效: {e}'}

        # 这里是"启动外部程序"的口子：路径只认配置目录之内的文件。否则任何能调 API 的
        # 同源脚本都能借它拉起别处的东西（例如自己写个 bat 再打开）。
        if not any(path.is_relative_to(root) for root in roots):
            log.warning(f'[{self._plugin_name}] 拒绝越界的外部打开请求: {path}')
            return {'error': '路径越界'}
        if not path.is_file():
            return {'error': '文件不存在'}

        try:
            self._open_with_system(path)
        except Exception as e:
            return {'error': f'调用系统程序失败: {e}'}
        return {'success': True}
