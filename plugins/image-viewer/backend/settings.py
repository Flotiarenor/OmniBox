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

    def get_settings(self, rel_path: str = '') -> Dict:
        """获取文件夹生效设置（含全局回退与逐级继承）。

        文件夹设置按「当前文件夹 → 父文件夹 → … → 全局 → 硬默认」逐级
        向上继承：在父文件夹（如 pixiv）上启用 time_name 后，其下所有子文件夹
        自动继承同一排序；某个子文件夹被单独修改（folders[该路径] 存在）时
        以它自己的设置优先，并继续向其子文件夹传播。

        `pixiv_explicit`：当前文件夹自身是否显式启用了 Pixiv 树（自己存了
        「Pixiv 排序支持」或「模糊匹配」）。它是 Pixiv 树的配置点——该层显示
        作者网格，继承它的子层才显示瀑布流；所以对「作者/作品名/序号.jpg」
        这类非数字命名的目录，勾一次模糊匹配就够，不必每个子目录再配一遍。
        """
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
        stored = {}
        if self._settings_store:
            raw = self._settings_store.get(self.name)
            if isinstance(raw, dict):
                stored.update(raw)
        if isinstance(self._resolved_config, dict):
            for key, value in self._resolved_config.items():
                stored.setdefault(key, value)
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

    def clear_folder_settings(self, rel_path: str) -> Dict:
        """删除指定文件夹的独立设置，使其回退到全局设置"""
        folders = self.setting('folders') or {}
        if isinstance(folders, dict) and rel_path in folders:
            del folders[rel_path]
            self.update_setting('folders', folders)
            self._invalidate_albums_cache()
        return {"success": True}
