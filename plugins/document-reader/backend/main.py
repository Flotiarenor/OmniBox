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

_tts_mod = load_sibling(__file__, 'tts_engine', 'document_reader_tts')

class DocumentReaderPlugin(PluginBase):
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
    # 书签：**唯一需要字级位置的状态**（阅读进度只记到章，见 docs/document-reader-redesign.md §4）
    MARKS_FILE = '.document_marks.json'
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
        self._marks_cache = None
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

    # ===== 朗读 =====
    #
    # 文本从哪来：**由前端给**（它读的是已经渲染进 DOM 的当前章正文），而不是后端按
    # 章节号重新解析一遍。三个理由：
    #   1. txt/md/epub 三种格式在前端 `.chapter-content` 那一层已经归一成同一份正文，
    #      后端再解析就要把格式分支写第二遍；
    #   2. 用户"从这一段开始读"的位置只有 DOM 知道，后端拿不到；
    #   3. 缓存键直接用文本内容算，同一段文字重复合成天然命中，不用跟章节号、编码、
    #      解析版本这些上下文纠缠。
    # 代价是后端不校验文本内容，只限长度 —— 这是同一台机器上的同源调用，可接受。

    def _tts_cache_dir(self) -> str:
        return os.path.join(self._cache_dir, 'tts')

    def _tts_params(self) -> dict:
        """把设置项整理成引擎参数。语音名交给引擎自己解释（各家的音色命名不同）。"""
        order = [item.strip() for item in str(self.setting('tts_order') or '').split(',')]
        order = [item for item in order if item]
        mode = str(self.setting('tts_engine') or 'auto')
        voice = str(self.setting('tts_voice') or '').strip()
        if mode == 'openai' and voice in ('', _tts_mod.DEFAULT_VOICE):
            # 默认音色是 edge 的名字，直接丢给第三方端点会被拒；让它用自己的默认
            voice = 'alloy'
        return {
            'mode': mode,
            'order': order or list(_tts_mod.ENGINES),
            'voice': voice or _tts_mod.DEFAULT_VOICE,
            'rate': int(self.setting('tts_rate') or 0),
            'base_url': str(self.setting('tts_base_url') or ''),
            'api_key': self._raw_settings().get('tts_api_key') or '',
            'model': str(self.setting('tts_model') or ''),
        }

    def tts_segment(self, text: str, max_chars: int = 240) -> Dict[str, Any]:
        """把正文切成"一句一片"：前端按片请求音频，边合成边播，不用等整章。"""
        segments = _tts_mod.split_text(str(text or ''), max(40, min(int(max_chars or 240), 1000)))
        return {'segments': segments}

    def tts_status(self) -> Dict[str, Any]:
        """引擎可用性一览 + 当前配置，供阅读器的朗读面板显示。"""
        params = self._tts_params()
        data = _tts_mod.status(params)
        data['mode'] = params['mode']
        data['voice'] = params['voice']
        data['rate'] = params['rate']
        return data

    def tts_voices(self) -> Dict[str, Any]:
        """edge 端点上**真实可用**的中文音色列表（给朗读设置页用）。

        `source` 告诉前端这份列表的来路：`edge` 表示问过端点（权威），
        `fallback` 表示端点不可达（前端用内置的常用音色兜底）。
        """
        voices = _tts_mod.edge_voices()
        return {'voices': voices, 'source': 'edge' if voices else 'fallback'}

    def tts_speak(self, text: str, cache_key: str = '', segment_index: int = 0,
                  engine: str = '') -> Dict[str, Any]:
        """合成一段文本并返回可播放的同源 URL（+ 可选的逐字时间戳）。

        `cache_key` 由前端给：**文本内容本身**的短哈希（同一段文字必定同一把钥匙）。
        不给就按文本现算，行为等价，只是前端能省掉一次传输。
        """
        text = str(text or '')
        if not text.strip():
            return {'error': '没有可朗读的文本'}
        if len(text) > _tts_mod.MAX_CHARS:
            return {'error': f'这一段太长（{len(text)} 字），请缩短后再朗读'}

        params = self._tts_params()
        if engine and engine in _tts_mod.ENGINES:
            params['mode'] = engine
        cache_dir = self._tts_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)

        key = str(cache_key or hashlib.md5(text.encode('utf-8')).hexdigest())[:32]
        # 文件名里带上引擎与音色：换了引擎/音色/语速就该重新合成，
        # 否则用户切换后听到的还是上一种声音（缓存"命中"得太成功）
        stamp = hashlib.md5(
            f"{params['mode']}|{params['voice']}|{params['rate']}|{params['base_url']}"
            .encode('utf-8')).hexdigest()[:8]
        prefix = f'{key}-{stamp}'
        # 只认音频扩展名：同一把钥匙旁边还躺着 `.json` 元数据文件，
        # 用 startswith 直接取第一个会把元数据当成音频返回（前端拿到 404/噪音）。
        audio_exts = ('.mp3', '.wav', '.ogg', '.opus', '.m4a')
        existing = [name for name in os.listdir(cache_dir)
                    if name.startswith(prefix + '.') and name.lower().endswith(audio_exts)]
        if existing:
            path = os.path.join(cache_dir, existing[0])
            meta = self._tts_cache_meta(cache_dir, prefix)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = time.time()
            return {'url': self._image_url(path), 'mtime': mtime, 'cached': True,
                    'engine': meta.get('engine', ''), 'marks': meta.get('marks', []),
                    'durationMs': meta.get('durationMs', 0), 'segmentIndex': segment_index}

        try:
            result = _tts_mod.synthesize(text, params)
        except Exception as e:
            log.warning(f'[{self.name}] 朗读失败: {e}')
            return {'error': str(e)}
        if len(result['audio']) > 40 * 1024 * 1024:
            return {'error': '合成结果异常大（超过 40MB），已放弃播放'}

        path = os.path.join(cache_dir, prefix + result['ext'])
        try:
            with open(path, 'wb') as f:
                f.write(result['audio'])
            with open(os.path.join(cache_dir, prefix + '.json'), 'w', encoding='utf-8') as f:
                json.dump({'engine': result['engine'], 'marks': result['marks']},
                          f, ensure_ascii=False)
        except OSError as e:
            log.warning(f'[{self.name}] 朗读音频落盘失败: {e}')
            return {'error': f'音频写入失败: {e}'}

        self._tts_prune(cache_dir)
        return {'url': self._image_url(path), 'mtime': os.path.getmtime(path), 'cached': False,
                'engine': result['engine'], 'marks': result['marks'],
                'segmentIndex': segment_index}

    @staticmethod
    def _tts_cache_meta(cache_dir: str, prefix: str) -> dict:
        try:
            with open(os.path.join(cache_dir, prefix + '.json'), 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _tts_prune(self, cache_dir: str, keep_bytes: int = 200 * 1024 * 1024) -> None:
        """缓存目录超过上限时，从最旧的开始删（音频+时间戳成对删）。

        `ponytail:` 上限是"整目录字节数"，不是"按书/按章"，也没有专门的索引文件 ——
        一次 os.scandir 排序足够；真到了几十万文件再看是否需要数据库。
        """
        entries = []
        total = 0
        try:
            for entry in os.scandir(cache_dir):
                if not entry.is_file():
                    continue
                size = entry.stat().st_size
                total += size
                entries.append((entry.stat().st_mtime, size, entry.path))
        except OSError:
            return
        if total <= keep_bytes:
            return
        entries.sort()
        for _mtime, size, path in entries:
            stem, _ext = os.path.splitext(path)
            for target in (path, stem + '.json'):
                try:
                    if os.path.exists(target):
                        os.remove(target)
                except OSError:
                    pass
            total -= size
            if total <= keep_bytes:
                break
        log.info(f'[{self.name}] 朗读缓存超过上限，已清理到 {total / 1024 / 1024:.0f} MB')

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

    # ===== 书签 =====
    #
    # 存 `{文档 id: [{chapter, char, snippet, time}]}`，与进度文件并列放状态目录。
    # `char` 是字级偏移（相对本章开头，坐标系是 `.chapter-content` 的 textContent），
    # `snippet` 是定位校验用的原文片段：解析器换了输出、正文节点漂移时，
    # 按 char 直接定位会静默落到隔壁段落 —— 有 snippet 才能在章内重新搜回来。

    def _load_marks(self) -> dict:
        if self._marks_cache is not None:
            return self._marks_cache
        self._marks_cache = {}
        path = self._cache_path(self.MARKS_FILE)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._marks_cache = data
            except Exception:
                self._marks_cache = {}
        return self._marks_cache

    def _save_marks(self, marks: dict) -> None:
        self._marks_cache = marks
        try:
            with open(self._cache_path(self.MARKS_FILE), 'w', encoding='utf-8') as f:
                json.dump(marks, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def marks_list(self, document_id: str = '') -> Dict[str, Any]:
        """列出书签。给 id 就只列那本书的，不给就列全部（书架左侧「书签」用）。"""
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
        items = [item for item in (marks.get(document_id) or [])
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
        items = marks.get(document_id) or []
        kept = [item for item in items
                if not (int(item.get('chapter', -1)) == int(chapter)
                        and int(item.get('char', -1)) == int(char))]
        if kept:
            marks[document_id] = kept
        else:
            marks.pop(document_id, None)
        self._save_marks(marks)
        return {'success': True, 'marks': marks.get(document_id) or []}

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
