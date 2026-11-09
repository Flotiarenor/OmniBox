"""document-reader 插件入口：API 表、壳钩子与装配。

本文件只做三件事：装配组件、声明对壳暴露的 API、把壳按名调用的钩子一行委派给组件。
设置项 schema 与常量在 `const.py`，业务逻辑在各组件里（分层门禁硬上限 250 行，目标是
200 行以内）。

接缝说明：`load_sibling` 返回 `ModuleType`，装配出来的组件属性在静态检查里是 `Any`。
这是有意的取舍 —— 需要检查的东西都在组件里；为让接缝有类型而塞进 `sys.path` 会换来
模块名冲突。

装配顺序就是依赖顺序：根目录 → 书架 → 正文 → 书签 → 朗读（引擎 / 音色 / 缓存）→
刷新；反向依赖一律由构造参数注入。`get_data_root` 直接返回 `roots.data_root()`：
已无 mixin 覆写基类方法。
"""

from pathlib import Path
from typing import Any, ClassVar, Dict, List

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

_const = load_sibling(__file__, 'const', 'document_reader')
_roots = load_sibling(__file__, 'roots', 'document_reader')
_library = load_sibling(__file__, 'library', 'document_reader')
_content = load_sibling(__file__, 'content', 'document_reader')
_marks = load_sibling(__file__, 'marks', 'document_reader')
_freshness = load_sibling(__file__, 'freshness', 'document_reader')
_parser = load_sibling(__file__, 'parser', 'document_reader')
_documents = load_sibling(__file__, 'documents', 'document_reader')
_markdown = load_sibling(__file__, 'markdown', 'document_reader')
_epub = load_sibling(__file__, 'epub', 'document_reader')
_tts = load_sibling(__file__, 'tts', 'document_reader')
_tts_cache = load_sibling(__file__, 'tts_cache', 'document_reader')
_tts_engine = load_sibling(__file__, 'tts_engine', 'document_reader')
_tts_edge = load_sibling(__file__, 'tts_edge', 'document_reader')
_tts_endpoint = load_sibling(__file__, 'tts_endpoint', 'document_reader')
_tts_system = load_sibling(__file__, 'tts_system', 'document_reader')


class DocumentReaderPlugin(PluginBase):
    # 设置项声明是纯数据，放在 const.py（入口只做装配与委派）。
    settings_schema: ClassVar[List[Dict[str, Any]]] = _const.SETTINGS_SCHEMA

    def __init__(self, manifest: dict, config: dict) -> None:
        super().__init__(manifest, config)
        names = _const.LAYOUT
        # 显式写 PluginBase.get_data_root(self)，不用 super()：不依赖 MRO 顺序。
        self.roots = _roots.RootLayout(
            self, lambda: PluginBase.get_data_root(self), names, self.name)
        self.library = _library.Library(self.roots, names, _const.KIND_BY_EXT, self.name)
        self.content = _content.DocumentContent(
            self.library, self.roots, _parser, _documents,
            _epub.EpubDocument, _markdown.MarkdownDocument,
            self._image_url, _documents.open_with_system,
            _const.IN_APP_KINDS, self.name)
        # 书被丢弃时，正文对象与全文缓存由 content 自己清（library 不持有它们）
        self.library.set_derived_handler(self.content.drop_book_cache)
        self.marks = _marks.Marks(
            self.roots.cache_path, names.marks_file,
            lambda document_id: self.library.info(document_id) is not None, self.name)

        # 朗读：三档引擎由入口装配成映射，组件之间互不知情
        speech = _tts_engine.SpeechEngine(
            {
                'edge': _tts_edge.synthesize,
                'openai': _tts_endpoint.synthesize,
                'system': _tts_system.synthesize,
            },
            {
                'edge': _tts_edge.available,
                'openai': _tts_endpoint.available,
                'system': _tts_system.available,
            },
            _tts_endpoint.VoiceCatalog(_tts_edge.voices).engine_voice_names,
        )
        voices = _tts_endpoint.VoiceCatalog(_tts_edge.voices)
        cache = _tts_cache.TtsCache(
            _const.TTS_CACHE_SUBDIR, _const.TTS_LIMITS.cache_bytes, self.name)
        self.tts = _tts.TtsService(
            self, self._raw_settings, self._image_url, _documents.open_with_system,
            speech, voices, cache, _const.TTS_LIMITS, self.name)

        self.freshness = _freshness.FreshnessProvider(
            self.roots, self.library, self.content,
            _const.KIND_BY_EXT, names.cache_version)

    def register_api(self) -> dict:
        return {
            'document_list': self.library.list_documents,
            'document_get_chapters': self.content.get_chapters,
            'document_get_content': self.content.get_content,
            'document_update_progress': self.library.update_progress,
            'document_open_external': self.content.open_external,
            'document_cover': self.content.document_cover,
            'marks_list': self.marks.marks_list,
            'marks_add': self.marks.marks_add,
            'marks_remove': self.marks.marks_remove,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
            # 朗读（实现见 backend/tts.py；引擎与音色走设置项）
            'tts_segment': self.tts.tts_segment,
            'tts_speak': self.tts.tts_speak,
            'tts_status': self.tts.tts_status,
            'tts_voices': self.tts.tts_voices,
            'tts_cache_info': self.tts.tts_cache_info,
            'tts_clear_cache': self.tts.tts_clear_cache,
            'tts_open_cache_dir': self.tts.tts_open_cache_dir,
        }

    # ===== 壳按名调用的钩子：名字与签名与迁移前完全一致 =====

    def on_load(self) -> None:
        """改名迁移：旧插件名下设置里的 `root_dir` 补写到新名，再按它重绑根目录。"""
        migrated = _roots.migrate_legacy_root_dir(
            self, self._settings_store, self.name,
            _const.LAYOUT.legacy_plugin, self.update_setting)
        if migrated:
            self._rebind_roots(migrated)

    def on_settings_changed(self, changed_keys) -> None:
        """设置变更：按改动范围重绑组件状态（入口是唯一知道依赖关系的编排点）。"""
        if 'root_dir' in changed_keys:
            self._rebind_roots(self.setting('root_dir'))

    def get_data_root(self) -> Path:
        return self.roots.data_root()

    def get_file_roots(self) -> List[Path]:
        return self.roots.file_roots(self.tts.file_root())

    def freshness_spec(self) -> Dict[str, Any]:
        return self.freshness.spec()

    # ===== 组件共用的宿主能力（闭包注入，组件不持有插件对象） =====

    def _image_url(self, abs_path: str) -> str:
        """把解包/引用的图片拼成 Shell 文件路由的 URL（绝对路径，多根目录下也不 404）。"""
        from urllib.parse import quote

        return f'/file?path={quote(str(abs_path))}&plugin={self.name}'

    def _rebind_roots(self, raw: Any) -> None:
        """换根：路径、书架缓存、书签与正文对象全部按新根重来。"""
        self.roots.rebind(raw)
        self.library.reset_for_new_root()
        self.content.clear_caches()
        self.marks.reset()
