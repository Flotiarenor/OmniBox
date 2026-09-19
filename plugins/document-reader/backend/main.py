import hashlib
import json
import logging
import os
import posixpath
import shutil
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)


_parser_mod = load_sibling(__file__, 'parser', 'document_reader')
TxtParser = _parser_mod.TxtParser

_docs_mod = load_sibling(__file__, 'documents', 'document_reader')
open_document = _docs_mod.open_document

class DocumentReaderPlugin(PluginBase):
    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        {"key": "root_dir", "label": "文档根目录", "type": "directory", "multi": True,
         "placeholder": "输入目录绝对路径，如 D:\\文档",
         "emptyText": "未添加任何目录，将使用默认数据目录（./data）",
         "help": "可添加多个目录（每行一个，第一行为主目录，缓存与阅读进度存在它下面）；"
                 "每个目录都会递归扫描子目录"},
    ]

    CACHE_FILE = '.document_cache.json'
    PROGRESS_FILE = '.document_progress.json'
    # 章节缓存键加入格式与编码、文档 id 改为完整文件名（含扩展名）后升版：
    # 旧版的章节/偏移缓存与新的键对不上，留着只会变成读不到的垃圾。
    CACHE_VERSION = 3

    CACHE_DIR_NAME = '.document_state'
    # 改名前的名字：只用于把旧数据搬过来，别拿它们当新写入口
    LEGACY_CACHE_DIR = '.novel_state'
    LEGACY_CACHE_FILE = '.novel_cache.json'
    LEGACY_PROGRESS_FILE = '.novel_progress.json'

    # 扩展名 → 处理方式。
    #   txt      原有偏移切片（前端转义成段落，行为逐字节不变）
    #   md/epub  documents.py 转成白名单 HTML
    #   pdf      阅读器内嵌 iframe，交给 WebView 自带的阅览器
    #   external 列出来，但只能交给系统默认程序打开
    KIND_BY_EXT: ClassVar[Dict[str, str]] = {
        '.txt': 'txt',
        '.md': 'md',
        '.markdown': 'md',
        '.epub': 'epub',
        '.pdf': 'pdf',
        '.docx': 'external',
        '.doc': 'external',
        '.odt': 'external',
        '.rtf': 'external',
        '.mobi': 'external',
        '.azw3': 'external',
        '.fb2': 'external',
        '.chm': 'external',
    }
    # 有章节模型的格式：前端按"目录 + 章节"阅读；其余的走专门分支
    IN_APP_KINDS: ClassVar[Tuple[str, ...]] = ('txt', 'md', 'epub')

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self._roots: List[str] = []
        self.document_dir = ''
        self._cache_dir = ''
        self._set_roots(self._parse_roots(self.setting('root_dir')))

        self._document_cache: Dict[str, dict] = {}
        self._chapter_cache: Dict[str, List[dict]] = {}
        self._offset_cache: Dict[str, List] = {}
        self._full_content_cache: Dict[str, str] = {}
        self._doc_cache: Dict[str, Any] = {}
        self._progress_cache: Optional[dict] = None

        self._load_cache()

    # ===== 文件服务根目录 =====

    def get_data_root(self) -> Path:
        """主目录（第一行）：缓存、EPUB 解包产物与阅读进度都在它下面。"""
        return Path(self.document_dir)

    def get_file_roots(self) -> List[Path]:
        """`/file` 允许访问的根：全部配置目录（EPUB 解包出来的图片按绝对路径引用）。"""
        return [Path(root) for root in self._roots]

    # ===== 设置持久化 =====

    def _parse_roots(self, raw: Any) -> List[str]:
        """把「文档根目录」解析成有序列表。

        值格式与其它插件一致：字符串、每行一个目录（shell 的目录字段 `multi: True`）。
        为空时回落到默认数据目录；配置了但暂时不存在的目录保留着（外接盘没插时不该
        把用户的选择丢掉），扫描阶段自然会跳过它。
        """
        entries = '\n'.join(str(item) for item in raw) if isinstance(raw, (list, tuple)) else str(raw or '')
        roots: List[str] = []
        for line in entries.splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                resolved = str(Path(text).expanduser().resolve())
            except (OSError, ValueError):
                log.warning(f'[{self.name}] 忽略无法解析的目录: {text}')
                continue
            if resolved not in roots:
                roots.append(resolved)
        if not roots:
            roots = [str(Path(super().get_data_root()).resolve())]
        return roots

    def _set_roots(self, roots: List[str]) -> None:
        self._roots = roots
        self.document_dir = roots[0]
        self._cache_dir = os.path.join(self.document_dir, self.CACHE_DIR_NAME)
        # 迁移必须早于 makedirs：新目录一旦建出来，旧目录就搬不过去了
        self._migrate_legacy_state()
        os.makedirs(self._cache_dir, exist_ok=True)

    def _migrate_legacy_state(self) -> None:
        """把旧插件名下的状态目录/文件搬到新名。

        状态放在**用户的文档目录**里（`.novel_state/`），改名后新代码找的是
        `.document_state/` —— 不搬就是"升级后所有阅读进度归零"。
        """
        legacy_dir = os.path.join(self.document_dir, self.LEGACY_CACHE_DIR)
        if not os.path.isdir(legacy_dir):
            return
        try:
            if not os.path.isdir(self._cache_dir):
                os.rename(legacy_dir, self._cache_dir)
                log.info(f'[{self.name}] 已迁移状态目录 {self.LEGACY_CACHE_DIR} → {self.CACHE_DIR_NAME}')
            # 目录搬过来后文件名还是旧的，逐个补上（包含解包产物所在子目录，不用动）。
            # 源文件可能在新目录里（刚整体搬过来），也可能还在旧目录里（新目录先建好了）。
            for old_name, new_name in ((self.LEGACY_CACHE_FILE, self.CACHE_FILE),
                                       (self.LEGACY_PROGRESS_FILE, self.PROGRESS_FILE)):
                dst = os.path.join(self._cache_dir, new_name)
                if os.path.exists(dst):
                    continue
                for base in (self._cache_dir, legacy_dir):
                    src = os.path.join(base, old_name)
                    if os.path.isfile(src):
                        os.replace(src, dst)
                        break
        except OSError as e:
            # 迁移失败只影响缓存与进度，不该让插件加载不了
            log.warning(f'[{self.name}] 旧状态目录迁移失败: {e}')
            return
        # 搬空的旧目录不该继续留在用户的文档目录里
        try:
            if os.path.isdir(legacy_dir) and not os.listdir(legacy_dir):
                os.rmdir(legacy_dir)
        except OSError:
            pass

    def on_settings_changed(self, changed_keys):
        if 'root_dir' in changed_keys:
            self._apply_root_dir(self.setting('root_dir'))

    def _apply_root_dir(self, raw_dir):
        self._set_roots(self._parse_roots(raw_dir))
        self._document_cache = {}
        self._chapter_cache = {}
        self._offset_cache = {}
        self._full_content_cache = {}
        self._doc_cache = {}
        self._load_cache()

    def on_load(self) -> None:
        """改名迁移：旧插件名（novel-reader）的设置文件里存着用户配置的 root_dir。

        构造期 `_settings_store` 尚未注入（`setting()` 那时只能读到壳预解析的
        `_resolved_config`），所以迁移放在 on_load：先补写新名下的设置，再按它重建根目录。
        迁移只在新区没有 root_dir 时发生，用户之后自己保存的设置不会被覆盖。
        """
        if self._settings_store is None or str(self.setting('root_dir') or '').strip():
            return
        try:
            legacy = self._settings_store.get('novel-reader') or {}
        except (TypeError, ValueError) as e:
            log.warning(f'[{self.name}] 读取旧插件名下的设置失败: {e}')
            return
        root_dir = str(legacy.get('root_dir') or '').strip()
        if not root_dir:
            return
        self.update_setting('root_dir', root_dir)
        log.info(f'[{self.name}] 已从 novel-reader 的设置迁移 root_dir')
        self._apply_root_dir(root_dir)

    # ===== API 注册 =====

    def register_api(self) -> dict:
        return {
            'document_list': self.list_documents,
            'document_get_chapters': self.get_chapters,
            'document_get_content': self.get_content,
            'document_update_progress': self.update_progress,
            'document_open_external': self.open_external,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    # ===== 核心业务（由旧版 DocumentModule 迁移） =====

    def _cache_path(self, name: str) -> str:
        return os.path.join(self._cache_dir, name)

    def _extract_dir(self, document_id: str) -> str:
        """EPUB 内嵌图片的解包目录（目录名用哈希，书名里的字符不进路径）。"""
        return os.path.join(self._cache_dir, 'extract', hashlib.md5(document_id.encode('utf-8')).hexdigest())

    def _image_url(self, abs_path: str) -> str:
        """把解包/引用的图片拼成 Shell 文件路由的 URL。

        用绝对路径而不是相对路径：多根目录下相对路径只认第一根，别的目录里的书就会
        404；文件路由会再校验这个路径是否落在 `get_file_roots()` 之内。
        """
        return f'/file?path={quote(str(abs_path))}&plugin={self.name}'

    def _load_cache(self):
        cache_file = self._cache_path(self.CACHE_FILE)
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
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

    def _save_cache(self):
        cache_file = self._cache_path(self.CACHE_FILE)
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'parser_version': self.CACHE_VERSION,
                    'documents': self._document_cache,
                    'chapters': self._chapter_cache,
                    'offsets': self._offset_cache
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_progress(self) -> dict:
        if self._progress_cache is not None:
            return self._progress_cache
        progress_file = self._cache_path(self.PROGRESS_FILE)
        self._progress_cache = {}
        if os.path.exists(progress_file):
            try:
                with open(progress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._progress_cache = data
            except Exception:
                self._progress_cache = {}
        return self._progress_cache

    def _save_progress(self, progress: dict):
        self._progress_cache = progress
        progress_file = self._cache_path(self.PROGRESS_FILE)
        try:
            with open(progress_file, 'w', encoding='utf-8') as f:
                json.dump(progress, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _drop_book_cache(self, document_id: str) -> None:
        """文件被改动/删除后清掉这本书的全部缓存，含 EPUB 解包出来的图片。"""
        prefix = document_id + ':'
        for cache in (self._chapter_cache, self._offset_cache,
                      self._full_content_cache, self._doc_cache):
            for key in [k for k in cache if k == document_id or k.startswith(prefix)]:
                cache.pop(key, None)
        shutil.rmtree(self._extract_dir(document_id), ignore_errors=True)

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
        """获取文档列表，检查文件修改时间"""
        current_files: Dict[str, dict] = {}
        for document_id, abs_path, _root, _rel, _kind in self._iter_documents():
            try:
                stat = os.stat(abs_path)
            except OSError as e:
                log.warning(f'[{self.name}] 读取文件信息失败 {abs_path}: {e}')
                continue
            current_files[document_id] = {'mtime': stat.st_mtime, 'size': stat.st_size}

        # 检查哪些文件需要更新
        needs_update = False
        for name, info in current_files.items():
            cached = self._document_cache.get(name)

            # 如果文件是新添加的，或者修改时间/大小变化了，需要重新解析。
            # 注意这里读的是 `file_size`（缓存条目本身就是返回给前端的那个 dict），
            # 不是本函数临时算的 `size` —— 两边键名不一致时比较恒为"变了"，于是每次
            # 列书架都会全量重扫 + 把所有章节缓存和章节数丢掉（书架那一行永远显示 "?"）。
            if not cached or \
               cached.get('mtime') != info['mtime'] or \
               cached.get('file_size') != info['size']:
                needs_update = True
                self._drop_book_cache(name)
                if cached:
                    # 内容变了，之前数出来的章节数要重算
                    cached['chapter_count'] = 0

        # 检查是否有文件被删除
        cached_ids = set(self._document_cache.keys())
        current_ids = set(current_files)
        if cached_ids != current_ids:
            needs_update = True
            for name in cached_ids - current_ids:
                self._drop_book_cache(name)

        # 如果没有变化，直接返回缓存
        if not needs_update and self._document_cache:
            return {'documents': list(self._document_cache.values())}

        # 重新扫描（按 id 排序：多根目录、递归之后 os.walk 的顺序不稳定）
        documents = []
        for document_id, abs_path, root, rel, kind in self._iter_documents():
            document_info = self._scan_document_meta(document_id, abs_path, root, rel, kind)
            if document_info:
                documents.append(document_info)
        documents.sort(key=lambda item: item['id'].lower())

        self._document_cache = {n['id']: n for n in documents}
        self._save_cache()

        return {'documents': documents}

    def _scan_document_meta(self, document_id: str, abs_path: str, root: str,
                         rel: str, kind: str) -> Optional[dict]:
        """扫描文档元信息，记录修改时间和大小"""
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
            # 章节数解析过一次就一直留着：只在文件变更时清零，列表里别显示成 "?"
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
                'mtime': stat.st_mtime,  # 记录修改时间
                'chapter_count': cached.get('chapter_count') or 0,
                'last_read_time': document_progress.get('last_read_time', ''),
                'last_read_chapter': document_progress.get('last_read_chapter', 0),
                'progress': document_progress.get('progress', 0.0),
                'scroll_position': document_progress.get('scroll_position', 0.0),
                'encoding': document_progress.get('encoding', 'auto')
            }
        except Exception as e:
            log.error(f"扫描文档失败 {abs_path}: {e}")
            return None

    def _document(self, document_id: str, encoding: str = 'auto'):
        """构造并缓存格式文档对象（EPUB 的解包目录与图片 URL 在这里注入）。"""
        key = f"{document_id}:{encoding}"
        doc = self._doc_cache.get(key)
        if doc is not None:
            return doc

        document_info = self._document_cache[document_id]
        doc = open_document(
            document_info['file_path'],
            document_info['kind'],
            encoding=encoding,
            root_dir=document_info.get('root') or self.document_dir,
            extract_dir=self._extract_dir(document_id),
            image_url=self._image_url,
        )
        if len(self._doc_cache) >= 3:
            self._doc_cache.pop(next(iter(self._doc_cache)), None)
        self._doc_cache[key] = doc
        return doc

    def get_chapters(self, document_id: str, encoding: str = 'auto') -> Dict[str, Any]:
        """获取章节目录（缓存按文档 + 格式 + 编码隔离）"""
        document_info = self._document_cache.get(document_id)
        if not document_info:
            return {'error': '文档不存在', 'chapters': []}

        kind = document_info['kind']
        if kind not in self.IN_APP_KINDS:
            # pdf / 其他格式没有章节模型，前端据此直接进"系统程序打开"分支
            return {'chapters': []}

        key = f"{document_id}:{kind}:{encoding}"
        if key in self._chapter_cache:
            return {'chapters': self._chapter_cache[key]}

        try:
            if kind == 'txt':
                chapters, offsets = TxtParser.parse_txt(document_info['file_path'], encoding)
                self._offset_cache[key] = offsets
            else:
                chapters = self._document(document_id, encoding).chapters()

            self._chapter_cache[key] = chapters
            self._save_cache()

            if document_id in self._document_cache:
                self._document_cache[document_id]['chapter_count'] = len(chapters)

            return {'chapters': chapters}
        except Exception as e:
            return {'error': f'解析章节失败: {e}', 'chapters': []}

    def get_full_content(self, document_id: str, encoding: str = 'auto') -> Dict[str, Any]:
        """获取纯文本文档完整内容（缓存按文档 + 编码隔离）"""
        key = f"{document_id}:{encoding}"
        if key in self._full_content_cache:
            return {'content': self._full_content_cache[key]}

        document_info = self._document_cache.get(document_id)
        if not document_info:
            return {'error': '文档不存在', 'content': ''}

        try:
            content = TxtParser.read_full_content(document_info['file_path'], encoding)
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
        document_info = self._document_cache.get(document_id)
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

        if key not in self._offset_cache:
            chapters_result = self.get_chapters(document_id, encoding)
            if chapters_result.get('error'):
                return {'error': chapters_result['error'], 'content': ''}

        offsets = self._offset_cache.get(key)
        if not offsets or not isinstance(chapter_index, int) or chapter_index < 0 or chapter_index >= len(offsets):
            return {'error': '章节索引无效', 'content': ''}

        entry = offsets[chapter_index]
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            start_offset, end_offset = entry
        else:
            # 兼容旧版单偏移缓存
            start_offset = entry
            end_offset = offsets[chapter_index + 1] if chapter_index + 1 < len(offsets) else len(full_content)

        chapter_content = full_content[int(start_offset):int(end_offset)]
        return {'content': chapter_content, 'format': 'text'}

    def open_external(self, document_id: str) -> Dict[str, Any]:
        """用系统默认程序打开文档（阅读器内不渲染的格式：pdf/docx/mobi…）。"""
        document_info = self._document_cache.get(document_id)
        if not document_info:
            return {'error': '文档不存在'}
        try:
            path = Path(document_info['file_path']).resolve()
            roots = [Path(root).resolve() for root in self._roots]
        except (OSError, ValueError) as e:
            return {'error': f'路径无效: {e}'}

        # 这里是"启动外部程序"的口子：路径只认配置目录之内的文件。否则任何能调 API 的
        # 同源脚本都能借它拉起别处的东西（例如自己写个 bat 再打开）。
        if not any(path.is_relative_to(root) for root in roots):
            log.warning(f'[{self.name}] 拒绝越界的外部打开请求: {path}')
            return {'error': '路径越界'}
        if not path.is_file():
            return {'error': '文件不存在'}

        try:
            _docs_mod.open_with_system(path)
        except Exception as e:
            return {'error': f'调用系统程序失败: {e}'}
        return {'success': True}

    def update_progress(self, document_id: str, chapter_index: int,
                        scroll_position: float = 0.0,
                        encoding: str = 'auto') -> Dict[str, Any]:
        """更新阅读进度"""
        progress = self._load_progress()

        kind = (self._document_cache.get(document_id) or {}).get('kind', 'txt')
        chapters = self._chapter_cache.get(f"{document_id}:{kind}:{encoding}", [])
        total_chapters = len(chapters)
        if not isinstance(chapter_index, int) or chapter_index < 0:
            chapter_index = 0
        if total_chapters > 0:
            chapter_index = min(chapter_index, total_chapters - 1)
        overall_progress = (chapter_index + scroll_position) / total_chapters if total_chapters > 0 else 0.0

        progress[document_id] = {
            'last_read_time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'last_read_chapter': chapter_index,
            'scroll_position': scroll_position,
            'progress': round(overall_progress, 4),
            'encoding': encoding
        }

        self._save_progress(progress)

        if document_id in self._document_cache:
            self._document_cache[document_id].update({
                'last_read_time': progress[document_id]['last_read_time'],
                'last_read_chapter': chapter_index,
                'progress': overall_progress,
                'encoding': encoding
            })

        return {'success': True}
