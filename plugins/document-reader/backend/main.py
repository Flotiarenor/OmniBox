import hashlib
import logging
import os
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple
from urllib.parse import quote

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)


_parser_mod = load_sibling(__file__, 'parser', 'document_reader')
TxtParser = _parser_mod.TxtParser

_docs_mod = load_sibling(__file__, 'documents', 'document_reader')
open_document = _docs_mod.open_document

# 分片：方法按职责拆出去，由 load_sibling 加载成 mixin。
# **mixin 必须排在 PluginBase 之前**（MRO 顺序）：分片里有覆写基类的方法时，
# 排在基类后面会被基类实现盖掉。
_marks = load_sibling(__file__, 'marks', 'document_reader')
_tts = load_sibling(__file__, 'tts', 'document_reader')
_library = load_sibling(__file__, 'library', 'document_reader')
_roots = load_sibling(__file__, 'roots', 'document_reader')


class DocumentReaderPlugin(
    _marks.MarksMixin,
    _tts.TtsMixin,
    _library.LibraryMixin,
    _roots.RootsMixin,
    PluginBase,
):
    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        {"key": "root_dir", "label": "文档根目录", "type": "directory", "multi": True,
         "placeholder": "输入目录绝对路径，如 D:\\文档",
         "emptyText": "未添加任何目录，将使用默认数据目录（./data）",
         "help": "可添加多个目录（每行一个，第一行为主目录，缓存与阅读进度存在它下面）；"
                 "每个目录都会递归扫描子目录"},
        # ===== 朗读（见 backend/tts_engine.py）=====
        {"key": "tts_engine", "label": "朗读引擎", "type": "select", "default": "auto",
         "options": [
             {"label": "自动（按下面的顺序降级）", "value": "auto"},
             {"label": "edge-tts（神经音色，联网，支持逐字跟随）", "value": "edge"},
             {"label": "OpenAI 兼容端点（本地模型/云服务）", "value": "openai"},
             {"label": "系统离线音色（SAPI5，兜底）", "value": "system"},
         ],
         "central": False},
        {"key": "tts_order", "label": "自动模式顺序", "type": "text", "default": "edge,openai,system",
         "central": False,
         "help": "逗号分隔，留空用默认顺序。例如只想要本地：openai,system"},
        {"key": "tts_base_url", "label": "OpenAI 兼容端点", "type": "text", "default": "",
         "placeholder": "http://127.0.0.1:8880", "central": False,
         "help": "只填根地址，程序会请求 <它>/v1/audio/speech。插件不含模型，"
                 "模型由你自己的服务提供（Kokoro-FastAPI / GPT-SoVITS / 云 API 均可）"},
        {"key": "tts_api_key", "label": "端点 API Key", "type": "text", "default": "",
         "secret": True, "central": False,
         "help": "本地服务通常不需要；云服务填这里（保存后不明文回显）"},
        {"key": "tts_model", "label": "端点模型名", "type": "text", "default": "",
         "placeholder": "留空则不发送 model 字段", "central": False},
        {"key": "tts_voice", "label": "朗读音色", "type": "text", "default": "zh-CN-XiaoxiaoNeural",
         "central": False,
         "help": "edge-tts 用 ShortName（如 zh-CN-XiaoxiaoNeural / zh-CN-YunxiNeural），"
                 "OpenAI 兼容端点用自己的音色名（如 alloy、zf_xiaoxiao）"},
        {"key": "tts_rate", "label": "朗读语速", "type": "range", "min": -50, "max": 100,
         "step": 5, "default": 100, "central": False,
         "help": "百分比：100 是端点允许的倍速上限（约两倍速），听书默认拉满；"
                 "-50 最慢。再往上端点会开始吞字，所以上限就到这里"},
        # ===== 阅读偏好（原先存 localStorage，改成跨设备持久化）=====
        {"key": "reader_font_size", "label": "正文字号(px)", "type": "number", "default": 16,
         "central": False},
        {"key": "reader_line_height", "label": "行距", "type": "number", "default": 1.8,
         "central": False},
        {"key": "reader_letter_spacing", "label": "字间距(px)", "type": "number", "default": 0,
         "central": False},
        {"key": "reader_theme", "label": "阅读主题", "type": "select", "default": "auto",
         "options": [
             {"label": "跟随主题", "value": "auto"},
             {"label": "护眼纸", "value": "sepia"},
             {"label": "夜间", "value": "dark"},
             {"label": "绿色", "value": "green"},
             {"label": "蓝色", "value": "blue"},
             {"label": "自定义", "value": "custom"},
         ], "central": False},
        {"key": "reader_bg_color", "label": "自定义背景色", "type": "text", "default": "#ffffff",
         "central": False},
        {"key": "reader_text_color", "label": "自定义文字色", "type": "text", "default": "#1a1a1a",
         "central": False},
        {"key": "reader_mode", "label": "阅读模式", "type": "select", "default": "scroll",
         "options": [
             {"label": "连续滚动", "value": "scroll"},
             {"label": "翻页（章内滚动）", "value": "page"},
         ], "central": False},
    ]

    CACHE_FILE = '.document_cache.json'
    PROGRESS_FILE = '.document_progress.json'
    # 书签文件名在 marks.py 的 MarksMixin.MARKS_FILE（它是书签那一块的实现细节）
    # 章节缓存键加入格式与编码、文档 id 改为完整文件名（含扩展名）后升版：
    # 旧版的章节/偏移缓存与新的键对不上，留着只会变成读不到的垃圾。
    # v4：TxtParser 的编码兜底修好后，同一本书解出来的正文长度会变（GBK 文件过去被
    # 按 utf-8 + ignore 解成了乱码），旧偏移切片到新正文上就是错位的章节。
    CACHE_VERSION = 4

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
        self._marks_cache: Optional[dict] = None

        self._load_cache()

    # ===== 文件服务根目录 / 设置持久化 =====
    #
    # `get_data_root` / `get_file_roots` / `_parse_roots` / `_set_roots` /
    # `_migrate_legacy_state` / `_apply_root_dir` / `on_settings_changed` / `on_load`
    # 全部在 roots.py 的 RootsMixin 里。**不要在本类里再定义一份**：本类排在 MRO
    # 最前，重复定义会静默盖掉分片（曾经因此让 `/file` 不认朗读缓存目录 → 音频 403）。

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
            'document_cover': self.document_cover,
            'marks_list': self.marks_list,
            'marks_add': self.marks_add,
            'marks_remove': self.marks_remove,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
            # 朗读（实现见 backend/tts_engine.py；引擎与音色走设置项）
            'tts_segment': self.tts_segment,
            'tts_speak': self.tts_speak,
            'tts_status': self.tts_status,
            'tts_voices': self.tts_voices,
            'tts_cache_info': self.tts_cache_info,
            'tts_clear_cache': self.tts_clear_cache,
            'tts_open_cache_dir': self.tts_open_cache_dir,
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

    def document_cover(self, document_id: str) -> Dict[str, Any]:
        """取文档封面图 URL（只有 EPUB 可能有）。

        返回 `{'cover': ''}` 是正常结果，不是错误 —— 表示"这本书没有封面图"，
        前端据此画生成式封面（外框 + 书脊 + 四条按书名哈希上色的文字条）。
        封面由 `documents.py` 解包到 `.document_state/extract/<哈希>/cover.*`，
        而 `_image_url` 用的是绝对路径，多根目录下也不会 404。
        """
        document_info = self._document_cache.get(document_id)
        if not document_info:
            return {'cover': ''}
        if document_info['kind'] != 'epub':
            return {'cover': ''}
        try:
            cover = self._document(document_id).cover()
        except Exception as e:
            log.warning(f'[{self.name}] 读取封面失败 {document_id}: {e}')
            return {'cover': ''}
        return {'cover': cover or ''}


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

    def tts_open_cache_dir(self) -> Dict[str, Any]:
        """用系统文件管理器打开朗读缓存目录（用户在界面点"打开所在文件夹"）。

        路径不来自请求参数，而是缓存目录自身，所以不存在越界问题；仍然先确认它存在，
        避免系统弹出一个"找不到路径"的对话框。
        """
        cache_dir = self._tts_cache_dir()
        if not cache_dir.is_dir():
            return {'success': False, 'error': '缓存目录不存在'}
        try:
            _docs_mod.open_with_system(cache_dir)
        except Exception as e:
            return {'success': False, 'error': f'调用文件管理器失败: {e}'}
        return {'success': True, 'path': str(cache_dir)}

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


