"""media-player 插件入口：API 表、壳钩子与装配。

本文件只做三件事：声明插件对壳暴露什么、把组件装配起来、把壳按名调用的钩子一行
委派给组件。设置项 schema 在 `const.py`，业务逻辑在各组件里（分层门禁硬上限 250 行，
目标是 200 行以内）。

接缝说明：`load_sibling` 返回 `ModuleType`，装配出来的组件属性在静态检查里是 `Any`。
这是有意的取舍 —— 需要检查的东西都在组件里；为让接缝有类型而塞进 `sys.path` 会换来
模块名冲突。

装配顺序就是依赖顺序：路径 → 扫描器 → 索引 → 用户状态 → 封面库 → 查询 / 导出 / 歌词 /
预设 / 诊断 → 刷新。反向依赖一律由构造参数注入，组件之间不互相 import。

"数据根"有两个：本插件对壳声明的 `get_data_root()` 是**媒体根**（索引与封面库跟着它
走），而壳自己的数据根（`PluginBase.get_data_root()`）只用来放用户状态文件。
"""

import logging
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Set, Tuple

from shell.backend.freshness import engine_for
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_const = load_sibling(__file__, 'const', 'media_player')
_models = load_sibling(__file__, 'models', 'media_player')
_metadata = load_sibling(__file__, 'metadata', 'media_player')
_video_meta = load_sibling(__file__, 'video_meta', 'media_player')
_ffmpeg = load_sibling(__file__, 'video_ffmpeg', 'media_player')
_scanner = load_sibling(__file__, 'scanner', 'media_player')
_paths = load_sibling(__file__, 'paths', 'media_player')
_index = load_sibling(__file__, 'index', 'media_player')
_state = load_sibling(__file__, 'state', 'media_player')
_collections = load_sibling(__file__, 'collections', 'media_player')
_covers = load_sibling(__file__, 'covers', 'media_player')
_library = load_sibling(__file__, 'library', 'media_player')
_report = load_sibling(__file__, 'report', 'media_player')
_lyrics = load_sibling(__file__, 'lyrics', 'media_player')
_presets = load_sibling(__file__, 'presets', 'media_player')
_diagnostics = load_sibling(__file__, 'diagnostics', 'media_player')
_freshness = load_sibling(__file__, 'freshness', 'media_player')


class MediaPlayerPlugin(PluginBase):
    # 设置项声明是纯数据，放在 const.py；壳的设置弹窗与 PluginBase 都从类属性读。
    settings_schema: ClassVar[List[Dict[str, Any]]] = _const.SETTINGS_SCHEMA

    def __init__(self, manifest: dict, config: dict) -> None:
        super().__init__(manifest, config)
        # 显式写 PluginBase.get_data_root(self) 而不是 super()：不依赖 MRO 顺序。
        self.paths = _paths.PathLayout(self, PluginBase.get_data_root(self),
                                       lambda: PluginBase.get_data_root(self))
        self.scanner = _scanner.MediaScanner(_models, _metadata, _video_meta, _ffmpeg)
        self.index = _index.MediaIndex(self.paths.cache_file,
                                       self.scanner.index_version, _models)
        self.state = _state.UserStateStore(self.paths, self.index,
                                           self.scanner.item_id)
        self.state.adopt_orphan()
        self.state.load()
        self.index.load()
        self.state.migrate_legacy()
        self.paths.drop_stale_scan_task()
        self.collections = _collections.MediaCollections(
            self.state, self.index, _const.QUEUE_PERSIST_LIMIT,
            lambda: self._verified_keys())
        self.covers = _covers.CoverStore(self.paths, self.index,
                                         self.scanner.cover_generator)
        self.library = _library.MediaLibrary(self.index, self.state, _models)
        self.report = _report.MissingReport(self.paths, self.index)
        self.lyrics = _lyrics.LyricsReader(self.index)
        self.presets = _presets.EqPresets(Path(__file__).parent.parent / 'eq-presets')
        self.diagnostics = _diagnostics.AudioDiagnostics(
            self.index, self, _ffmpeg,
            lambda: load_sibling(__file__, 'metadata', 'media_player'))
        self.freshness = _freshness.FreshnessProvider(
            self.paths, self.index, self.covers, self.scanner, lambda: engine_for(self))
        # ffmpeg 可选通道：注入用户设置路径（探测结果缓存，设置变更时刷新）
        _ffmpeg.configure(self.setting('ffmpeg_path') or '')

    def _verified_keys(self) -> Optional[Set[str]]:
        """整趟校验确认存在的条目键；没有权威结论时返回 None。

        播放状态的清空判据（`MediaCollections.get_playback`）只认这个结论：索引里
        没有 ≠ 文件没了（启动早期索引为空、外接盘未挂载、首扫预算用尽都会这样）。
        引擎未接入或引擎还没跑过完整校验时返回 None，调用方据此保留用户数据。
        """
        engine = engine_for(self)
        if engine is None:
            return None
        # 引擎的 verified_keys 与入口并行实现：用 getattr 兼容尚未提供该查询的版本
        return getattr(engine, 'verified_keys', lambda: None)()

    def register_api(self) -> dict:
        return {
            'media_scan': self.freshness.scan,
            # 兼容入口：保留给按旧契约轮询扫描进度的调用方；壳挂载的刷新控件直接读
            # `system_freshness_state`，当前前端只调 `media_scan` 建索引，不调这两个方法。
            'media_scan_status': self.freshness.status,
            'media_scan_cancel': self.freshness.cancel,
            'media_search': self.library.search,
            'media_recent': self.library.recent,
            'media_stats': self.library.stats,
            'media_audio_albums': self.library.audio_albums,
            'media_all_audio': self.library.all_audio,
            'media_video_albums': self.library.video_albums,
            'media_all_video': self.library.all_video,
            'media_album_items': self.library.album_items,
            'media_get_item': self.library.get_item,
            'media_get_items': self.collections.get_items,
            'media_playlist_list': self.collections.playlist_list,
            'media_playlist_get': self.collections.playlist_get,
            'media_playlist_save': self.collections.playlist_save,
            'media_playlist_delete': self.collections.playlist_delete,
            'media_toggle_favorite': self.collections.toggle_favorite,
            'media_add_favorites': self.collections.add_favorites,
            'media_update_recent': self.collections.update_recent,
            'media_get_state': self.collections.get_state,
            'media_save_playback': self.collections.save_playback,
            'media_save_queue': self.collections.save_queue,
            'media_get_playback': self.collections.get_playback,
            'media_get_lyrics': self.lyrics.get,
            'media_export_missing': self.report.export,
            'media_put_thumb': self.covers.put,
            'media_thumb_missing': self.covers.missing,
            'media_debug_meta': self.diagnostics.debug_meta,
            'media_ffmpeg_status': self.diagnostics.ffmpeg_status,
            'media_list_eq_presets': self.presets.list_all,
            'media_save_eq_preset': self.presets.save,
            'media_get_config': lambda key, default=None: self.setting(key, default),
            'media_set_config': lambda key, value: {'success': self.update_setting(key, value)},
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    # ===== 壳按名调用的钩子：名字与签名与迁移前完全一致 =====

    def get_data_root(self) -> Path:
        return self.paths.root_dir

    def get_file_roots(self) -> List[Path]:
        """跨主目录与额外媒体目录提供文件访问。"""
        return self.paths.file_roots()

    def get_thumb_data(self, rel_path: str) -> Optional[Tuple[bytes, str]]:
        return self.covers.get_thumb_data(rel_path)

    def freshness_spec(self) -> Dict[str, Any]:
        return self.freshness.spec()

    def on_settings_changed(self, changed_keys) -> None:
        """设置变更：按改动范围重绑组件状态（入口是唯一知道依赖关系的编排点）。"""
        changed = set(changed_keys)
        if 'ffmpeg_path' in changed:
            _ffmpeg.configure(self.setting('ffmpeg_path') or '')
        if 'media_roots' not in changed:
            return
        old_root = self.paths.root_dir
        old_roots = list(self.paths.scan_roots)
        self.paths.rebind()
        engine = engine_for(self)
        if engine is not None:
            # 根集合变了：清掉去抖，让下一次进视图的被动同步立刻按新根重扫；
            # 被移除的根留下的条目由校验的差集删除清理（不再是幽灵条目）
            engine.reset_throttle()
        if self.paths.root_dir == old_root:
            # 只有**数据根**（第一行）变了才需要搬缓存：额外的扫描根增删不影响
            # 索引/进度/缩略图库的落点，重建一遍反而会把已有索引丢掉。
            if old_roots != self.paths.scan_roots:
                log.info(f'[MediaPlayer] 扫描根变更：{len(old_roots)} → '
                         f'{len(self.paths.scan_roots)} 个')
            return
        # 数据根变更：终止进行中的校验，索引与封面库跟着换落点。
        # **用户数据（歌单/喜欢/播放进度）不跟着走** —— 它落在壳数据根下（paths.state_file），
        # 换文件夹不该把用户攒的东西一起换掉。
        if engine is not None:
            engine.request_cancel()
        self.index.bind(self.paths.cache_file)
        self.index.load()
        self.state.load()
        self.covers.rebind()

    def on_unload(self) -> None:
        self.freshness.shutdown()
