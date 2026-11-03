"""子目录瓦片与连续浏览序列（分级加载的第二、三级）。

为什么与分页列表分开：一次列目录要在**首屏**只读一层，而"容器子目录的封面、递归图片
数、以及灯箱要的整段序列"是另一件事 —— 它们要读整棵子树，代价高到必须推迟到前端真的
需要时才做（进入视口的瓦片、打开灯箱）。把这段单独成组件，列表就不必为了首屏流畅去
跳过自己代码里的分支。

三条加载级别（与前端契约一致）：

- 首屏 `list_folder_items` 只读当前目录一层，容器子目录返回 `pending=True` 的占位卡片；
- `load_album_tiles` 按视口批量补齐封面与递归计数（每批十几个目录）；
- `list_album_images` 在打开灯箱时才展开整段序列，并给出"点到某个子目录"的偏移。

状态自持：无长期状态（缓存都在相册索引里），因此构造参数就是它的依赖清单。
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.media_catalog import natural_sort_key

log = logging.getLogger(__name__)


class AlbumTiles:
    """子目录卡片、瓦片补齐与灯箱序列。"""

    def __init__(self, paths: Any, entries: Any, settings: Any, thumbs: Any,
                 naming: Any, extensions: Any, limits: Any) -> None:
        self._paths = paths
        self._entries = entries
        self._settings = settings
        self._thumbs = thumbs
        self._naming = naming
        self._extensions = extensions
        self._limits = limits

    # ===== 子目录扫描 =====

    def scan_items(self, rel_path: str, sub_dirs: List[str],
                   aggregate: bool = False) -> Tuple[List[Dict], Dict[str, List[Dict]]]:
        """扫描每个直接子目录（并行）。

        返回 (卡片列表, {sub_rel: [直接图片 dict 列表]})。
        卡片封面 = p0（文件名自然序第一张）；纯容器子目录（无直接图片但有子文件夹）
        用递归聚合：封面 = 最新作品 p0、total_count = 递归总图片数；
        image_count 始终为直接图片数（连续浏览序列按它展开）。

        `aggregate=False` 时容器子目录在索引未命中就只回 `pending=True` 的占位卡片，
        封面与 total_count 由 `load_album_tiles` 按批补齐。
        """
        results: Dict[str, Tuple] = {}
        names = sorted(sub_dirs)
        with ThreadPoolExecutor(max_workers=self._limits.scan_workers) as executor:
            futures = {executor.submit(self._scan_one, rel_path, name, aggregate): name
                       for name in names}
            for future, name in futures.items():
                try:
                    out = future.result()
                except Exception:
                    out = None
                if out is not None:
                    results[name] = out
        cards: List[Dict] = []
        album_images: Dict[str, List[Dict]] = {}
        for name in names:
            if name not in results:
                continue
            sub_rel, card, images = results[name]
            cards.append(card)
            album_images[sub_rel] = images
        return cards, album_images

    def _scan_one(self, rel_path: str, name: str, aggregate: bool = False):
        """扫描单个直接子目录（供线程池并行调用），返回 (sub_rel, card, images)。

        纯容器子目录（作者文件夹等）的递归计数与封面要从整棵子树聚合：

        - 索引命中（`children_total` 能直接复用相册索引算好的结果）→ 立即给出；
        - 索引未命中且 `aggregate=False`（首屏路径）→ 标 `pending` 交给
          `load_album_tiles` 按需补，首屏因此不必为了看不见的角标读整棵子树；
        - 索引未命中且 `aggregate=True`（连续浏览序列需要展开图片）→ 当场算一次并
          回写缓存，第二次列表就是命中。
        """
        sub_rel = f'{rel_path}/{name}' if rel_path else name
        # 命名空间节点（额外根目录的顶层）：没有直接图片，封面与总数递归聚合
        if self._paths.is_namespace_node(sub_rel):
            root, _ = self._paths.split_virtual(sub_rel)
            if root is None:
                return None
            try:
                mtime = root.stat().st_mtime
            except OSError:
                return None
            total, cover = self._entries.aggregate(root, sub_rel)
            cover_width = cover_height = 1
            if cover:
                cover_path, _ = self._paths.resolve_path(cover)
                try:
                    stat = cover_path.stat()
                    cover_width, cover_height = self._thumbs.get_image_size(
                        str(cover_path), stat.st_mtime)
                except (OSError, AttributeError):
                    pass
            card = {
                'type': 'album', 'path': sub_rel,
                'name': name[len(self._paths.marker):],
                'cover': cover, 'image_count': 0, 'total_count': total,
                'has_children': True, 'mtime': mtime,
                'width': cover_width or 1, 'height': cover_height or 1,
                'use_time_name': self._settings.pixiv_mode(sub_rel),
                'root_scope': True,
            }
            return sub_rel, card, []
        dir_path, _ = self._paths.resolve_dir(sub_rel)
        try:
            mtime = dir_path.stat().st_mtime
        except OSError:
            return None
        entry = self._entries.scan(sub_rel, dir_path, mtime)
        pixiv = entry.get('pixiv', False)
        direct_count = entry.get('direct_count', 0)
        cover = entry.get('direct_cover', '')
        has_children = bool(entry.get('has_children'))
        total_count: Optional[int] = direct_count
        # 纯容器子目录（作者文件夹等）：递归计数与封面要从子树里聚合。相册索引的全树
        # 聚合已经把结果写回同一份缓存（`total_mtime`），因此这里通常**直接命中**，
        # 不必再走子树；只有缓存确实是冷的（首启、或索引尚未建过）才把它标成 `pending`
        # 交给 `load_album_tiles` 按需补 —— 那条路径同样会回写缓存，第二次列表就命中。
        pending = False
        if has_children and not direct_count:
            cached_total = entry.get('total_count')
            cached_mtime = entry.get('total_mtime')
            # 容差与 `scan_dir_cached` / 全树聚合的 mtime 判定保持一致：同一份 mtime
            # 可能来自 `os.walk` 的 stat 与 `entry.stat()`，浮点上未必逐位相等
            if cached_total is not None and cached_mtime is not None \
                    and abs(float(cached_mtime) - float(mtime)) < 0.5:
                total_count, cover = cached_total, entry.get('total_cover', '') or ''
            elif aggregate:
                total_count, cover = self._entries.children_total(
                    sub_rel, dir_path, mtime, entry)
            else:
                pending = True
                total_count = None          # None = 待补；0 是"确实是空相册"，要能区分

        cover_width, cover_height = 1, 1
        images: List[Dict] = []
        try:
            with os.scandir(dir_path) as entries:
                for item in entries:
                    if item.name.startswith('.') or item.name == '.cache':
                        continue
                    if item.is_file() and Path(item.name).suffix.lower() in self._extensions:
                        stat = item.stat()
                        url = (Path(sub_rel) / item.name).as_posix()
                        width, height = self._thumbs.get_image_size(item.path, stat.st_mtime)
                        images.append({'url': url, 'mtime': stat.st_mtime,
                                       'width': width, 'height': height})
                        if url == cover:
                            cover_width, cover_height = width, height
        except OSError:
            pass
        if cover and (cover_width, cover_height) == (1, 1):
            abs_path, _ = self._paths.resolve_path(cover)
            try:
                stat = abs_path.stat()
                cover_width, cover_height = self._thumbs.get_image_size(
                    str(abs_path), stat.st_mtime)
            except (OSError, AttributeError):
                pass
        card = {
            'type': 'album',
            'path': sub_rel,
            'name': name,
            'cover': cover,
            'image_count': direct_count,
            'total_count': total_count,
            'has_children': has_children,
            'pending': pending,
            'mtime': mtime,
            'width': cover_width or 1,
            'height': cover_height or 1,
            'use_time_name': pixiv,
            # 合成根（第一根的顶层）与额外根的命名空间节点同地位：前端把它当 Pixiv 树
            # 的配置点。普通作者目录不算，否则会被误判成配置点而少一层
            'root_scope': rel_path == '' and not name.startswith(self._paths.marker),
        }
        return sub_rel, card, images

    def resolve_pending(self, items: List[Dict]) -> List[Dict]:
        """就地把仍标着 `pending` 的瓦片按索引缓存补全，返回补全后的列表。

        列表缓存里的卡片可能是补全**之前**存下的（`load_album_tiles` 补齐后它自己
        不知道），所以缓存命中分支要走一遍这里。只查内存缓存、不碰磁盘，因此不会把
        要避免的子树扫描又带回来。
        """
        for item in items:
            if item.get('type') != 'album' or not item.get('pending'):
                continue
            cached = self._entries.cached_total(item['path'])
            if cached is None:
                continue
            total, cover = cached
            item['pending'] = False
            item['total_count'] = total
            if cover and not item.get('cover'):
                item['cover'] = cover
                card = self.card_cover_size(cover)
                if card != (1, 1):
                    item['width'], item['height'] = card
        return items

    def card_cover_size(self, cover_rel: str) -> Tuple[int, int]:
        """封面图的像素尺寸（尺寸元数据缓存命中时无 I/O）。"""
        cover_path, _ = self._paths.resolve_path(cover_rel)
        if cover_path is None:
            return 1, 1
        try:
            stat = cover_path.stat()
            width, height = self._thumbs.get_image_size(str(cover_path), stat.st_mtime)
        except (OSError, AttributeError):
            return 1, 1
        return (width or 1, height or 1)

    # ===== 二级加载：按批补齐瓦片 =====

    def load_album_tiles(self, paths: List[str]) -> Dict:
        """按批补齐相册瓦片：封面、递归图片数与封面尺寸（分级加载的第二级）。

        首屏（`list_folder_items`）只读当前目录一层，容器子目录以 `pending=True` 的
        占位卡片返回；前端对**进入视口**的瓦片调用本方法，每批十几个目录。已经算过的
        子树走 `children_total` 的缓存，重复请求不重扫磁盘。

        返回 `{'tiles': {path: {cover, total_count, image_count, width, height,
        use_time_name}}}`；`paths` 里的非法路径或不存在目录直接不出现在结果里。
        """
        tiles: Dict[str, Dict] = {}
        for rel in paths or []:
            if not isinstance(rel, str) or not self._paths.is_safe(rel):
                continue
            rel = rel.strip('/')
            if rel in tiles:
                continue
            total = 0
            direct = 0
            cover = ''
            mtime = 0.0
            pixiv = self._settings.pixiv_mode(rel)
            if self._paths.is_namespace_node(rel):
                root, _ = self._paths.split_virtual(rel)
                if root is None:
                    continue
                try:
                    mtime = root.stat().st_mtime
                except OSError:
                    continue
                total, cover = self._entries.aggregate(root, rel)
            else:
                dir_path, _ = self._paths.resolve_dir(rel)
                if dir_path is None or not dir_path.is_dir():
                    continue
                try:
                    mtime = dir_path.stat().st_mtime
                except OSError:
                    continue
                entry = self._entries.scan(rel, dir_path, mtime)
                direct = entry.get('direct_count', 0)
                if direct:
                    total, cover = direct, entry.get('direct_cover', '')
                else:
                    total, cover = self._entries.children_total(rel, dir_path, mtime, entry)
            width = height = 1
            if cover:
                cover_path, _ = self._paths.resolve_path(cover)
                try:
                    stat = cover_path.stat()
                    width, height = self._thumbs.get_image_size(str(cover_path), stat.st_mtime)
                except (OSError, AttributeError):
                    pass
            tiles[rel] = {
                'path': rel,
                'cover': cover,
                'total_count': total,
                'image_count': direct,
                'width': width or 1,
                'height': height or 1,
                'use_time_name': pixiv,
                'mtime': mtime,
            }
        self._thumbs.flush()
        return {'tiles': tiles}

    # ===== 三级加载：整段连续浏览序列 =====

    def list_album_images(self, rel_path: str = '') -> Dict:
        """一个目录（含其直接子目录）的连续浏览序列，供灯箱左右翻看（分级加载第三级）。

        `list_folder_items` 出于首屏速度不再展开容器子目录的图片（展开要读整棵子树），
        这里把那段展开单独提供出来，前端在**打开灯箱**时才取。返回
        `{'images': [{url, width, height}], 'offset': {子目录路径: 在序列中的起点},
        'capped': 是否命中上限}`。

        `offset` 是给"点到某个子目录的瓦片"用的：那一下要打开到该子目录第一张，而不是
        整个序列的开头。
        """
        if not self._paths.is_safe(rel_path):
            return {'images': [], 'offset': {}, 'capped': False}
        target_dir, _ = self._paths.resolve_dir(rel_path)
        if target_dir is None or not target_dir.is_dir():
            return {'images': [], 'offset': {}, 'capped': False}

        sub_dirs: List[str] = []
        images: List[Dict] = []
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
        if not rel_path:
            sub_dirs.extend(self._paths.namespace_nodes())

        self._thumbs.fill_image_sizes(images)
        for image in images:
            image['url'] = (Path(rel_path) / image['name']).as_posix()
            image['width'] = image.pop('width')
            image['height'] = image.pop('height')

        # 这里要真展开图片，所以 aggregate=True（代价只付在打开灯箱这一下）
        cards, album_images = self.scan_items(rel_path, sub_dirs, aggregate=True)
        sort_by = self._settings.for_path(rel_path).get('sort_by') or 'mtime'
        images.sort(key=lambda item: natural_sort_key(Path(item['url']).name))
        cards.sort(key=lambda item: natural_sort_key(item['name']))

        sequence: List[Dict] = []
        offset: Dict[str, int] = {}
        for card in cards:
            offset[card['path']] = len(sequence)
            rep = self.card_cover_size(card['cover']) if card['cover'] else (1, 1)
            sub_settings = self._settings.for_path(card['path'])
            sub_sort = sub_settings.get('sort_by') or sort_by
            sub_reverse = (sub_settings.get('sort_order') or 'desc') == 'desc'
            sub_images = sorted(album_images.get(card['path'], []),
                                key=self._naming.image_sort_key(sub_sort),
                                reverse=(sub_sort != 'time_name' and sub_reverse))
            if card.get('has_children') and not sub_images:
                # 容器子目录：位置锚在它自己的代表封面（画师最近的作品 p0）。整棵子树
                # 不展开 —— 那正是列表时被推迟的工作，这里只为"点得到"提供一个入口，
                # 翻到尽头后灯箱停在它上面。
                if card['cover']:
                    sequence.append({'url': card['cover'],
                                     'width': rep[0], 'height': rep[1]})
            else:
                for sub_image in sub_images:
                    sequence.append({'url': sub_image['url'],
                                     'width': sub_image['width'],
                                     'height': sub_image['height']})
        for image in images:
            sequence.append({'url': image['url'], 'width': image['width'],
                             'height': image['height']})
        self._thumbs.flush()
        sequence, capped = self._limits.limit_all_images(sequence)
        return {'images': sequence, 'offset': offset, 'capped': capped}
