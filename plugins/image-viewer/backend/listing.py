"""单个目录的分页列表（图片列表 / 混合列表 / 目录树）。

状态自持：列表缓存在这里。它的键在两种列表里形状不同（`list_images` 与
`list_folder_items` 各一套），失效判据是目录 mtime；谁写入谁负责清（删除、移动、
改设置、被动同步都调 `clear_cache()`）。

首屏只读当前目录一层：容器子目录的递归计数与封面由 `tiles.AlbumTiles` 按需补，
因此这里拿到的是 `pending=True` 的占位卡片。
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List

from shell.backend.media_catalog import natural_sort_key

log = logging.getLogger(__name__)


class Listing:
    """目录内的分页列表与列表缓存。"""

    def __init__(self, paths: Any, tiles: Any, settings: Any, thumbs: Any,
                 naming: Any, extensions: Any, limits: Any) -> None:
        self._paths = paths
        self._tiles = tiles
        self._settings = settings
        self._thumbs = thumbs
        self._naming = naming
        self._extensions = extensions
        self._limits = limits
        self._list_cache: Dict[Any, tuple] = {}

    # ===== 列表缓存 =====

    def clear_cache(self) -> None:
        """清空列表缓存（删除/移动/改设置/被动同步后调用）。"""
        self._list_cache.clear()

    def _cache(self, key: Any, value: tuple) -> None:
        """写入列表缓存；超过上限时淘汰最旧一半，防止长时间使用内存膨胀。"""
        self._list_cache[key] = value
        if len(self._list_cache) > self._limits.max_list_cache:
            for stale in list(self._list_cache)[:self._limits.max_list_cache // 2]:
                del self._list_cache[stale]

    @property
    def cache_entries(self) -> int:
        """诊断用：列表缓存条目数。"""
        return len(self._list_cache)

    # ===== 图片列表 =====

    def list_images(self, rel_path: str = '', page: int = 1,
                    per_page: int = 40, sort_by: str = 'mtime',
                    sort_order: str = 'desc') -> Dict:
        if not self._paths.is_safe(rel_path):
            return {'images': [], 'page': 1, 'total': 0, 'settings': {}}
        try:
            page = max(1, int(page))
            per_page = min(200, max(1, int(per_page)))
        except (TypeError, ValueError):
            page, per_page = 1, 40

        cache_key = (rel_path, sort_by, sort_order)
        dir_mtime = self._thumbs.dir_mtime(rel_path)

        if cache_key in self._list_cache:
            cached_mtime, cached_images = self._list_cache[cache_key]
            if cached_mtime == dir_mtime:
                total = len(cached_images)
                start = (page - 1) * per_page
                end = start + per_page
                return {
                    'images': cached_images[start:end],
                    'page': page,
                    'total': total,
                    'has_next': end < total,
                    'has_prev': page > 1,
                    'settings': self._settings.for_path(rel_path),
                }

        target_dir, _ = self._paths.resolve_dir(rel_path)
        images: List[Dict] = []
        try:
            with os.scandir(target_dir) as entries:
                for entry in entries:
                    if entry.is_file() \
                            and Path(entry.name).suffix.lower() in self._extensions:
                        stat = entry.stat()
                        images.append({
                            'path': entry.path,
                            'name': entry.name,
                            'mtime': stat.st_mtime,
                            'size': stat.st_size,
                        })
        except (FileNotFoundError, TypeError, AttributeError):
            pass

        # 并行读取尺寸：首次扫描大文件夹时 Pillow 打开文件是主要开销
        self._thumbs.fill_image_sizes(images)

        for image in images:
            url_path = (Path(rel_path) / image['name']).as_posix()
            image.update({'url': url_path, 'width': image.pop('width'),
                          'height': image.pop('height')})

        reverse = (sort_order == 'desc')
        if sort_by == 'name':
            images.sort(key=lambda item: natural_sort_key(Path(item['url']).name),
                        reverse=reverse)
        elif sort_by == 'time_name':
            # 新标准「时间+文件名」：图片内部一律按文件名自然序（p0 → p1），
            # 时间维度只作用于作品/相册卡片之间的顶层排序
            images.sort(key=lambda item: natural_sort_key(Path(item['url']).name))
        else:
            images.sort(key=lambda item: item['mtime'], reverse=reverse)

        self._cache(cache_key, (dir_mtime, images))
        self._thumbs.flush()

        total = len(images)
        start = (page - 1) * per_page
        end = start + per_page
        return {
            'images': images[start:end],
            'page': page,
            'total': total,
            'has_next': end < total,
            'has_prev': page > 1,
            'settings': self._settings.for_path(rel_path),
        }

    # ===== 混合列表 =====

    def list_folder_items(self, rel_path: str = '', page: int = 1,
                          per_page: int = 40, sort_by: str = 'name',
                          sort_order: str = 'asc') -> Dict:
        """混合列表：直接图片 + 直接子相册 p0 瓦片（只处理一层嵌套），瀑布流统一展示。

        子文件夹以 p0 瓦片返回（封面 = p0，time_name 模式下带圆圈数量角标），单图以
        image 返回。`all_images` 为按瀑布流顺序展开的连续浏览序列：每个子文件夹的图片
        （p0 → p1 → …）按瀑布流里它自己的位置展开，供灯箱向右连续翻看画师的其他作品。

        **首屏只读当前目录一层**：没有直接图片、只有子目录的容器目录以 `pending=True`
        的占位卡片返回（`cover` 空、`total_count` 为 0），其封面与递归计数由前端对进入
        视口的瓦片调用 `load_album_tiles` 补齐。返回的 `pending_count` 是本页待补瓦片
        数，`sequence_pending` 表示 `all_images` 因为跳过了这些容器而不完整。
        """
        if not self._paths.is_safe(rel_path):
            return {'items': [], 'all_images': [], 'page': 1, 'total': 0, 'settings': {}}
        try:
            page = max(1, int(page))
            per_page = min(200, max(1, int(per_page)))
        except (TypeError, ValueError):
            page, per_page = 1, 40

        cache_key = ('items', rel_path, sort_by, sort_order)
        dir_mtime = self._thumbs.dir_mtime(rel_path)
        if cache_key in self._list_cache:
            cached = self._list_cache[cache_key]
            if cached[0] == dir_mtime:
                cached_items = cached[1]
                cached_all = cached[2] if len(cached) > 2 else None
                total = len(cached_items)
                start = (page - 1) * per_page
                end = start + per_page
                image_total = sum(1 for item in cached_items if item.get('type') == 'image')
                all_offset = self._all_images_offset(cached_items, start)
                all_images, truncated = self._limits.limit_all_images(
                    cached_all if cached_all is not None
                    else [item for item in cached_items if item.get('type') == 'image'])
                page_items = self._tiles.resolve_pending(cached_items[start:end])
                return {
                    'items': page_items,
                    'all_images': all_images,
                    'all_truncated': truncated,
                    'all_offset': all_offset,
                    'page': page, 'total': total, 'image_total': image_total,
                    'pending_count': sum(1 for item in page_items if item.get('pending')),
                    'sequence_pending': any(
                        item.get('has_children') and item.get('image_count') == 0
                        for item in cached_items),
                    'has_next': end < total, 'has_prev': page > 1,
                    'settings': self._settings.for_path(rel_path),
                }

        target_dir, _ = self._paths.resolve_dir(rel_path)
        sub_dirs: List[str] = []
        images: List[Dict] = []
        if target_dir is not None:
            try:
                with os.scandir(target_dir) as entries:
                    for entry in entries:
                        if entry.name.startswith('.') or entry.name == '.cache':
                            continue
                        if entry.is_dir():
                            sub_dirs.append(entry.name)
                        elif entry.is_file() \
                                and Path(entry.name).suffix.lower() in self._extensions:
                            stat = entry.stat()
                            images.append({
                                'type': 'image', 'path': entry.path, 'name': entry.name,
                                'mtime': stat.st_mtime, 'size': stat.st_size,
                            })
            except OSError:
                pass
        # 相册树的合成根节点：额外根目录在这里以命名空间卡片的形式出现（要用完整虚拟
        # 路径 `__<token>`，不能用裸 token —— 那会被当成第一根下的同名物理目录去扫描，
        # 结果直接消失）
        if not rel_path:
            sub_dirs.extend(self._paths.namespace_nodes())

        # 并行读取直接图片尺寸（首次扫描大文件夹时的主要开销）
        self._thumbs.fill_image_sizes(images)
        for image in images:
            image['url'] = (Path(rel_path) / image['name']).as_posix()
            image['width'] = image.pop('width')
            image['height'] = image.pop('height')

        # 是否当场展开容器子目录的图片：现在一律不展开（见下面的说明），
        # `aggregate=False` 让容器瓦片在索引未命中时以 `pending` 占位返回。
        cards, album_images = self._tiles.scan_items(rel_path, sub_dirs, aggregate=False)

        items: List[Dict] = []
        reverse = (sort_order == 'desc')
        if sort_by == 'time_name':
            # Pixiv 排序支持：顶层作品/单图按 pixiv 数字号（前导数字）排序，方向生效
            # （倒序 = 大号在前 = 新作品在前），无数字名排最后；模糊匹配时同号/无号条目
            # 改为整名自然序（先数字再文字），让「日期+标题」也能排对。多图作品（子文件夹
            # 瓦片）与单图（平铺文件）排在**同一个**序列里，否则就是「先所有文件夹、再
            # 所有单图」两段，与作品号无关。当前目录自身是作品时，里面是页序号：恒按
            # 文件名自然序 p0 → p1，方向不参与（倒序不翻转作品内部的页序）。
            fuzzy = self._settings.pixiv_fuzzy_mode(rel_path)
            entries = cards + images
            if self._naming.is_work_dir(rel_path, entries, lambda item: item['name'],
                                        has_cards=bool(cards)):
                items = sorted(entries, key=lambda item: natural_sort_key(item['name']))
            else:
                items = self._naming.sort(entries, lambda item: item['name'],
                                          reverse, fuzzy)
        else:
            if sort_by == 'mtime':
                cards.sort(key=lambda item: item['mtime'], reverse=reverse)
                images.sort(key=lambda item: item['mtime'], reverse=reverse)
            else:
                cards.sort(key=lambda item: natural_sort_key(item['name']), reverse=reverse)
                images.sort(key=lambda item: natural_sort_key(Path(item['url']).name),
                            reverse=reverse)
            items = cards + images

        # 连续浏览序列只含**已经扫过的**图片：单图 + 非容器子目录的图片。容器子目录
        # （作者文件夹等）不展开 —— 展开要读整棵子树，实测某个 40978 张的容器目录单是
        # 列出外层目录就要 3.3s。序列不完整由 `sequence_pending` 告知，前端在打开灯箱
        # 时才调 `list_album_images` 取完整序列并就地替换。
        pending_dirs = [item['path'] for item in items
                        if item.get('has_children') and item.get('image_count') == 0]
        all_images: List[Dict] = []
        for item in items:
            if item['type'] == 'image':
                all_images.append({'url': item['url'], 'width': item['width'],
                                   'height': item['height']})
            else:
                sub_settings = self._settings.for_path(item['path'])
                sub_sort = sub_settings.get('sort_by') or sort_by
                sub_reverse = (sub_settings.get('sort_order') or 'desc') == 'desc'
                sub_images = sorted(album_images.get(item['path'], []),
                                    key=self._naming.image_sort_key(sub_sort),
                                    reverse=(sub_sort != 'time_name' and sub_reverse))
                for sub_image in sub_images:
                    all_images.append({'url': sub_image['url'],
                                       'width': sub_image['width'],
                                       'height': sub_image['height']})

        self._cache(cache_key, (dir_mtime, items, all_images))
        self._thumbs.flush()

        all_images, capped = self._limits.limit_all_images(all_images)
        total = len(items)
        start = (page - 1) * per_page
        end = start + per_page
        all_offset = self._all_images_offset(items, start)
        return {
            'items': items[start:end],
            'all_images': all_images,
            # 截断可能有两个原因，前端提示文案不同：命中条数上限（`all_capped`）或序列
            # 里有待补的容器瓦片（`sequence_pending`）
            'all_truncated': capped or bool(pending_dirs),
            'all_capped': capped,
            'all_offset': all_offset,
            'page': page, 'total': total,
            'image_total': len(images),
            # 本页返回的瓦片里还有哪些需要二级加载（前端按视口分批取封面与计数）；
            # 连续浏览序列也因此尚不完整，前端需要时再并入
            'pending_count': sum(1 for item in items[start:end] if item.get('pending')),
            'sequence_pending': bool(pending_dirs),
            'has_next': end < total, 'has_prev': page > 1,
            'settings': self._settings.for_path(rel_path),
        }

    def _all_images_offset(self, items: List[Dict], start: int) -> int:
        """当前页首项之前，连续浏览序列 all_images 中已有多少张图片。

        分页后前端用 items[0] 对应 all_images[all_offset]，
        避免点击第 2+ 页的瓦片时灯箱打开到序列开头的错误图片。
        """
        offset = 0
        for item in items[:start]:
            offset += item.get('image_count', 0) if item.get('type') == 'album' else 1
        return offset

    # ===== 目录树 =====

    def list_dir(self, rel_path: str = '') -> List[Dict]:
        """目录树浏览（移动目标选择等）：命名空间根展开为该根的子目录。"""
        if self._paths.is_namespace_node(rel_path):
            root, _ = self._paths.split_virtual(rel_path)
            if root is None:
                return []
            return [{'name': entry.name, 'path': self._paths.virtual_path(root, entry.name)}
                    for entry in sorted(root.iterdir(),
                                        key=lambda path: natural_sort_key(path.name))
                    if entry.is_dir() and not entry.name.startswith('.')]
        root, inner = self._paths.split_virtual(rel_path)
        if root is None:
            return []
        return self._paths.list_directory(root, inner)
