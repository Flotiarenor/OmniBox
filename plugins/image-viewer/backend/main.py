"""image-viewer 插件入口：API 表、壳钩子与装配。

本文件只做三件事：装配组件、声明对壳暴露的 API、把壳按名调用的钩子一行委派给组件。
设置项 schema 与常量在 `const.py`，业务逻辑在各组件里（分层门禁硬上限 250 行，目标是
200 行以内）。

接缝说明：`load_sibling` 返回 `ModuleType`，装配出来的组件属性在静态检查里是 `Any`。这是
有意的取舍 —— 需要检查的东西都在组件里；为让接缝有类型而塞进 `sys.path` 会换来模块名冲突。

装配顺序就是依赖顺序：路径 → 设置 → 缩略图 → 扫描 → 目录快照 → 可见性配置 → 目录条目
→ 漫画书范围 → 相册索引 → 最近图片 → 瓦片 → 列表 → 文件操作 / 重建 / 刷新；反向依赖一律
由构造参数注入。
`get_data_root` 直接返回 `paths.root`：已无 mixin 覆写基类方法。
"""

from pathlib import Path
from typing import Any, ClassVar, Dict, List

from shell.backend.freshness import engine_for
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

_const = load_sibling(__file__, 'const', 'image_viewer')
_naming = load_sibling(__file__, 'naming', 'image_viewer')
_paths = load_sibling(__file__, 'paths', 'image_viewer')
_settings = load_sibling(__file__, 'settings', 'image_viewer')
_thumbs = load_sibling(__file__, 'thumbs', 'image_viewer')
_scanner = load_sibling(__file__, 'scanner', 'image_viewer')
_entries = load_sibling(__file__, 'entries', 'image_viewer')
_dirs = load_sibling(__file__, 'dirs', 'image_viewer')
_album_config = load_sibling(__file__, 'album_config', 'image_viewer')
_albums = load_sibling(__file__, 'albums', 'image_viewer')
_books = load_sibling(__file__, 'books', 'image_viewer')
_recent = load_sibling(__file__, 'recent', 'image_viewer')
_tiles = load_sibling(__file__, 'tiles', 'image_viewer')
_listing = load_sibling(__file__, 'listing', 'image_viewer')
_file_ops = load_sibling(__file__, 'file_ops', 'image_viewer')
_rebuild = load_sibling(__file__, 'rebuild', 'image_viewer')
_freshness = load_sibling(__file__, 'freshness', 'image_viewer')


class ImageViewerPlugin(PluginBase):
    # 设置项声明是纯数据，放在 const.py（入口只做装配与委派）；限权说明随声明一起。
    settings_schema: ClassVar[List[Dict[str, Any]]] = _const.SETTINGS_SCHEMA

    def __init__(self, manifest: dict, config: dict) -> None:
        super().__init__(manifest, config)
        limits = _const.LIMITS
        extensions = _const.IMAGE_EXTENSIONS
        # 先算根、再建组件：其余路径全部从 PathLayout 派生；显式用基类方法名，不依赖 MRO。
        root = Path(str(self.setting('root_dir')
                        or PluginBase.get_data_root(self))).resolve()
        self.limits = limits
        self.paths = _paths.PathLayout(root, self, extensions)
        self.naming = _naming.PixivNaming()
        self.settings = _settings.SettingsResolver(
            self.paths, self, self, self._read_raw_settings,
            self.settings_schema, _const.HARD_DEFAULTS, self._invalidate_albums)
        self.thumbs = _thumbs.ThumbStore(self.paths, limits.scan_workers)
        self.scanner = _scanner.DirScanner(self.paths, self.naming, extensions)
        self.dirs_snapshot = _dirs.DirSnapshot(self.paths, limits)
        self.album_config = _album_config.AlbumConfig(self.paths, self._invalidate_albums)
        self.entries = _entries.DirEntryCache(
            self.paths, self.scanner, self.settings, self.naming)
        # 漫画书的边界由漫画阅读插件给（manifest 的 dependencies 声明这条联动）：
        # 组件只拿一个读取函数，插件不在 / 未配置时它返回 None，时间线整体退化成普通图片。
        self.books = _books.MangaBooks(self.paths, self._manga_root, extensions)
        self.albums = _albums.AlbumIndex(
            self.paths, self.dirs_snapshot, self.entries, self.settings,
            self.album_config, self.naming, limits, self.books)
        self.recent = _recent.RecentImages(
            self.paths, self.albums, self.thumbs, self.naming, extensions, limits,
            self.books)
        self.tiles = _tiles.AlbumTiles(
            self.paths, self.entries, self.settings, self.thumbs, self.naming,
            extensions, limits)
        self.listing = _listing.Listing(
            self.paths, self.tiles, self.settings, self.thumbs,
            self.naming, extensions, limits)
        self.file_ops = _file_ops.FileOps(
            self.paths, self.thumbs, self.album_config, self.albums, self.listing,
            self.is_protected_path, extensions)
        self.rebuild = _rebuild.RebuildTask(
            self.paths, self.thumbs, self.albums, self.listing, extensions)
        self.freshness = _freshness.FreshnessProvider(
            self.paths, self.thumbs, self.albums, self.entries, self.listing,
            extensions, limits, lambda: engine_for(self))
        if self._resolved_config.get('dirs_prewarm'):
            self.dirs_snapshot.prewarm()

    def register_api(self) -> dict:
        return {
            'list_images': self.listing.list_images,
            'list_folder_items': self.listing.list_folder_items,
            'load_album_tiles': self.tiles.load_album_tiles,
            'list_album_images': self.tiles.list_album_images,
            'list_dir': self.listing.list_dir,
            'list_albums': self.albums.list_albums,
            'create_folder': self.file_ops.create_folder,
            'get_album_config': self.album_config.get,
            'set_album_config': self.album_config.set_action,
            'get_image_info': self.thumbs.get_image_info,
            'delete_files': self.file_ops.delete_files,
            'move_files': self.file_ops.move_files,
            'regenerate_thumbs': self.file_ops.regenerate_thumbs,
            'refresh': self.freshness.refresh,
            'rebuild_all': self.rebuild.rebuild_all,
            'rebuild_folder': self.rebuild.rebuild_folder,
            'rebuild_status': self.rebuild.status,
            'rebuild_cancel': self.rebuild.cancel,
            'get_settings': self.settings.for_path,
            'save_settings': self.settings.save_folder,
            'get_root_dir': self.paths.get_root_dir,
            'diagnose': self.diagnose,
            'clear_folder_settings': self.settings.clear_folder,
            'list_roots': self.paths.list_roots,
            'browse_dir': self.paths.browse_dir,
            'delete_folder': self.file_ops.delete_folder,
            'list_recent_images': self.recent.list_recent_images,
        }

    # ===== 宿主协作者（组件通过闭包/端口拿到，不持有插件对象） =====

    def _manga_root(self) -> str | None:
        """漫画根（来自漫画阅读插件 `manga-library` 的设置）；读不到时返回 None。

        时间线按这根画出的边界把"一本书"合并成一张卡，因此它属于跨插件联动而不是本插件
        的设置项：漫画插件不在、未配置或根目录不在本插件的图片根里时返回 None，调用方
        退化成普通图片，不会用文件名去猜。`_resolved_config` 的 `manga_root` 预设供自检
        与用例在不起插件的情况下指定同一根。
        """
        preset = self._resolved_config.get('manga_root')
        if preset:
            return str(preset)
        dependency = self.get_dependency('manga-library')
        if dependency is None:
            return None
        try:
            settings = dependency.get_settings()
        except Exception:
            return None
        root = settings.get('root_dir') if isinstance(settings, dict) else None
        return str(root) if root else None

    def _read_raw_settings(self) -> Dict[str, Any]:
        """启动时预设 ∪ 当前设置文件（未脱敏、不含 schema 默认值，不能用 setting() 代替）。"""
        data: Dict[str, Any] = {}
        preset = self._resolved_config
        if isinstance(preset, dict):
            data.update(preset)
        store = self._settings_store
        stored = store.get(self.name) if store is not None else None
        if isinstance(stored, dict):
            data.update(stored)
        return data

    def _invalidate_albums(self) -> None:
        """派生数据失效的统一出口（设置写入、可见性变更都走这里）。"""
        self.albums.invalidate()

    # ===== 壳按名调用的钩子：名字与签名与迁移前完全一致 =====

    def get_data_root(self) -> Path:
        return self.paths.root

    def get_file_roots(self) -> List[Path]:
        return self.paths.roots()

    def resolve_file_path(self, rel_path: str):
        return self.paths.resolve_file_path(rel_path)

    def get_thumb_data(self, rel_path: str):
        return self.thumbs.get_thumb_data(rel_path)

    def ensure_thumb(self, rel_path: str) -> None:
        return self.thumbs.ensure_thumb(rel_path)

    def get_cache_dir(self) -> Path:
        return self.paths.cache_dir

    # ===== 附属插件按实例调用的契约（image-cleaner 拿额外根前缀、复用宿主的删除） =====

    def list_roots(self) -> List[Dict]:
        return self.paths.list_roots()

    def delete_files(self, rel_paths: List[str]) -> Dict:
        return self.file_ops.delete_files(rel_paths)

    def freshness_spec(self) -> Dict[str, Any]:
        return self.freshness.spec()

    def on_settings_changed(self, changed_keys) -> None:
        """设置变更：按改动范围重绑组件状态（入口是唯一知道依赖关系的编排点）。"""
        changed = set(changed_keys)
        self.books.invalidate()
        if _const.ROOT_KEYS & changed:
            rooted = 'root_dir' in changed
            new_dir = self.setting('root_dir')
            root = (Path(str(new_dir)).resolve()
                    if rooted and new_dir and Path(str(new_dir)).is_dir()
                    else self.paths.root)
            self.paths.rebind(root)
            if rooted:
                # 缓存/缩略图库/命名空间都挂在第一根下，换根后全部重来
                self.thumbs.rebind()
            self.albums.reset_index()
            self.listing.clear_cache()
            self.albums.invalidate()
        if _const.SORT_KEYS & changed:
            self.listing.clear_cache()
            self.albums.invalidate()
        self.settings.invalidate()
        engine = getattr(self, '_freshness_engine', None)
        if engine is not None:
            engine.reset_throttle()

    def on_unload(self) -> None:
        self.rebuild.shutdown()

    def diagnose(self, method: str = '') -> dict:
        """诊断钩子：报缓存与走盘状态（走盘次数、各阶段耗时、重建重扫目录数）。"""
        report: Dict[str, Any] = {
            'method': method,
            'root_dir': str(self.paths.root),
            'roots': [str(path) for path in self.paths.roots()],
        }
        report.update(self.dirs_snapshot.diagnostics())
        report['list_cache_entries'] = self.listing.cache_entries
        report['meta_entries'] = self.thumbs.meta_entries
        report.update(self.albums.diagnostics())
        report['settings_by_path'] = self.settings.stats['settings_by_path']
        return report
