"""按目录的设置读写（ImageViewerPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 ImageViewerPlugin.__init__ 持有 ——
分片只把方法挂到同一个类上，因此方法与调用点都没有变。
"""

import logging
from pathlib import Path
from typing import Dict

from shell.backend.thumb_cache import ThumbCache

log = logging.getLogger(__name__)

class SettingsMixin:
    """按目录的设置读写。"""

    def _settings_source(self) -> dict:
        """本插件的原始设置字典（带缓存）。

        **必须缓存**：`SettingsStore.get()` 每次都 `open() + json.load()` 读盘（无任何
        缓存），而 `get_settings()` 一条路径就要查它三次。`_build_albums` 会对全树
        每个目录调一次 `_pixiv_mode()` → `get_settings()`：11 万张 / 9168 个目录的
        库上实测 7.4 秒，其中大部分就是这 9168×3 次读盘。

        失效由写入路径负责（`update_setting` / `save_settings` 覆写），因此缓存不会
        看到过期值：本插件的设置只可能经这两处落盘。
        """
        cached = self._settings_source_cache
        if cached is not None:
            return cached
        data: dict = {}
        if isinstance(self._resolved_config, dict):
            # 启动时的已解决设置先铺底 …
            data.update(self._resolved_config)
        if self._settings_store:
            # … 再用**当前**设置文件覆盖：文件是权威来源。反过来（setdefault）会让
            # 进程启动时的那份快照永久盖住磁盘上的新值 —— 表现为"改了全局排序，
            # 界面和索引算出来的 Pixiv 标记对不上"。
            raw = self._settings_store.get(self.name)
            if isinstance(raw, dict):
                data.update(raw)
        self._settings_source_cache = data
        return data

    def _invalidate_settings_cache(self) -> None:
        """设置变了 → 按路径缓存的 `get_settings` 结果与 Pixiv 标记整份作废。"""
        self._settings_source_cache = None
        self._settings_by_path = {}
        self._pixiv_flags = None

    def update_setting(self, key: str, value) -> bool:
        ok = super().update_setting(key, value)
        self._invalidate_settings_cache()
        return ok

    def save_settings(self, settings: Dict | None = None) -> Dict:
        result = super().save_settings(settings)
        self._invalidate_settings_cache()
        return result

    def _folders_snapshot(self) -> Dict:
        """`folders` 设置（按目录的设置树）。与 `_settings_source` 同一份缓存来源。"""
        folders = self._settings_source().get('folders') or {}
        return folders if isinstance(folders, dict) else {}

    def compute_pixiv_flags(self, paths) -> Dict[str, bool]:
        """一次算出一批目录的"是否 Pixiv 树"，**不逐个查设置**。

        同一份答案用 `_pixiv_mode()` 逐个问的话，每个目录都要走一遍 `get_settings`
        的继承链与设置来源查询：9168 个目录实测 2.7-3.4 秒（设置来源缓存首次填充
        时每次都要读设置文件）。这里改成自顶向下算一遍：

        - 排序只由「当前目录 → 父目录 → … → 全局」里**最近一个显式设置了 sort_by
          的层**决定（`get_settings` 的继承语义），因此按路径长度升序处理，父层算过
          子层就能直接继承；
        - 只对"自己设置了 sort_by"的目录取设置（本机配置里是 5 个），其余全是字典
          查表。

        结果同时写进 `_pixiv_flags`，后续 `_pixiv_mode()` 直接命中。
        """
        folders = self._folders_snapshot()
        global_sort = self._global_sort_by()
        # 有效排序字符串的传递表：**必须存字符串而不是布尔值** —— 拿布尔值当
        # "有效排序"往下传，子层比较 `True == 'time_name'` 会恒为 False，于是
        # 继承链在第二层就断掉（曾经写错过一次，表现为 9027 个目录里只认出 3 个）。
        effective_sort: Dict[str, str] = {}
        scope = set(str(p) for p in paths)
        scope.update(str(rel) for rel in folders)
        for rel in sorted(scope, key=lambda p: (p.count('/'), p)):
            own = folders.get(rel)
            if isinstance(own, dict) and own.get('sort_by'):
                effective = str(own['sort_by'])
            else:
                effective = effective_sort.get(rel.rpartition('/')[0], global_sort)
            effective_sort[rel] = effective
        self._pixiv_flags = {rel: (value == 'time_name')
                             for rel, value in effective_sort.items()}
        return self._pixiv_flags

    def _global_sort_by(self) -> str:
        """全局生效的排序方式（`get_settings('')` 的 sort_by）。"""
        stored = self._settings_source()
        value = stored.get('sort_by')
        if value:
            return str(value)
        legacy = self._folders_snapshot().get('__global__')
        if isinstance(legacy, dict) and legacy.get('sort_by'):
            return str(legacy['sort_by'])
        for item in self.settings_schema:
            if item.get('key') == 'sort_by':
                return str(item.get('default') or 'mtime')
        return 'mtime'

    def get_settings(self, rel_path: str = '') -> Dict:
        """获取文件夹生效设置（含全局回退与逐级继承）。

        结果按 `rel_path` 缓存（见 `_settings_by_path`）：这条路径在"列一次目录"
        与"重建全树索引"里都是每个目录调一次，未缓存时 9168 次纯 Python 字典合并
        实测 3.4s。缓存随设置写入与索引变更一起失效（`_invalidate_settings_cache`）。

        文件夹设置按「当前文件夹 → 父文件夹 → … → 全局 → 硬默认」逐级
        向上继承：在父文件夹（如 pixiv）上启用 time_name 后，其下所有子文件夹
        自动继承同一排序；某个子文件夹被单独修改（folders[该路径] 存在）时
        以它自己的设置优先，并继续向其子文件夹传播。

        `pixiv_explicit`：当前文件夹自身是否显式启用了 Pixiv 树（自己存了
        「Pixiv 排序支持」或「模糊匹配」）。它是 Pixiv 树的配置点——该层显示
        作者网格，继承它的子层才显示瀑布流；所以对「作者/作品名/序号.jpg」
        这类非数字命名的目录，勾一次模糊匹配就够，不必每个子目录再配一遍。
        """
        hit = self._settings_by_path.get(rel_path)
        if hit is not None:
            return dict(hit)          # 拷贝：调用方可能改返回值（如补 root_dir）
        result = self._compute_settings(rel_path)
        self._settings_by_path[rel_path] = result
        return dict(result)

    def _compute_settings(self, rel_path: str) -> Dict:
        """`get_settings` 的计算本体（结果会被缓存，见上）。"""
        folders = self.setting('folders') or {}
        if not isinstance(folders, dict):
            folders = {}
        # 全局设置有两个来源：
        #   1. 插件级键（row_height / per_page / sort_by / …）——壳设置面板与
        #      save_folder_settings('') 走 PluginBase.save_settings() 写入，是当前写入路径；
        #   2. folders['__global__']——更早的兼容位置，当前代码不再写入。
        # 历史缺陷：这里只读 2，于是"保存到全局"当场生效、重启后静默回落到硬默认值。
        # 两者都读，但**以当前写入路径为准**：只把真正存过的插件级键（设置存储或
        # 启动时预设里有这个键）算进来，不能用 self.setting() 的 schema 默认值去
        # 覆盖 __global__，否则老配置里的全局值会被默认值悄悄顶掉。
        # 走带缓存的口取（见 `_settings_source`）：这条路径会被全树每个目录调用一次，
        # 未缓存时每次都要读一遍设置文件。
        stored = dict(self._settings_source())
        global_settings = {}
        legacy = folders.get('__global__')
        if isinstance(legacy, dict):
            global_settings.update(legacy)
        for item in self.settings_schema:
            key = item.get('key')
            if not key or key == 'root_dir':
                continue
            if key in stored and stored[key] is not None:
                global_settings[key] = stored[key]
        hard_defaults = {
            "row_height": 200,
            "per_page": 40,
            "sort_by": "mtime",
            "sort_order": "desc",
            "pixiv_fuzzy": False,
            "album_sort_by": "mtime",
            "album_sort_order": "desc"
        }
        folder_settings = {}
        # 命名空间节点（额外根目录的顶层）与第一根的合成根同地位：它没有自己的
        # 文件夹级设置，直接吃全局值，成为该根下作者层的继承源头。
        parts = [] if self._is_namespace_node(rel_path) \
            else [p for p in (rel_path or '').split('/') if p]
        for i in range(len(parts), 0, -1):
            key = '/'.join(parts[:i])
            if key in folders:
                folder_settings = folders[key]
                break
        result = {**hard_defaults, **global_settings, **folder_settings}
        result['root_dir'] = str(self.root_dir)
        # 配置点 = 当前文件夹自身显式启用了 Pixiv 树（自己存了排序方式或
        # 模糊匹配）。继承来的不算：所以「在作者目录上勾模糊匹配」就足以把
        # 该层变成作者网格，而它下面的作品层继承后仍走瀑布流。
        own_entry = folders.get(rel_path or '__global__', {})
        result['pixiv_explicit'] = bool(own_entry.get('pixiv_fuzzy')) \
            or own_entry.get('sort_by') == 'time_name'
        return result

    def save_folder_settings(self, rel_path: str = '', settings: Dict | None = None) -> Dict:
        """前端 save_settings 的 API 实现。

        兼容两种调用：
        - save_folder_settings(settings_dict)      → 保存全局
        - save_folder_settings(rel_path, settings) → 保存指定文件夹
        """
        if settings is None:
            if isinstance(rel_path, dict):
                settings = rel_path
                rel_path = ''
            else:
                settings = {}
        if rel_path:
            # 文件夹级设置只保存视图/排序偏好；root_dir 是全局设置，不能写入某个文件夹，
            # 否则会造成“看起来保存了但根目录没变”的困惑。
            folder_settings = dict(settings or {})
            folder_settings.pop('root_dir', None)
            folders = self.setting('folders') or {}
            if not isinstance(folders, dict):
                folders = {}
            folders[rel_path] = folder_settings
            self.update_setting('folders', folders)
            self._invalidate_albums_cache()
            return {"success": True}
        result = super().save_settings(settings)
        if result.get('success'):
            self._invalidate_albums_cache()
        return result

    def on_settings_changed(self, changed_keys):
        tracked = {'root_dir', 'extra_roots'}
        if tracked & set(changed_keys):
            rooted = 'root_dir' in changed_keys
            new_dir = self.setting('root_dir')
            if rooted and new_dir and Path(new_dir).is_dir():
                self.root_dir = Path(new_dir).resolve()
            self._rebuild_paths()
            if rooted:
                # 缓存/缩略图库/命名空间都挂在第一根下，换根后全部重来
                self.thumb_cache = ThumbCache(self.thumb_db_path)
                self._meta_cache = self._load_meta()
            self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}
            self._list_cache.clear()
            self._invalidate_albums_cache()
        # 排序/封面规则变更（壳设置面板走的是 PluginBase.save_settings，不经过
        # save_folder_settings）也要立即生效，而不是等 30s TTL 过期
        if {'sort_by', 'sort_order', 'pixiv_fuzzy',
                'album_sort_by', 'album_sort_order'} & set(changed_keys):
            self._list_cache.clear()
            self._invalidate_albums_cache()
        # 统一刷新基建：清了去抖时间戳，下一次进视图的被动同步会立刻真扫
        # （否则刚改完根目录要等 min_sync_interval 秒才生效）
        engine = getattr(self, '_freshness_engine', None)
        if engine is not None:
            engine.reset_throttle()

    def clear_folder_settings(self, rel_path: str) -> Dict:
        """删除指定文件夹的独立设置，使其回退到全局设置"""
        folders = self.setting('folders') or {}
        if isinstance(folders, dict) and rel_path in folders:
            del folders[rel_path]
            self.update_setting('folders', folders)
            self._invalidate_albums_cache()
        return {"success": True}
