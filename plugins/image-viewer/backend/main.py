"""image-viewer 插件后端入口：类骨架、初始化与 API 注册。

78 个方法按职责拆到 8 个 mixin 分片（namespace / thumbs / listing / albums /
file_ops / rebuild / freshness / settings），共享的模块级常量与纯函数在 common.py。
分片的方法体逐字未改，状态仍由本类的 __init__ 持有。

**mixin 必须排在 PluginBase 之前**：get_data_root / get_file_roots / ensure_thumb /
get_thumb_data / get_settings / on_settings_changed / on_unload / get_cache_dir /
freshness_spec 都是对基类的覆写，排在基类后面会被基类实现盖掉。
"""

import logging
import threading
import time
from pathlib import Path
from typing import Any, ClassVar, Dict, List

from shell.backend.media_catalog import list_subdirectories
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling
from shell.backend.thumb_cache import ThumbCache

log = logging.getLogger(__name__)

# common 里的共享定义：重新绑定成原来的名字，既有引用与测试都不用改
_common = load_sibling(__file__, 'common', 'image_viewer')
_pick_cover = _common.pick_cover

# 各职责分片
_namespace = load_sibling(__file__, 'namespace', 'image_viewer')
_thumbs = load_sibling(__file__, 'thumbs', 'image_viewer')
_listing = load_sibling(__file__, 'listing', 'image_viewer')
_albums = load_sibling(__file__, 'albums', 'image_viewer')
_file_ops = load_sibling(__file__, 'file_ops', 'image_viewer')
_rebuild = load_sibling(__file__, 'rebuild', 'image_viewer')
_freshness = load_sibling(__file__, 'freshness', 'image_viewer')
_settings = load_sibling(__file__, 'settings', 'image_viewer')


class ImageViewerPlugin(
    _namespace.NamespaceMixin,
    _thumbs.ThumbMixin,
    _listing.ListingMixin,
    _albums.AlbumMixin,
    _file_ops.FileOpsMixin,
    _rebuild.RebuildMixin,
    _freshness.FreshnessMixin,
    _settings.SettingsMixin,
    PluginBase,
):
    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        # root_dir / extra_roots 决定 `/file`、`/thumbs` 的允许根，因此改动它们
        # 等于改动本机可读/可删/可移的文件范围 —— 声明 admin_only，由
        # PluginBase.save_settings / update_setting 统一判定（见 docs/plugin-guide.md §8.2）。
        {"key": "root_dir", "label": "数据根目录", "type": "text", "admin_only": True,
         "placeholder": "默认: ./data", "help": "图片浏览的数据根目录（改动需管理员）"},
        {"key": "extra_roots", "label": "额外图片目录", "type": "textarea", "admin_only": True,
         "placeholder": "每行一个目录（也可在插件设置面板里增删）",
         "help": "与主根目录一起浏览：每个目录在相册树里显示为顶层节点（改动需管理员）"},
        {"key": "row_height", "label": "图片行高", "type": "range",
         "min": 100, "max": 400, "default": 200, "help": "Justified 布局的每行目标高度"},
        {"key": "per_page", "label": "每页图片数", "type": "number",
         "min": 10, "max": 200, "default": 40},
        {"key": "lightbox_cache", "label": "图片缓存", "type": "range",
         "min": 0, "max": 64, "default": 8,
         "help": "缓存最近看过的图，再打开时直接显示，不用等它一行行刷出来。"
                 "数值越大越流畅，占内存也越多；0 = 关闭"},
        {"key": "sort_by", "label": "排序方式", "type": "select",
         "default": "mtime",
         "options": [{"label": "修改时间", "value": "mtime"},
                     {"label": "文件名", "value": "name"},
                     {"label": "Pixiv 排序支持", "value": "time_name"}]},
        {"key": "sort_order", "label": "排序方向", "type": "select",
         "default": "desc",
         "options": [{"label": "倒序", "value": "desc"}, {"label": "正序", "value": "asc"}]},
        {"key": "pixiv_fuzzy", "label": "模糊匹配", "type": "checkbox",
         "default": False,
         "help": "仅「Pixiv 排序支持」生效：作品名不以前导数字开头时按"
                 "「先数字再文字」排序，并让该文件夹套用 Pixiv 的浏览效果"},
        {"key": "album_sort_by", "label": "作者视图排序方式", "type": "select",
         "default": "mtime",
         "options": [{"label": "更新时间", "value": "mtime"},
                     {"label": "文件名", "value": "name"},
                     {"label": "图片数量", "value": "count"}]},
        {"key": "album_sort_order", "label": "作者视图排序方向", "type": "select",
         "default": "desc",
         "options": [{"label": "倒序", "value": "desc"}, {"label": "正序", "value": "asc"}]},
    ]

    # ===== 常量 =====
    _ALBUMS_TTL = 30.0            # list_albums 全树扫描结果缓存秒数
    # 全树目录枚举（os.walk）结果的缓存秒数：机械盘上 9167 个目录要 6s，而它换来的
    # 只是"有哪些目录 + 各自 mtime"。索引文件里的目录级 mtime 缓存已经能挡住重复
    # 扫描，这里挡住的是枚举本身 —— 30s 一过就重走全树，等于每次进相册页都可能
    # 卡 6s。目录增删由统一刷新基建的被动同步（目录 mtime + 条目数）发现，它会走
    # `_invalidate_albums_cache()` 立刻作废本缓存，因此这里可以放宽到 10 分钟。
    _DIRS_TTL = 600.0
    # 相册索引缓存版本：封面挑选规则变更时必须 +1，否则旧缓存会让新规则不生效
    # （目录 mtime 没变 → 增量扫描直接复用旧的 direct_cover）。
    # 4：Pixiv 树封面 = 作品号最大的那张（画师最近的作品）
    # 3：封面 = 文件名自然序第一张（p0）
    # 2：封面 = 最新 mtime 的那张
    _ALBUM_CACHE_VERSION = 4
    _MAX_LIST_CACHE = 200         # 列表缓存最大条目数，超出后淘汰最旧一半
    _MAX_ALL_IMAGES = 5000        # 连续浏览序列最大返回条数，超出截断
    _SCAN_WORKERS = 8             # 扫描/尺寸读取并行线程数

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        # 必须显式写 `PluginBase.get_data_root(self)`，不能用 `super().get_data_root()`：
        # 本类的 MRO 以 mixin 开头，`super()` 命中的是 `ThumbMixin.get_data_root`，而它
        # 读的正是这一行要算的 `self.root_dir`。后果不是"回退值不对"，而是**插件整体
        # 加载失败**：`AttributeError: 'ImageViewerPlugin' object has no attribute
        # 'root_dir'`，并且声明 `dependencies: ["image-viewer"]` 的 pixiv-sync 一起挂掉。
        # 只在**空白设置**下触发（`self.setting('root_dir')` 为空才会走到 `or` 右边），
        # 因此开发机上被 `.config/plugins/image-viewer.json` 里的历史 root_dir 掩盖，
        # 新装用户则必然踩到。守卫用例：tests/test_image_viewer_multi_root.py 的
        # `test_constructs_without_any_settings` 与
        # tests/test_multi_instance_mesh.py 的 `test_fresh_instance_loads_all_plugins`。
        root = self.setting('root_dir') or str(PluginBase.get_data_root(self))
        self.root_dir = Path(root).resolve()
        self._rebuild_paths()
        self._meta_cache = self._load_meta()
        self._meta_dirty = False          # 尺寸元数据是否有新增条目需要落盘
        self._list_cache = {}
        self._album_config = self._load_album_config()
        self._album_cache = self._load_album_cache()
        self._dirs_cache = None           # _list_album_dirs 的全树枚举结果（含 mtime）
        self._dirs_cached_at = 0.0
        self._dirs_roots = None           # 上面那份快照对应的根集合指纹
        self._dirs_paths = None           # 上面那份快照对应的根路径（换库判别）
        # 先读落盘的全树目录快照：有了它，"打开相册页"不必等任何走盘（见 albums.py）。
        # 读不到（首次运行 / 换过根 / 校验清过）才需要走一趟。
        self._dirs_cache = self._load_dirs_index()
        if self._dirs_cache:
            self._dirs_cached_at = time.time()
            self._dirs_roots = self._roots_fingerprint()
            self._dirs_paths = tuple(str(p) for p in self._roots())
        self._dirs_lock = threading.Lock()  # 保护走盘结果的"检查 + 赋值"
        self._dirs_prewarm = None         # 预热线程句柄（默认不起，见下）
        self._albums_cached_at = 0.0      # list_albums 结果 TTL 缓存
        self._albums_cached = None
        # 诊断计数（`diagnose()`）：全树走盘发生了几次、各花多久、list_albums 被调几次
        self._dirs_walk_count = 0
        self._dirs_walk_ms = 0.0
        self._albums_calls = 0
        self._albums_last_ms = 0.0
        self._albums_stage_ms = {}        # 最近一次 list_albums 各阶段耗时（诊断用）
        self._last_build_scans = 0        # 最近一次索引重建真正重扫的目录数
        self._last_build_changed = 0
        self._scan_miss_count = 0         # _scan_dir_cached 的累计未命中次数
        self._build_breakdown = {}        # _build_albums 内部分段耗时（诊断用）
        # 设置来源缓存（见 settings.py 的 `_settings_source`）：`get_settings` 会被
        # 全树每个目录调用一次，不缓存时每次都读一遍设置文件
        self._settings_source_cache = None
        self._settings_by_path = {}       # get_settings 结果缓存（按目录）
        self._pixiv_flags = None
        # 缩略图缓存（共享基建）与全量重建后台任务（共享基建）
        self.thumb_cache = ThumbCache(self.thumb_db_path)
        self._rebuild = None              # BackgroundTask | None
        # 这里**不再**自动预热全树目录枚举：目录快照已经落盘（`dirs_index.json`），
        # `list_albums` 读它就能直接出结果（实测 0.08s），不必开局就占着机械盘走一趟。
        # 目录增删由统一刷新基建的被动同步发现（它一发现就 `_invalidate_albums_cache()`，
        # 那时才真的重走）。`_resolved_config['dirs_prewarm'] = True` 可显式打开预热。
        if self._resolved_config.get('dirs_prewarm'):
            self.prewarm_album_dirs()

    def _pixiv_mode(self, rel_path: str = '') -> bool:
        """该目录生效的排序是否为「Pixiv 排序支持」（含逐级继承）。

        封面与排序的 Pixiv 规则只由排序方式决定，模糊匹配不参与：它只把
        显式勾选它的目录认定为 Pixiv 树的配置点（见 `get_settings` 的
        `pixiv_explicit`），让「作者/作品名/序号.jpg」这类非数字命名的目录
        也拿到两层浏览效果。

        结果按目录缓存（`_pixiv_flags`）：`_build_albums` 会对全树每个目录问一次，
        未缓存时每次都要走一遍 `get_settings` 的继承链与设置来源查询 —— 9168 个
        目录实测 3.3s，而它只要一个布尔值。
        """
        flags = self._pixiv_flags
        if flags is None:
            flags = self._pixiv_flags = {}
        cached = flags.get(rel_path)
        if cached is not None:
            return cached
        value = bool(self.get_settings(rel_path).get('sort_by') == 'time_name')
        flags[rel_path] = value
        return value

    def _pixiv_fuzzy_mode(self, rel_path: str = '') -> bool:
        """该目录是否在「Pixiv 排序支持」下还勾了「模糊匹配」。"""
        s = self.get_settings(rel_path)
        return s.get('sort_by') == 'time_name' and bool(s.get('pixiv_fuzzy'))

    def diagnose(self, method: str = '') -> dict:
        """诊断钩子（也经 `register_api` 暴露成 `image-viewer__diagnose`）：报缓存与走盘状态。

        为什么要有它：相册的慢取决于**这个进程里**的缓存状态 —— 目录快照是否读到、
        有没有发生过全树走盘、索引重建重扫了几个目录。这些离线复现不出来（同一份
        代码在探针脚本里 0.1s、在真实壳进程里 8-25s），只能问活着的实例。最关键的
        三个数是 `album_dirs_walk_count`（走盘次数，非 0 即"又在重扫全树"）、
        `list_albums_stages_ms`（分阶段耗时）与 `last_build_scans`（重建重扫目录数）。
        """
        return {
            'method': method,
            'root_dir': str(self.root_dir),
            'roots': [str(p) for p in self._roots()],
            'dirs_cache_file': str(self.dirs_cache_file),
            'dirs_cache_file_exists': self.dirs_cache_file.exists(),
            'dirs_cache_entries': len(self._dirs_cache or {}),
            'dirs_snapshot_usable': self._dirs_snapshot(time.time()) is not None,
            'roots_fingerprint': repr(self._roots_fingerprint()),
            'dirs_paths': list(self._dirs_paths or []),
            'album_index_entries': len(self._album_cache.get('dirs') or {}),
            'album_index_version': self._album_cache.get('version'),
            'expected_version': self._ALBUM_CACHE_VERSION,
            'list_cache_entries': len(self._list_cache),
            'meta_entries': len(self._meta_cache),
            'album_dirs_walk_count': self._dirs_walk_count,
            'album_dirs_walk_ms': round(self._dirs_walk_ms, 1),
            'list_albums_calls': self._albums_calls,
            'list_albums_last_ms': round(self._albums_last_ms, 1),
            'list_albums_stages_ms': dict(self._albums_stage_ms),
            'last_build_scans': self._last_build_scans,
            'build_breakdown': dict(self._build_breakdown),
            'settings_by_path': len(self._settings_by_path),
            'albums_cached': self._albums_cached is not None,
        }

    def register_api(self) -> dict:
        return {
            'list_images': self.list_images,
            'list_folder_items': self.list_folder_items,
            'load_album_tiles': self.load_album_tiles,
            'list_album_images': self.list_album_images,
            'list_dir': self.list_dir,
            'list_albums': self.list_albums,
            'create_folder': self.create_folder,
            'get_album_config': self.get_album_config,
            'set_album_config': self.set_album_config,
            'get_image_info': self.get_image_info,
            'delete_files': self.delete_files,
            'move_files': self.move_files,
            'regenerate_thumbs': self.regenerate_thumbs,
            'refresh': self.refresh,
            'rebuild_all': self.rebuild_all,
            'rebuild_folder': self.rebuild_folder,
            'rebuild_status': self.rebuild_status,
            'rebuild_cancel': self.rebuild_cancel,
            'get_settings': self.get_settings,
            'save_settings': self.save_folder_settings,
            'get_root_dir': self.get_root_dir,
            'diagnose': self.diagnose,
            'clear_folder_settings': self.clear_folder_settings,
            'list_roots': self.list_roots,
            'browse_dir': self.browse_dir,
            'delete_folder': self.delete_folder,
        }

    def get_root_dir(self) -> str:
        return str(self.root_dir)

    def list_roots(self) -> List[Dict]:
        """全部根目录（第一根在前），供设置页管理多文件夹，也供附属插件按根遍历。

        `namespace` 是额外根的命名空间 token（第一根为空串）；`prefix` 是拼虚拟路径时
        直接用的前缀（第一根为空串，额外根形如 `__额外图库`）。**附属插件用 `prefix`**：
        把 `<prefix>/<根内相对路径>` 交给 Shell 的 /file、/thumbs 与 `delete_files()` 即可，
        不必自己认 `__` 记号，也不必复刻命名空间的取名规则（重名加序号，见 namespace.py）。
        """
        roots = []
        for index, root in enumerate(self._roots()):
            roots.append({
                'path': str(root),
                'label': root.name or str(root),
                'is_primary': index == 0,
                'exists': root.is_dir(),
                'namespace': '' if index == 0 else self._namespace(root),
                'prefix': self._virtual_path(root, ''),
            })
        return roots

    def browse_dir(self, path: str = '') -> Dict:
        """浏览本机目录（绝对路径），供设置页添加额外图片目录。

        目录枚举与「有哪些媒体类型」的判断走共享基建
        `shell.backend.media_catalog`（`list_subdirectories` / `find_kinds`），
        所以这里能同时标出含图片 / 视频 / 音乐的目录；空路径是「我的电脑」层。
        """
        result = list_subdirectories(path)
        if result.get('error'):
            return result
        for entry in result.get('entries', []):
            entry['is_image_dir'] = 'image' in (entry.get('kinds') or [])
        return result
