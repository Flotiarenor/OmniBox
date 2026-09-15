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

    def _aggregate_children(self, dir_path: Path, rel_path: str) -> tuple:
        """递归统计目录下的图片总数与代表封面（含更深层的子目录）。

        返回 (总图片数, 代表封面)。直接子目录有图片时取该目录的 p0；只有更深层
        才有图片时**继续往下递归**——否则「作者/作品/1.jpg」这类作品文件夹会被
        算成 0 张，命名空间卡片与空目录判定就都不准了。
        代表封面按 pixiv 号倒序取第一个非空封面（= 最近的作品）。
        """
        children = []
        total = 0
        try:
            with os.scandir(dir_path) as entries:
                for e in entries:
                    if e.name.startswith('.') or e.name == '.cache' or not e.is_dir():
                        continue
                    sub_rel = f"{rel_path}/{e.name}" if rel_path else e.name
                    entry = self._scan_dir_direct(Path(e.path), sub_rel)
                    count = entry.get('direct_count', 0)
                    cover = entry.get('direct_cover', '')
                    if not count:
                        deep_total, deep_cover = self._aggregate_children(Path(e.path), sub_rel)
                        count, cover = deep_total, deep_cover
                    total += count
                    children.append((e.name, cover))
        except OSError:
            pass
        children = _pixiv_sort(children, lambda t: t[0], reverse=True)
        cover = next((c for _n, c in children if c), '')
        return total, cover

    def _scan_one_subdir(self, rel_path: str, name: str, cache_dirs: dict):
        """扫描单个直接子目录（供线程池并行调用），返回 (sub_rel, card, images)。

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
        # 缓存命中条件除目录 mtime 外还要看 Pixiv 排序标志：封面挑选规则由它决定，
        # 切换排序后旧封面必须重算（缓存里没存图片列表，无法就地重挑）
        cached = cache_dirs.get(sub_rel)
        pixiv = self._pixiv_mode(sub_rel)
        if cached and cached.get('mtime') is not None \
                and abs(float(cached.get('mtime', 0)) - float(mtime)) < 0.5 \
                and cached.get('pixiv') == pixiv:
            entry = cached
        else:
            entry = self._scan_dir_direct(dir_path, sub_rel)
            entry['mtime'] = mtime
            cache_dirs[sub_rel] = entry
        cover = entry.get('direct_cover', '')
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
        direct_count = len(images)
        total_count = direct_count
        if not images and entry.get('has_children'):
            # 纯容器子目录：递归聚合（一层），代表封面 + 总图片数
            agg_total, agg_cover = self._aggregate_children(dir_path, sub_rel)
            total_count = agg_total
            if agg_cover:
                cover = agg_cover
        if cover:
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
            'has_children': entry.get('has_children', False),
            'mtime': mtime,
            'width': cw or 1,
            'height': ch or 1,
            'use_time_name': pixiv,
            # 合成根（第一根的顶层）与额外根的命名空间节点同地位：前端把它当
            # Pixiv 树的配置点。普通作者目录不算，否则会被误判成配置点而少一层
            'root_scope': rel_path == '' and not name.startswith(NAMESPACE_MARKER),
        }
        return sub_rel, card, images

    def _scan_album_items(self, rel_path: str, sub_dirs: List[str]) -> tuple:
        """扫描每个直接子目录（并行）。

        返回 (卡片列表, {sub_rel: [直接图片 dict 列表]})。
        卡片封面 = p0（文件名自然序第一张）；纯容器子目录（无直接图片但有子文件夹）
        用递归聚合：封面 = 最新作品 p0、total_count = 递归总图片数；
        image_count 始终为直接图片数（连续浏览序列按它展开）。
        """
        cache_dirs = self._album_cache.get('dirs')
        if not isinstance(cache_dirs, dict):
            cache_dirs = self._album_cache['dirs'] = {}
        results = {}
        names = sorted(sub_dirs)
        with ThreadPoolExecutor(max_workers=self._SCAN_WORKERS) as ex:
            futures = {ex.submit(self._scan_one_subdir, rel_path, name, cache_dirs): name
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
                return {
                    "items": cached_items[start:end],
                    "all_images": all_images,
                    "all_truncated": truncated,
                    "all_offset": all_offset,
                    "page": page, "total": total, "image_total": image_total,
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

        cards, album_images = self._scan_album_items(rel_path, sub_dirs)

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

        # 连续浏览序列：按瀑布流顺序展开；每个子文件夹内部按【它自己生效的设置】排序
        # （自己有设置用自己，没有则逐级继承父级），不受当前视图排序影响——
        # 这样改外层排序不会破坏子文件夹内部已设定好的 p0 → p1 顺序。
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

        all_images, truncated = self._limit_all_images(all_images)
        total = len(items)
        start = (page - 1) * per_page
        end = start + per_page
        all_offset = self._all_images_offset(items, start)
        return {
            "items": items[start:end],
            "all_images": all_images,
            "all_truncated": truncated,
            "all_offset": all_offset,
            "page": page, "total": total,
            "image_total": len(images),
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
