"""统一刷新基建接入（ImageViewerPlugin 的一个 mixin 分片）。

本分片把相册插件接进壳的「同步 / 校验」基建（`shell/backend/freshness.py`）：
壳负责去抖、单飞、增量短路、幽灵清理、进度与状态展示，这里只回答三个问题
——管哪些文件、怎么算指纹、哪些条目需要重活。

对外的两个动作（用户可见名字与全壳统一）：

- **同步**：被动、增量。目录 mtime + 纳入索引的名字个数没变就整目录跳过；
  变了才失效该目录的列表缓存与相册索引（相册插件的派生数据本来就是**惰性**
  重算的，所以"重活"在这里就是"丢掉旧结果"，下一帧按需重算）。
- **校验**：手动、全量。逐项 stat 校对指纹、清掉消失条目的缩略图与尺寸缓存、
  并按有效键集合对账缩略图库的孤儿行。

为什么值得接：旧实现里「刷新」只清三个内存缓存（`refresh()`，见 file_ops.py），
既不清缩略图孤儿、也没有"删除/移动后失效相册缓存"这条（`delete_files` /
`move_files` 只清 `_list_cache`），而 `_ALBUM_CACHE_VERSION` 这种"改规则必须手动
+1"的约定一旦忘记就是静默失效。接入后这些判据由壳统一托管。
"""

import logging
from pathlib import Path
from typing import Any, Dict

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'image_viewer')
ALLOWED_EXTENSIONS = _common.ALLOWED_EXTENSIONS
drop_image_meta = _common.drop_image_meta

class FreshnessMixin:
    """与壳的统一刷新基建对接（声明 + 三个钩子）。"""

    def get_cache_dir(self) -> Path:
        """缓存目录 = 本插件既有的 `<数据根>/.cache`。

        默认实现会给成 `<数据根>/.cache/image-viewer`，但相册插件的缩略图库 /
        尺寸元数据 / 相册索引一直平铺在 `.cache` 下，指纹库跟着它们放才不会
        出现"两份缓存目录"，换根时也只换一处（`_rebuild_paths`）。
        """
        return self.cache_dir

    def freshness_spec(self) -> Dict[str, Any]:
        """声明本插件的新鲜度规格（字段语义见 shell/backend/freshness.py）。"""
        return {
            # 可调用：额外图片目录能在运行时增删，引擎是长期缓存的对象
            'roots': self._roots,
            # 前缀也必须跟着现算：命名空间 token 会因重名加序号，写死会与
            # `_virtual_path()` 的结果不一致（缩略图库的键就全对不上了）
            'prefixes': lambda: tuple(self._virtual_path(root, '') for root in self._roots()),
            'include': tuple(sorted(ALLOWED_EXTENSIONS)),
            'derive': self._freshness_derive,
            'prune': self._freshness_prune,
            'on_verified': self._freshness_audit,
            'rebuild': self._freshness_rebuild,
            # 封面挑选规则变更时的整体失效信号：替代原先"记得手动 +1"的
            # `_ALBUM_CACHE_VERSION`（忘记加就是静默用旧封面）
            'content_version': self._ALBUM_CACHE_VERSION,
            # 目录不大（一个相册几十张）且"图片被替换"必须立刻反映到相册索引，
            # 因此逐条 stat；大媒体库的插件应保持默认 False。
            'stat_entries': True,
            'unit': '张',
            'min_sync_interval': 5.0,
            'max_dirs_per_sync': 400,
        }

    # ===== 钩子（全部由壳在它自己的线程里调用，只做缓存失效，不做重活） =====

    def _freshness_derive(self, items) -> Dict[str, Any]:
        """条目新增/变化：丢掉列表缓存与相册索引，让下一帧按需重算。

        相册的派生数据（图片列表、封面、递归计数）本来就是**惰性**重算的，所以
        这里不需要真的去算 —— 把旧结果丢掉，读盘时自然是最新的。这也让"同步"
        在稳态下几乎零成本。

        不做"只丢受影响目录"的精细失效：父目录的 `all_images` 连续浏览序列里
        嵌着子目录的图片，只清子目录会留下父目录的陈旧序列（`_list_cache` 的键
        在 `list_images` 与 `list_folder_items` 里形状还不一样）。总量上限 200 条，
        整表清空更简单也更不容易错。
        """
        self._list_cache.clear()
        # 目录集合变了会影响封面聚合：整个相册索引作废（它自己会逐目录按 mtime
        # 复用，不作废反而会留着已消失目录的条目）
        self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}
        self._invalidate_albums_cache()
        return {'count': len(items)}

    def _freshness_prune(self, keys) -> int:
        """条目消失：删缩略图行、丢尺寸元数据。返回处理条数。"""
        pruned = 0
        for rel in keys:
            try:
                self.thumb_cache.delete(rel)
                abs_path, _ = self._resolve_path(rel)
                drop_image_meta(self._meta_cache, str(abs_path or (self.root_dir / rel)))
                pruned += 1
            except Exception as e:
                log.warning(f'[ImageViewer] 清理失效条目失败 {rel}: {e}')
        if pruned:
            self._meta_dirty = True
            self._flush_meta_if_dirty()
        return pruned

    def _freshness_audit(self, keys) -> Dict[str, Any]:
        """全量校验后的对账：按"当前有效图片键集合"清缩略图库里的孤儿行。

        这是旧实现缺的那一环：只有插件内的删除会 `thumb_cache.delete(rel)`，
        在资源管理器 / pixiv-sync 里删掉的图片会永久留在 `thumbs.db` 里，而
        `refresh()` 不 prune、`rebuild_all()` 只能把整个库清空重来。
        """
        valid = {str(k) for k in keys}
        removed_rows = self.thumb_cache.prune(valid)
        # 相册索引同理：不存在的目录条目由壳的目录级清理负责，这里只处理条目
        return {'thumb_rows_removed': removed_rows, 'valid_entries': len(valid)}

    def _freshness_rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃派生缓存（缩略图库 + 尺寸元数据 + 相册索引）。

        **不预生成**缩略图：`/thumbs` 会按需生成（见 albums.py 的说明：预生成
        上万次随机小文件 I/O 得不偿失）。需要"一次生成整库"的场景仍走
        `rebuild_all()`（它保留给预生成用途，不再挂在工具栏上）。
        """
        cleared: Dict[str, Any] = {'kind': kind}
        try:
            self.thumb_cache.clear()
            cleared['thumbs'] = True
        except Exception as e:
            cleared['thumbs_error'] = str(e)
        self._meta_cache = {}
        self._meta_dirty = False
        try:
            if self.meta_file.exists():
                self.meta_file.unlink()
        except OSError as e:
            cleared['meta_error'] = str(e)
        self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}
        self._list_cache.clear()
        self._invalidate_albums_cache()
        try:
            if self.album_cache_file.exists():
                self.album_cache_file.unlink()
        except OSError as e:
            cleared['album_error'] = str(e)
        return cleared
