"""目录 / 图片列表与子目录聚合扫描（ImageViewerPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 ImageViewerPlugin.__init__ 持有 ——
分片只把方法挂到同一个类上，因此方法与调用点都没有变。
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'image_viewer')
NAMESPACE_MARKER = _common.NAMESPACE_MARKER
ALLOWED_EXTENSIONS = _common.ALLOWED_EXTENSIONS
list_directory = _common.list_directory
natural_sort_key = _common.natural_sort_key
_pixiv_sort = _common.pixiv_sort

class ListingMixin:
    """目录 / 图片列表与子目录聚合扫描。"""

    def list_images(self, rel_path: str = '', page: int = 1,
                    per_page: int = 40, sort_by: str = 'mtime',
                    sort_order: str = 'desc') -> Dict:
        if not self._is_safe(rel_path):
            return {"images": [], "page": 1, "total": 0, "settings": {}}
        try:
            page = max(1, int(page))
            per_page = min(200, max(1, int(per_page)))
        except (TypeError, ValueError):
            page, per_page = 1, 40

        cache_key = (rel_path, sort_by, sort_order)
        dir_mtime = self._get_dir_mtime(rel_path)

        if cache_key in self._list_cache:
            cached_mtime, cached_images = self._list_cache[cache_key]
            if cached_mtime == dir_mtime:
                total = len(cached_images)
                start = (page - 1) * per_page
                end = start + per_page
                return {
                    "images": cached_images[start:end],
                    "page": page,
                    "total": total,
                    "has_next": end < total,
                    "has_prev": page > 1,
                    "settings": self.get_settings(rel_path)
                }

        target_dir, _ = self._resolve_dir(rel_path)
        images = []
        try:
            with os.scandir(target_dir) as entries:
                for entry in entries:
                    if entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
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
        self._fill_image_sizes(images)

        for img in images:
            url_path = (Path(rel_path) / img['name']).as_posix()
            img.update({'url': url_path, 'width': img.pop('width'), 'height': img.pop('height')})

        reverse = (sort_order == 'desc')
        if sort_by == 'name':
            images.sort(key=lambda x: natural_sort_key(Path(x['url']).name), reverse=reverse)
        elif sort_by == 'time_name':
            # 新标准「时间+文件名」：图片内部一律按文件名自然序（p0 → p1），
            # 时间维度只作用于作品/相册卡片之间的顶层排序
            images.sort(key=lambda x: natural_sort_key(Path(x['url']).name))
        else:
            images.sort(key=lambda x: x['mtime'], reverse=reverse)

        self._cache_list(cache_key, (dir_mtime, images))
        self._flush_meta_if_dirty()

        total = len(images)
        start = (page - 1) * per_page
        end = start + per_page
        return {
            "images": images[start:end],
            "page": page,
            "total": total,
            "has_next": end < total,
            "has_prev": page > 1,
            "settings": self.get_settings(rel_path)
        }

    def _scan_dir_cached(self, rel_path: str, dir_path: Path, mtime: float) -> dict:
        """带 mtime 校验的目录直扫（进程内缓存，键 = 虚拟路径）。

        与 `_scan_dir_direct` 的唯一区别是"mtime 没变就复用上次结果"。缓存落在
        `_album_cache['dirs']`，与相册索引同一份，因此相册索引重建时也顺带复用它。
        """
        cache_dirs = self._album_cache.get('dirs')
        if not isinstance(cache_dirs, dict):
            cache_dirs = self._album_cache['dirs'] = {}
        pixiv = self._pixiv_mode(rel_path)
        cached = cache_dirs.get(rel_path)
        if cached and cached.get('mtime') is not None \
                and abs(float(cached.get('mtime', 0)) - float(mtime)) < 0.5 \
                and cached.get('pixiv') == pixiv:
            return cached
        entry = self._scan_dir_direct(dir_path, rel_path)
        entry['pixiv'] = pixiv
        entry['mtime'] = mtime
        cache_dirs[rel_path] = entry
        self._scan_miss_count += 1
        return entry

    def _children_total(self, rel_path: str, dir_path: Path, mtime: float,
                        entry: dict) -> tuple:
        """子目录聚合计数与封面，复用缓存条目，重复调用不重扫磁盘。

        返回 (递归图片总数, 代表封面)。命中条件是该目录自身的 mtime 与上次算聚合时
        完全一致（`total_mtime`）—— 相册索引的全树聚合与这里的按需聚合写的是同一
        组字段，因此两条路径互为缓存，重复列表只 stat 一层子目录，不再走整棵子树。

        递归统计的语义与旧实现一致：直接子目录有图片时取该目录的 p0；只有更深层
        才有图片就继续往下递归（否则「作者/作品/1.jpg」会被算成 0 张）；代表封面
        按 pixiv 号倒序取第一个非空封面（= 最近的作品）。
        """
        if entry.get('total_count') is not None and entry.get('total_mtime') == mtime:
            return entry.get('total_count', 0), entry.get('total_cover', '') or ''

        children = []
        try:
            with os.scandir(dir_path) as entries:
                for e in entries:
                    if e.name.startswith('.') or e.name == '.cache' or not e.is_dir():
                        continue
                    children.append(e.name)
        except OSError:
            children = list(entry.get('children') or [])

        sub_totals = {}
        for name in children:
            sub_rel = f"{rel_path}/{name}" if rel_path else name
            sub_path = dir_path / name
            try:
                sub_mtime = sub_path.stat().st_mtime
            except OSError:
                continue
            sub_entry = self._scan_dir_cached(sub_rel, sub_path, sub_mtime)
            count = sub_entry.get('direct_count', 0)
            cover = sub_entry.get('direct_cover', '')
            if not count:
                # 纯容器子目录：它自己的聚合缓存未失效时直接用，否则递归重算
                count, cover = self._children_total(sub_rel, sub_path, sub_mtime, sub_entry)
            sub_totals[name] = (count, cover, sub_mtime)

        named = _pixiv_sort([(n, sub_totals[n][1]) for n in sub_totals],
                            lambda t: t[0], reverse=True)
        total = sum(sub_totals[n][0] for n in sub_totals)
        cover = next((c for _n, c in named if c), '')
        entry['total_mtime'] = mtime
        entry['total_count'] = total
        entry['total_cover'] = cover
        return total, cover

    def _aggregate_children(self, dir_path: Path, rel_path: str) -> tuple:
        """递归统计目录下的图片总数与代表封面（含更深层的子目录）。

        走 `_children_total` 的缓存路径：目录 mtime 未变时直接复用上次结果（含
        相册索引全树聚合算出来的那一份），不再每列一次上级目录就把整棵子树重扫。
        """
        try:
            mtime = dir_path.stat().st_mtime
        except OSError:
            return 0, ''
        entry = self._scan_dir_cached(rel_path, dir_path, mtime)
        return self._children_total(rel_path, dir_path, mtime, entry)

    def _scan_one_subdir(self, rel_path: str, name: str, cache_dirs: dict,
                         aggregate: bool = False):
        """扫描单个直接子目录（供线程池并行调用），返回 (sub_rel, card, images)。

        纯容器子目录（作者文件夹等）的递归计数与封面要从整棵子树聚合：

        - 索引命中（`_children_total` 能直接复用相册索引算好的结果）→ 立即给出；
        - 索引未命中且 `aggregate=False`（首屏路径）→ 标 `pending` 交给
          `load_album_tiles` 按需补，首屏因此不必为了看不见的角标读整棵子树；
        - 索引未命中且 `aggregate=True`（连续浏览序列需要展开图片）→ 当场算一次
          并回写缓存，第二次列表就是命中。

        cache_dirs / _meta_cache 为进程内共享字典，GIL 下并发读写安全。
        """
        sub_rel = f"{rel_path}/{name}" if rel_path else name
        # 命名空间节点（额外根目录的顶层）：没有直接图片，封面与总数递归聚合
        if self._is_namespace_node(sub_rel):
            root, _ = self._split_virtual(sub_rel)
            try:
                mtime = root.stat().st_mtime
            except OSError:
                return None
            total, cover = self._aggregate_children(root, sub_rel)
            cw = ch = 1
            if cover:
                cover_path, _ = self._resolve_path(cover)
                try:
                    st = cover_path.stat()
                    cw, ch = self._get_image_size(str(cover_path), st.st_mtime)
                except (OSError, AttributeError):
                    pass
            card = {
                'type': 'album', 'path': sub_rel, 'name': name[len(NAMESPACE_MARKER):],
                'cover': cover, 'image_count': 0, 'total_count': total,
                'has_children': True, 'mtime': mtime,
                'width': cw or 1, 'height': ch or 1,
                'use_time_name': self._pixiv_mode(sub_rel),
                'root_scope': True,
            }
            return sub_rel, card, []
        dir_path, _ = self._resolve_dir(sub_rel)
        try:
            mtime = dir_path.stat().st_mtime
        except OSError:
            return None
        entry = self._scan_dir_cached(sub_rel, dir_path, mtime)
        pixiv = entry.get('pixiv', False)
        direct_count = entry.get('direct_count', 0)
        cover = entry.get('direct_cover', '')
        has_children = bool(entry.get('has_children'))
        total_count = direct_count
        # 纯容器子目录（作者文件夹等）：递归计数与封面要从子树里聚合。相册索引
        # 的全树聚合已经把结果写回同一份缓存（`total_mtime`），因此这里通常**直接
        # 命中**，不必再走子树；只有缓存确实是冷的（首启、或索引尚未建过）才把
        # 它标成 `pending` 交给 `load_album_tiles` 按需补 —— 那条路径同样会回写
        # 缓存，第二次列表就是命中了。
        pending = False
        if has_children and not direct_count:
            cached_total = entry.get('total_count')
            cached_mtime = entry.get('total_mtime')
            # 容差与 `_scan_dir_cached` / `_build_albums` 的 mtime 判定保持一致：
            # 同一份 mtime 可能来自 `os.walk` 的 stat 与 `entry.stat()`，浮点上
            # 未必逐位相等
            if cached_total is not None and cached_mtime is not None \
                    and abs(float(cached_mtime) - float(mtime)) < 0.5:
                total_count, cover = cached_total, entry.get('total_cover', '') or ''
            elif aggregate:
                total_count, cover = self._children_total(sub_rel, dir_path, mtime, entry)
            else:
                pending = True
                total_count = None          # None = 待补；0 是"确实是空相册"，要能区分

        cw, ch = 1, 1
        images = []
        try:
            with os.scandir(dir_path) as entries:
                for e in entries:
                    if e.name.startswith('.') or e.name == '.cache':
                        continue
                    if e.is_file() and Path(e.name).suffix.lower() in ALLOWED_EXTENSIONS:
                        st = e.stat()
                        url = (Path(sub_rel) / e.name).as_posix()
                        w, h = self._get_image_size(e.path, st.st_mtime)
                        images.append({'url': url, 'mtime': st.st_mtime,
                                       'width': w, 'height': h})
                        if url == cover:
                            cw, ch = w, h
        except OSError:
            pass
        if cover and (cw, ch) == (1, 1):
            abs_path, _ = self._resolve_path(cover)
            try:
                st = abs_path.stat()
                cw, ch = self._get_image_size(str(abs_path), st.st_mtime)
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
            'width': cw or 1,
            'height': ch or 1,
            'use_time_name': pixiv,
            # 合成根（第一根的顶层）与额外根的命名空间节点同地位：前端把它当
            # Pixiv 树的配置点。普通作者目录不算，否则会被误判成配置点而少一层
            'root_scope': rel_path == '' and not name.startswith(NAMESPACE_MARKER),
        }
        return sub_rel, card, images

    def _resolve_pending_items(self, items: List[Dict]) -> List[Dict]:
        """就地把仍标着 `pending` 的瓦片按索引缓存补全，返回补全后的列表。

        列表缓存里的卡片可能是补全**之前**存下的（`load_album_tiles` 补齐后它自己
        不知道），所以缓存命中分支要走一遍这里。只查内存缓存、不碰磁盘，因此不会
        把要避免的子树扫描又带回来。
        """
        for it in items:
            if it.get('type') != 'album' or not it.get('pending'):
                continue
            cached = self._children_total_cached(it['path'])
            if cached is None:
                continue
            total, cover = cached
            it['pending'] = False
            it['total_count'] = total
            if cover and not it.get('cover'):
                it['cover'] = cover
                card = self._card_cover_size(cover)
                if card != (1, 1):
                    it['width'], it['height'] = card
        return items

    def _card_cover_size(self, cover_rel: str) -> tuple:
        """封面图的像素尺寸（尺寸元数据缓存命中时无 I/O）。"""
        cover_path, _ = self._resolve_path(cover_rel)
        if cover_path is None:
            return 1, 1
        try:
            st = cover_path.stat()
            width, height = self._get_image_size(str(cover_path), st.st_mtime)
        except (OSError, AttributeError):
            return 1, 1
        return (width or 1, height or 1)

    def _children_total_cached(self, rel_path: str):
        """相册索引里本目录的聚合结果（总数, 封面）；未算过或已失效时返回 None。

        只查缓存、不碰磁盘：调用方用它决定"要不要当场展开子树"，因此这里绝不能
        触发扫描，否则等于把要避免的开销提前付掉。
        """
        entry = self._album_cache.get('dirs', {}).get(rel_path)
        if not isinstance(entry, dict):
            return None
        total = entry.get('total_count')
        cached_mtime = entry.get('total_mtime')
        if total is None or cached_mtime is None or entry.get('mtime') is None:
            return None
        if abs(float(cached_mtime) - float(entry.get('mtime'))) >= 0.5:
            return None
        return total, entry.get('total_cover', '') or ''

    def list_album_images(self, rel_path: str = '') -> Dict:
        """一个目录（含其直接子目录）的连续浏览序列，供灯箱左右翻看（分级加载第三级）。

        `list_folder_items` 出于首屏速度不再展开容器子目录的图片（展开要读整棵
        子树），这里把那段展开单独提供出来，前端在**打开灯箱**时才取。返回
        `{'images': [{url, width, height}], 'offset': {子目录路径: 在序列中的起点},
        'capped': 是否命中 `_MAX_ALL_IMAGES` 上限}`。

        `offset` 是给"点到某个子目录的瓦片"用的：那一下要打开到该子目录第一张，
        而不是整个序列的开头。
        """
        if not self._is_safe(rel_path):
            return {'images': [], 'offset': {}, 'capped': False}
        target_dir, _ = self._resolve_dir(rel_path)
        if target_dir is None or not target_dir.is_dir():
            return {'images': [], 'offset': {}, 'capped': False}

        sub_dirs = []
        images = []
        try:
            with os.scandir(target_dir) as entries:
                for entry in entries:
                    if entry.name.startswith('.') or entry.name == '.cache':
                        continue
                    if entry.is_dir():
                        sub_dirs.append(entry.name)
                    elif entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                        stat = entry.stat()
                        images.append({
                            'type': 'image', 'path': entry.path, 'name': entry.name,
                            'mtime': stat.st_mtime, 'size': stat.st_size,
                        })
        except OSError:
            pass
        if not rel_path:
            sub_dirs.extend(f'{NAMESPACE_MARKER}{token}' for token in self._roots_index())

        self._fill_image_sizes(images)
        for img in images:
            img['url'] = (Path(rel_path) / img['name']).as_posix()
            img['width'] = img.pop('width')
            img['height'] = img.pop('height')

        # 这里要真展开图片，所以 aggregate=True（代价只付在打开灯箱这一下）
        cards, album_images = self._scan_album_items(rel_path, sub_dirs, aggregate=True)
        sort_by = self.get_settings(rel_path).get('sort_by') or 'mtime'
        images.sort(key=lambda x: natural_sort_key(Path(x['url']).name))
        cards.sort(key=lambda x: natural_sort_key(x['name']))

        seq: List[Dict] = []
        offset: Dict[str, int] = {}
        for card in cards:
            offset[card['path']] = len(seq)
            rep = self._card_cover_size(card['cover']) if card['cover'] else (1, 1)
            sub_settings = self.get_settings(card['path'])
            sub_sort = sub_settings.get('sort_by') or sort_by
            sub_reverse = (sub_settings.get('sort_order') or 'desc') == 'desc'
            sub_imgs = sorted(album_images.get(card['path'], []),
                              key=self._item_image_sort_key(sub_sort),
                              reverse=(sub_sort != 'time_name' and sub_reverse))
            if card.get('has_children') and not sub_imgs:
                # 容器子目录：位置锚在它自己的代表封面（画师最近的作品 p0）。
                # 整棵子树不展开 —— 那正是列表时被推迟的工作，这里只为"点得到"
                # 提供一个入口，翻到尽头后灯箱停在它上面。
                if card['cover']:
                    seq.append({'url': card['cover'], 'width': rep[0], 'height': rep[1]})
            else:
                for si in sub_imgs:
                    seq.append({'url': si['url'], 'width': si['width'], 'height': si['height']})
        for img in images:
            seq.append({'url': img['url'], 'width': img['width'], 'height': img['height']})
        self._flush_meta_if_dirty()
        capped = len(seq) > self._MAX_ALL_IMAGES
        return {'images': seq[:self._MAX_ALL_IMAGES], 'offset': offset, 'capped': capped}

    def _scan_album_items(self, rel_path: str, sub_dirs: List[str],
                          aggregate: bool = False) -> tuple:
        """扫描每个直接子目录（并行）。

        返回 (卡片列表, {sub_rel: [直接图片 dict 列表]})。
        卡片封面 = p0（文件名自然序第一张）；纯容器子目录（无直接图片但有子文件夹）
        用递归聚合：封面 = 最新作品 p0、total_count = 递归总图片数；
        image_count 始终为直接图片数（连续浏览序列按它展开）。

        `aggregate=False` 时容器子目录在索引未命中就只回 `pending=True` 的占位卡片，
        封面与 total_count 由 `load_album_tiles` 按批补齐（见 `_scan_one_subdir`）。
        """
        cache_dirs = self._album_cache.get('dirs')
        if not isinstance(cache_dirs, dict):
            cache_dirs = self._album_cache['dirs'] = {}
        results = {}
        names = sorted(sub_dirs)
        with ThreadPoolExecutor(max_workers=self._SCAN_WORKERS) as ex:
            futures = {ex.submit(self._scan_one_subdir, rel_path, name,
                                 cache_dirs, aggregate): name
                       for name in names}
            for fut, name in futures.items():
                try:
                    out = fut.result()
                except Exception:
                    out = None
                if out is not None:
                    results[name] = out
        cards = []
        album_images = {}
        for name in names:
            if name not in results:
                continue
            sub_rel, card, images = results[name]
            cards.append(card)
            album_images[sub_rel] = images
        return cards, album_images

    def load_album_tiles(self, paths: List[str]) -> Dict:
        """按批补齐相册瓦片：封面、递归图片数与封面尺寸（分级加载的第二级）。

        首屏（`list_folder_items`）只读当前目录一层，容器子目录以 `pending=True`
        的占位卡片返回；前端对**进入视口**的瓦片调用本方法，每批十几个目录。
        已经算过的子树走 `_children_total` 的缓存，重复请求不重扫磁盘。

        返回 `{'tiles': {path: {cover, total_count, image_count, width, height,
        use_time_name}}}`；`paths` 里的非法路径或不存在目录直接不出现在结果里。
        """
        tiles = {}
        for rel in paths or []:
            if not isinstance(rel, str) or not self._is_safe(rel):
                continue
            rel = rel.strip('/')
            if rel in tiles:
                continue
            total = 0
            direct = 0
            cover = ''
            mtime = 0.0
            pixiv = self._pixiv_mode(rel)
            if self._is_namespace_node(rel):
                root, _ = self._split_virtual(rel)
                if root is None:
                    continue
                try:
                    mtime = root.stat().st_mtime
                except OSError:
                    continue
                total, cover = self._aggregate_children(root, rel)
            else:
                dir_path, _ = self._resolve_dir(rel)
                if dir_path is None or not dir_path.is_dir():
                    continue
                try:
                    mtime = dir_path.stat().st_mtime
                except OSError:
                    continue
                entry = self._scan_dir_cached(rel, dir_path, mtime)
                direct = entry.get('direct_count', 0)
                if direct:
                    total, cover = direct, entry.get('direct_cover', '')
                else:
                    total, cover = self._children_total(rel, dir_path, mtime, entry)
            width = height = 1
            if cover:
                cover_path, _ = self._resolve_path(cover)
                try:
                    st = cover_path.stat()
                    width, height = self._get_image_size(str(cover_path), st.st_mtime)
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
        self._flush_meta_if_dirty()
        return {'tiles': tiles}

    def _item_image_sort_key(self, sort_by: str):
        """图片排序键：time_name 下图片一律按文件名自然序（p0 → p1），
        时间维度只作用于作品/相册卡片之间的顶层排序；其余按设置。"""
        if sort_by == 'time_name':
            return lambda i: natural_sort_key(Path(i['url']).name)
        if sort_by == 'mtime':
            return lambda i: i.get('mtime', 0.0)
        return lambda i: natural_sort_key(Path(i['url']).name)

    def list_folder_items(self, rel_path: str = '', page: int = 1,
                          per_page: int = 40, sort_by: str = 'name',
                          sort_order: str = 'asc') -> Dict:
        """混合列表：直接图片 + 直接子相册 p0 瓦片（只处理一层嵌套），瀑布流统一展示。

        子文件夹以 p0 瓦片返回（封面 = p0，time_name 模式下带圆圈数量角标），
        单图以 image 返回。`all_images` 为按瀑布流顺序展开的连续浏览序列：
        每个子文件夹的图片（p0 → p1 → …）依次展开、随后是单图，
        供灯箱向右连续翻看画师的其他作品（包括文件夹与单图）。

        **首屏只读当前目录一层**：没有直接图片、只有子目录的容器目录以
        `pending=True` 的占位卡片返回（`cover` 空、`total_count` 为 0），其封面
        与递归计数由前端对进入视口的瓦片调用 `load_album_tiles` 补齐。返回的
        `pending_count` 是本页待补瓦片数，`sequence_pending` 表示 `all_images`
        因为跳过了这些容器而不完整。
        """
        if not self._is_safe(rel_path):
            return {"items": [], "all_images": [], "page": 1, "total": 0, "settings": {}}
        try:
            page = max(1, int(page))
            per_page = min(200, max(1, int(per_page)))
        except (TypeError, ValueError):
            page, per_page = 1, 40

        cache_key = ('items', rel_path, sort_by, sort_order)
        dir_mtime = self._get_dir_mtime(rel_path)
        if cache_key in self._list_cache:
            cached = self._list_cache[cache_key]
            if cached[0] == dir_mtime:
                cached_items = cached[1]
                cached_all = cached[2] if len(cached) > 2 else None
                total = len(cached_items)
                start = (page - 1) * per_page
                end = start + per_page
                image_total = sum(1 for it in cached_items if it.get('type') == 'image')
                all_offset = self._all_images_offset(cached_items, start)
                all_images, truncated = self._limit_all_images(
                    cached_all if cached_all is not None
                    else [im for im in cached_items if im.get('type') == 'image'])
                page_items = self._resolve_pending_items(cached_items[start:end])
                return {
                    "items": page_items,
                    "all_images": all_images,
                    "all_truncated": truncated,
                    "all_offset": all_offset,
                    "page": page, "total": total, "image_total": image_total,
                    "pending_count": sum(1 for it in page_items if it.get('pending')),
                    "sequence_pending": any(it.get('has_children') and it.get('image_count') == 0
                                            for it in cached_items),
                    "has_next": end < total, "has_prev": page > 1,
                    "settings": self.get_settings(rel_path)
                }

        target_dir, _ = self._resolve_dir(rel_path)
        sub_dirs = []
        images = []
        if target_dir is not None:
            try:
                with os.scandir(target_dir) as entries:
                    for entry in entries:
                        if entry.name.startswith('.') or entry.name == '.cache':
                            continue
                        if entry.is_dir():
                            sub_dirs.append(entry.name)
                        elif entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                            stat = entry.stat()
                            images.append({
                                'type': 'image', 'path': entry.path, 'name': entry.name,
                                'mtime': stat.st_mtime, 'size': stat.st_size,
                            })
            except OSError:
                pass
        # 相册树的合成根节点：额外根目录在这里以命名空间卡片的形式出现
        # （要用完整虚拟路径 `__<token>`，不能用裸 token —— 那会被当成第一根
        #  下的同名物理目录去扫描，结果直接消失）
        if not rel_path:
            sub_dirs.extend(f'{NAMESPACE_MARKER}{token}' for token in self._roots_index())

        # 并行读取直接图片尺寸（首次扫描大文件夹时的主要开销）
        self._fill_image_sizes(images)
        for img in images:
            img['url'] = (Path(rel_path) / img['name']).as_posix()
            img['width'] = img.pop('width')
            img['height'] = img.pop('height')

        # 是否当场展开容器子目录的图片：现在一律不展开（见下面的说明），
        # `aggregate=False` 让容器瓦片在索引未命中时以 `pending` 占位返回。
        cards, album_images = self._scan_album_items(rel_path, sub_dirs, aggregate=False)

        reverse = (sort_order == 'desc')
        if sort_by == 'time_name':
            # Pixiv 排序支持：顶层作品/单图按 pixiv 数字号（前导数字）排序，方向生效
            # （倒序 = 大号在前 = 新作品在前），无数字名排最后；模糊匹配时同号/
            # 无号条目改为整名自然序（先数字再文字），让「日期+标题」也能排对。
            # 纯图片文件夹（作品内部）图片仍按文件名自然序 p0 → p1，不受方向影响。
            fuzzy = self._pixiv_fuzzy_mode(rel_path)
            cards = _pixiv_sort(cards, lambda x: x['name'], reverse, fuzzy)
            if cards:
                images = _pixiv_sort(images, lambda x: Path(x['url']).name, reverse, fuzzy)
            else:
                images.sort(key=lambda x: natural_sort_key(Path(x['url']).name))
            items = cards + images
        else:
            if sort_by == 'mtime':
                cards.sort(key=lambda x: x['mtime'], reverse=reverse)
                images.sort(key=lambda x: x['mtime'], reverse=reverse)
            else:
                cards.sort(key=lambda x: natural_sort_key(x['name']), reverse=reverse)
                images.sort(key=lambda x: natural_sort_key(Path(x['url']).name), reverse=reverse)
            items = cards + images

        # 连续浏览序列只含**已经扫过的**图片：单图 + 非容器子目录的图片。容器
        # 子目录（作者文件夹等）不展开 —— 展开要读整棵子树，实测某个 40978 张的
        # 容器目录单是列出外层目录就要 3.3s。序列不完整由 `sequence_pending` 告知，
        # 前端在打开灯箱时才调 `list_album_images` 取完整序列并就地替换。
        pending_dirs = [it['path'] for it in items
                        if it.get('has_children') and it.get('image_count') == 0]
        all_images = []
        for it in items:
            if it['type'] == 'image':
                all_images.append({'url': it['url'], 'width': it['width'], 'height': it['height']})
            else:
                sub_settings = self.get_settings(it['path'])
                sub_sort = sub_settings.get('sort_by') or sort_by
                sub_reverse = (sub_settings.get('sort_order') or 'desc') == 'desc'
                sub_imgs = sorted(album_images.get(it['path'], []),
                                  key=self._item_image_sort_key(sub_sort),
                                  reverse=(sub_sort != 'time_name' and sub_reverse))
                for si in sub_imgs:
                    all_images.append({'url': si['url'], 'width': si['width'], 'height': si['height']})

        self._cache_list(cache_key, (dir_mtime, items, all_images))
        self._flush_meta_if_dirty()

        all_images, capped = self._limit_all_images(all_images)
        total = len(items)
        start = (page - 1) * per_page
        end = start + per_page
        all_offset = self._all_images_offset(items, start)
        return {
            "items": items[start:end],
            "all_images": all_images,
            # 截断可能有两个原因，前端提示文案不同：命中 5000 条上限（`all_capped`）
            # 或序列里有待补的容器瓦片（`sequence_pending`）
            "all_truncated": capped or bool(pending_dirs),
            "all_capped": capped,
            "all_offset": all_offset,
            "page": page, "total": total,
            "image_total": len(images),
            # 本页返回的瓦片里还有哪些需要二级加载（前端按视口分批取封面与计数）；
            # 连续浏览序列也因此尚不完整，前端需要时再并入
            "pending_count": sum(1 for it in items[start:end] if it.get('pending')),
            "sequence_pending": bool(pending_dirs),
            "has_next": end < total, "has_prev": page > 1,
            "settings": self.get_settings(rel_path)
        }

    def _all_images_offset(self, items: List[Dict], start: int) -> int:
        """当前页首项之前，连续浏览序列 all_images 中已有多少张图片。

        分页后前端用 items[0] 对应 all_images[all_offset]，
        避免点击第 2+ 页的瓦片时灯箱打开到序列开头的错误图片。
        """
        offset = 0
        for it in items[:start]:
            offset += it.get('image_count', 0) if it.get('type') == 'album' else 1
        return offset

    def list_dir(self, rel_path: str = '') -> List[Dict]:
        """目录树浏览（移动目标选择等）：命名空间根展开为该根的子目录。"""
        if self._is_namespace_node(rel_path):
            root, _ = self._split_virtual(rel_path)
            return [{'name': e.name, 'path': self._virtual_path(root, e.name)}
                    for e in sorted(root.iterdir(), key=lambda p: natural_sort_key(p.name))
                    if e.is_dir() and not e.name.startswith('.')]
        root, inner = self._split_virtual(rel_path)
        if root is None:
            return []
        return list_directory(root, inner)
