import os
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Dict
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling
from shell.backend.tasks import BackgroundTask
from shell.backend.thumb_cache import ThumbCache



_fs = load_sibling(__file__, 'filesystem', 'image_viewer')
ALLOWED_EXTENSIONS = _fs.ALLOWED_EXTENSIONS
drop_image_meta = _fs.drop_image_meta
ensure_thumbnail = _fs.ensure_thumbnail
get_image_size = _fs.get_image_size
is_safe_path = _fs.is_safe_path
list_directory = _fs.list_directory
natural_sort_key = _fs.natural_sort_key
pixiv_number = _fs.pixiv_number
stat_mtime = _fs.stat_mtime


def _pixiv_sort(entries, name_fn, reverse):
    """Pixiv 排序：前导数字条目按数字大小排（方向生效），
    无前导数字条目按自然名排并始终位于最后。"""
    numeric, other = [], []
    for it in entries:
        name = name_fn(it)
        num = pixiv_number(name)
        key = (num, natural_sort_key(name))
        (numeric if num is not None else other).append((key, it))
    numeric.sort(key=lambda t: t[0], reverse=reverse)
    other.sort(key=lambda t: t[0], reverse=reverse)
    return [it for _, it in numeric] + [it for _, it in other]

class ImageViewerPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "数据根目录", "type": "text",
         "placeholder": "默认: ./data", "help": "图片浏览的数据根目录"},
        {"key": "row_height", "label": "图片行高", "type": "range",
         "min": 100, "max": 400, "default": 200, "help": "Justified 布局的每行目标高度"},
        {"key": "per_page", "label": "每页图片数", "type": "number",
         "min": 10, "max": 200, "default": 40},
        {"key": "sort_by", "label": "排序方式", "type": "select",
         "default": "mtime",
         "options": [{"label": "修改时间", "value": "mtime"},
                     {"label": "文件名", "value": "name"},
                     {"label": "Pixiv 排序支持", "value": "time_name"}]},
        {"key": "sort_order", "label": "排序方向", "type": "select",
         "default": "desc",
         "options": [{"label": "倒序", "value": "desc"}, {"label": "正序", "value": "asc"}]},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir') or str(super().get_data_root())
        self.root_dir = Path(root).resolve()
        self.cache_dir = self.root_dir / '.cache'
        self.thumb_dir = self.cache_dir / 'thumbs'
        self.thumb_db_path = self.cache_dir / 'thumbs.db'
        self.meta_file = self.cache_dir / 'image_meta.json'
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.album_cache_file = self.cache_dir / 'albums_index.json'
        self.album_config_file = self.cache_dir / 'albums_config.json'
        self._meta_cache = self._load_meta()
        self._meta_dirty = False          # 尺寸元数据是否有新增条目需要落盘
        self._list_cache = {}
        self._album_config = self._load_album_config()
        self._album_cache = self._load_album_cache()
        self._albums_cached_at = 0.0      # list_albums 结果 TTL 缓存
        self._albums_cached = None
        # 缩略图缓存（共享基建）与全量重建后台任务（共享基建）
        self.thumb_cache = ThumbCache(self.thumb_db_path)
        self._rebuild = None              # BackgroundTask | None

    # ===== 常量 =====
    _ALBUMS_TTL = 30.0            # list_albums 全树扫描结果缓存秒数
    _MAX_LIST_CACHE = 200         # 列表缓存最大条目数，超出后淘汰最旧一半
    _MAX_ALL_IMAGES = 5000        # 连续浏览序列最大返回条数，超出截断
    _SCAN_WORKERS = 8             # 扫描/尺寸读取并行线程数

    def _load_meta(self) -> dict:
        if self.meta_file.exists():
            try:
                with open(self.meta_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_meta(self):
        """原子写元数据：先写临时文件再 os.replace，避免中途崩溃损坏缓存。"""
        try:
            tmp = self.meta_file.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._meta_cache, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.meta_file)
        except Exception as e:
            print(f"[ImageViewer] 保存元数据失败: {e}")

    def _mark_meta_dirty(self):
        self._meta_dirty = True

    def get_data_root(self) -> Path:
        return self.root_dir

    def _is_safe(self, rel_path: str) -> bool:
        return is_safe_path(self.root_dir, rel_path)


    def register_api(self) -> dict:
        return {
            'list_images': self.list_images,
            'list_folder_items': self.list_folder_items,
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
            'clear_folder_settings': self.clear_folder_settings,
        }

    def get_root_dir(self) -> str:
        return str(self.root_dir)

    def ensure_thumb(self, rel_path: str) -> str:
        # 旧版文件式入口，供其他兼容代码使用；新路由优先走 get_thumb_data。
        if not self._is_safe(rel_path):
            return ''
        try:
            thumb = self._get_thumb(rel_path)
            return str(thumb) if thumb and thumb.exists() else ''
        except Exception:
            return ''

    def get_thumb_data(self, rel_path: str):
        """供 Shell /thumbs 路由使用：从 SQLite（ThumbCache）读取/生成缩略图字节。"""
        if not self._is_safe(rel_path):
            return None
        try:
            return self.thumb_cache.get(rel_path, self.root_dir / rel_path)
        except Exception:
            return None

    def get_image_info(self, rel_path: str) -> Dict:
        """返回单张图片的存储大小与分辨率，供全屏查看器右侧信息面板使用。"""
        if not self._is_safe(rel_path):
            return {'success': False, 'error': '非法路径'}
        abs_path = self.root_dir / rel_path
        try:
            if not abs_path.is_file():
                return {'success': False, 'error': '不是图片文件'}
            stat = abs_path.stat()
            width, height = self._get_image_size(str(abs_path), stat.st_mtime)
            return {
                'success': True,
                'rel_path': rel_path,
                'size': stat.st_size,
                'width': width,
                'height': height,
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _get_dir_mtime(self, rel_path: str) -> float:
        return stat_mtime(self.root_dir, rel_path)


    def _get_image_size(self, abs_path: str, mtime: float) -> tuple:
        before = len(self._meta_cache)
        result = get_image_size(abs_path, mtime, self._meta_cache)
        if len(self._meta_cache) != before:
            # 新增了尺寸缓存条目才需要落盘，避免每次列表都全量重写 meta 文件
            self._meta_dirty = True
        return result

    def _fill_image_sizes(self, images: List[dict]):
        """并行填充 images 列表的 width/height（要求条目含 path/mtime 字段）。

        首次扫描大文件夹时 Pillow 打开文件读尺寸是主要开销，8 线程并行可
        把 4 万张图片的扫描从 ~5s 降到 ~1.5s。
        """
        if not images:
            return
        with ThreadPoolExecutor(max_workers=self._SCAN_WORKERS) as ex:
            futures = [ex.submit(self._get_image_size, it['path'], it['mtime'])
                       for it in images]
            for it, fut in zip(images, futures):
                it['width'], it['height'] = fut.result()

    def _cache_list(self, key, value):
        """写入列表缓存；超过上限时淘汰最旧一半，防止长时间使用内存膨胀。"""
        self._list_cache[key] = value
        if len(self._list_cache) > self._MAX_LIST_CACHE:
            for k in list(self._list_cache)[:self._MAX_LIST_CACHE // 2]:
                del self._list_cache[k]

    def _flush_meta_if_dirty(self):
        """尺寸元数据有新增条目时落盘一次（列表操作末尾调用）。"""
        if self._meta_dirty:
            self._save_meta()
            self._meta_dirty = False

    def _limit_all_images(self, all_images: List[Dict]) -> tuple:
        """连续浏览序列截断保护：超大相册不全量下发，防止内存/带宽放大。"""
        if len(all_images) > self._MAX_ALL_IMAGES:
            return all_images[:self._MAX_ALL_IMAGES], True
        return all_images, False


    def _get_thumb(self, rel_path: str) -> Path:
        return ensure_thumbnail(self.root_dir, rel_path, self.thumb_dir)


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

        target_dir = self.root_dir / rel_path
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
        except FileNotFoundError:
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

    # ===== 混合瀑布流（只处理一层嵌套）：直接图片 + 直接子相册 p0 瓦片 =====

    def _aggregate_children(self, dir_path: Path, rel_path: str) -> tuple:
        """统计纯容器子目录下一层子目录的聚合信息。

        返回 (总图片数, 代表封面)。代表封面 = 按 pixiv 号倒序
        第一个含直接图片的子目录的 p0（即画师文件夹展示其最新作品的 p0）。
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
                    total += entry.get('direct_count', 0)
                    children.append((e.name, entry.get('direct_cover', '')))
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
        dir_path = self.root_dir / sub_rel
        try:
            mtime = dir_path.stat().st_mtime
        except OSError:
            return None
        cached = cache_dirs.get(sub_rel)
        if cached and cached.get('mtime') is not None \
                and abs(float(cached.get('mtime', 0)) - float(mtime)) < 0.5:
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
            abs_path = self.root_dir / cover
            try:
                st = abs_path.stat()
                cw, ch = self._get_image_size(str(abs_path), st.st_mtime)
            except OSError:
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
            'use_time_name': (self.get_settings(sub_rel).get('sort_by') == 'time_name'),
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

        target_dir = self.root_dir / rel_path
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
        except FileNotFoundError:
            pass

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
            # （倒序 = 大号在前 = 新作品在前），无数字名排最后；
            # 纯图片文件夹（作品内部）图片仍按文件名自然序 p0 → p1，不受方向影响。
            cards = _pixiv_sort(cards, lambda x: x['name'], reverse)
            if cards:
                images = _pixiv_sort(images, lambda x: Path(x['url']).name, reverse)
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
        return list_directory(self.root_dir, rel_path)
    # ===== 相册索引（持久化 + 按目录 mtime 增量更新） =====

    def _load_album_cache(self) -> dict:
        if self.album_cache_file.exists():
            try:
                with open(self.album_cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # 版本 3：封面改为文件名自然序第一张（p0）。
                # 旧版（version 2）缓存里封面是按最新 mtime 取的，必须作废重扫。
                if isinstance(data, dict) and data.get('version') == 3:
                    return data
            except Exception:
                pass
        return {'version': 3, 'dirs': {}}

    def _save_album_cache(self):
        try:
            self.album_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.album_cache_file, 'w', encoding='utf-8') as f:
                json.dump(self._album_cache, f, ensure_ascii=False)
        except Exception as e:
            print(f'[ImageViewer] 保存相册索引失败: {e}')
    def _load_album_config(self) -> dict:
        defaults = {'collapsed': [], 'promoted': []}
        if self.album_config_file.exists():
            try:
                with open(self.album_config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {**defaults, **data}
            except Exception:
                pass
        return defaults

    def _save_album_config(self):
        try:
            self.album_config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.album_config_file, 'w', encoding='utf-8') as f:
                json.dump(self._album_config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f'[ImageViewer] 保存相册配置失败: {e}')

    def _list_album_dirs(self) -> dict:
        """只遍历目录树本身（不读文件），返回 {rel_path: dir_mtime}。"""
        dirs = {}
        try:
            stat = self.root_dir.stat()
            dirs[''] = stat.st_mtime
        except OSError:
            return dirs
        for current, dir_names, _files in os.walk(self.root_dir):
            dir_names[:] = [d for d in dir_names
                            if not d.startswith('.') and d != '.cache']
            current = Path(current)
            if current == self.root_dir:
                continue
            try:
                rel = current.relative_to(self.root_dir).as_posix()
                dirs[rel] = current.stat().st_mtime
            except (OSError, ValueError):
                continue
        return dirs

    def _scan_dir_direct(self, dir_path: Path, rel_path: str) -> dict:
        """只扫描一个目录的直接图片（一次 os.scandir，开销可控）。"""
        images = []
        children = []
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    if entry.name.startswith('.') or entry.name == '.cache':
                        continue
                    if entry.is_dir():
                        children.append(entry.name)
                    elif entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                        try:
                            stat = entry.stat()
                        except OSError:
                            continue
                        images.append({
                            'rel': (Path(rel_path) / entry.name).as_posix() if rel_path else entry.name,
                            'mtime': stat.st_mtime,
                        })
        except OSError:
            pass
        # 封面 = 文件名自然序第一张（p0），相册新旧仍按最新 mtime 计算
        newest = max((img['mtime'] for img in images), default=0.0)
        cover = min(images, key=lambda img: natural_sort_key(Path(img['rel']).name))['rel'] if images else ''
        return {
            'path': rel_path,
            'name': dir_path.name if rel_path else '未分类',
            'depth': rel_path.count('/') + (1 if rel_path else 0),
            'parent': '/'.join(rel_path.split('/')[:-1]) if rel_path else None,
            'direct_count': len(images),
            'direct_cover': cover,
            'direct_mtime': newest,
            'has_children': len(children) > 0,
            'children': sorted(children),
        }

    def _build_albums(self, dirs: dict, cache_dirs: dict) -> tuple:
        """直接扫描变化目录，再自底向上聚合出递归统计。"""
        cache_dirs = cache_dirs or {}
        entries = {}
        changed = 0
        for rel, mtime in dirs.items():
            cached = cache_dirs.get(rel)
            if cached and cached.get('mtime') is not None \
                    and abs(float(cached.get('mtime', 0)) - float(mtime)) < 0.5:
                entries[rel] = cached
                continue
            dir_path = self.root_dir / rel if rel else self.root_dir
            entry = self._scan_dir_direct(dir_path, rel)
            entry['mtime'] = mtime
            entries[rel] = entry
            changed += 1

        # 自底向上聚合 image_count / cover / mtime
        ordered = sorted(entries.values(), key=lambda e: e['depth'], reverse=True)
        totals = {}
        for entry in ordered:
            rel = entry['path']
            total = entry['direct_count']
            cover = entry['direct_cover']
            newest = entry['direct_mtime']
            for child_name in entry['children']:
                child_rel = f"{rel}/{child_name}" if rel else child_name
                child_totals = totals.get(child_rel)
                if not child_totals:
                    continue
                total += child_totals[0]
                if child_totals[2] > newest:
                    newest = child_totals[2]
                    cover = child_totals[1]
            totals[rel] = (total, cover, newest)

        albums = []
        for entry in entries.values():
            total, cover, newest = totals[entry['path']]
            albums.append({
                'name': entry['name'],
                'path': entry['path'],
                'parent': entry['parent'],
                'image_count': total,
                'direct_count': entry['direct_count'],
                'has_children': entry['has_children'],
                'cover': cover,
                'mtime': newest,
                'depth': entry['depth'],
                'use_time_name': (self.get_settings(entry['path']).get('sort_by') == 'time_name'),
            })

        # 不再预生成所有封面缩略图：交给 /thumbs 按需生成，避免上万次随机小文件 I/O。
        return albums, entries, changed

    def list_albums(self) -> Dict:
        """相册列表：目录 mtime 未变时直接复用持久化索引，避免每次重启全量扫描。

        全树目录枚举（os.walk）在大型图库上耗时数秒，结果带 30 秒 TTL 缓存：
        频繁进入相册页直接返回上次结果；手动刷新（refresh）与全量重建会强制重扫。
        """
        now = time.time()
        if self._albums_cached is not None \
                and (now - self._albums_cached_at) < self._ALBUMS_TTL:
            return {**self._albums_cached, 'cached': True}
        dirs = self._list_album_dirs()
        albums, entries, changed = self._build_albums(dirs, self._album_cache.get('dirs', {}))
        self._album_cache = {'version': 3, 'dirs': entries}
        if changed:
            self._save_album_cache()
        result = {'albums': albums, 'config': self._album_config, 'changed': changed}
        self._albums_cached_at = now
        self._albums_cached = result
        return result

    def _invalidate_albums_cache(self):
        """让 list_albums 下一次调用强制重扫（刷新/重建/相册配置变更时调用）。"""
        self._albums_cached_at = 0.0
        self._albums_cached = None

    def get_album_config(self) -> Dict:
        return self._album_config

    def set_album_config(self, rel_path: str, action: str) -> Dict:
        """album 层级控制：collapse/expand（收纳子相册）、promote/unpromote（提升到全部相册）。"""
        rel_path = (rel_path or '').strip().strip('/')
        collapsed = set(self._album_config.get('collapsed', []))
        promoted = set(self._album_config.get('promoted', []))
        if action == 'collapse':
            collapsed.add(rel_path)
        elif action == 'expand':
            collapsed.discard(rel_path)
        elif action == 'promote':
            promoted.add(rel_path)
        elif action == 'unpromote':
            promoted.discard(rel_path)
        else:
            return {'success': False, 'error': f'未知操作: {action}'}
        self._album_config = {
            'collapsed': sorted(collapsed),
            'promoted': sorted(promoted),
        }
        self._save_album_config()
        self._invalidate_albums_cache()
        return {'success': True, 'config': self._album_config}

    def create_folder(self, rel_path: str) -> Dict:
        """在根目录（或指定相对目录）下新建相册文件夹。"""
        rel_path = (rel_path or '').replace('\\', '/').strip('/')
        if not rel_path or not self._is_safe(rel_path):
            return {'success': False, 'error': '文件夹名称非法'}
        target = self.root_dir / rel_path
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'path': rel_path}


    def delete_files(self, rel_paths: List[str]) -> Dict:
        deleted, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f"非法路径: {rel}")
                continue
            abs_path = self.root_dir / rel
            try:
                if abs_path.exists():
                    abs_path.unlink()
                    self.thumb_cache.delete(rel)
                    drop_image_meta(self._meta_cache, str(abs_path))
                    self._meta_dirty = True
                    deleted.append(rel)
            except Exception as e:
                errors.append(f"删除失败 {rel}: {str(e)}")
        self._list_cache.clear()
        if deleted:
            self._flush_meta_if_dirty()
        return {"deleted": deleted, "errors": errors}

    def move_files(self, rel_paths: List[str], dest_rel: str) -> Dict:
        if not self._is_safe(dest_rel):
            return {"moved": [], "errors": ["目标目录非法"]}
        dest_dir = self.root_dir / dest_rel
        if not dest_dir.is_dir():
            return {"moved": [], "errors": ["目标目录不存在"]}
        moved, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f"非法源路径: {rel}")
                continue
            src = self.root_dir / rel
            try:
                if src.exists():
                    dest_file = dest_dir / src.name
                    if dest_file.exists() and src != dest_file:
                        stem, suffix = dest_file.stem, dest_file.suffix
                        counter = 1
                        while dest_file.exists():
                            dest_file = dest_dir / f"{stem}_{counter}{suffix}"
                            counter += 1
                    shutil.move(str(src), str(dest_file))
                    self.thumb_cache.delete(rel)
                    drop_image_meta(self._meta_cache, str(src))
                    self._meta_dirty = True
                    moved.append(rel)
            except Exception as e:
                errors.append(f"移动失败 {rel}: {str(e)}")
        self._list_cache.clear()
        if moved:
            self._flush_meta_if_dirty()
        return {"moved": moved, "errors": errors}

    def regenerate_thumbs(self, rel_paths: List[str]) -> Dict:
        """重新生成选中图片的缩略图：删除缓存缩略图并重新生成。

        用于修复下载丢失/文件被替换后残留的坏缩略图（如黑图、空图）。
        同时清理该图片的尺寸元数据缓存，避免旧尺寸残留。
        """
        regenerated, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f'非法路径: {rel}')
                continue
            try:
                self.thumb_cache.delete(rel)
                drop_image_meta(self._meta_cache, str(self.root_dir / rel))
                new_thumb = self.get_thumb_data(rel)
                if new_thumb:
                    regenerated.append(rel)
                else:
                    errors.append(f'缩略图生成失败: {rel}')
            except Exception as e:
                errors.append(f'缩略图更新失败 {rel}: {str(e)}')
        if regenerated:
            self._save_meta()
        return {'regenerated': regenerated, 'errors': errors}

    def refresh(self) -> Dict:
        """清空内存缓存并作废旧相册索引，让新增/替换的图片立即生效（无需重启）。"""
        self._list_cache.clear()
        self._album_cache = {'version': 3, 'dirs': {}}
        self._invalidate_albums_cache()
        try:
            if self.album_cache_file.exists():
                self.album_cache_file.unlink()
        except OSError:
            pass
        return {'success': True}

    def _collect_all_images(self, rel_path: str = '') -> List[str]:
        """收集数据根目录下（或指定子文件夹下）所有需要生成缩略图的图片相对路径。"""
        rel_path = (rel_path or '').strip().strip('/')
        base_dir = self.root_dir / rel_path if rel_path else self.root_dir
        prefix = rel_path
        images = []
        try:
            for current, dir_names, filenames in os.walk(base_dir):
                dir_names[:] = [d for d in dir_names if not d.startswith('.') and d != '.cache']
                current_path = Path(current)
                rel_dir = '' if current_path == base_dir else current_path.relative_to(base_dir).as_posix()
                for name in filenames:
                    if name.startswith('.'):
                        continue
                    if Path(name).suffix.lower() in ALLOWED_EXTENSIONS:
                        parts = [p for p in (prefix, rel_dir, name) if p]
                        images.append('/'.join(parts))
        except OSError:
            pass
        return images

    def rebuild_all(self, rel_path: str = '', force: bool = True) -> Dict:
        """全量/指定文件夹重建。

        - rel_path 非空时：只重建该文件夹，且不会清空已有缩略图（增量补齐）。
        - rel_path 为空且 force=True 时：清空旧缓存后全量重新生成。
        - rel_path 为空且 force=False 时：全库增量补齐，跳过已有有效缩略图。
        """
        rel_path = (rel_path or '').strip().strip('/')
        if rel_path and not self._is_safe(rel_path):
            return {'started': False, 'success': False, 'error': '非法路径'}
        if self._rebuild and self._rebuild.state == 'running':
            return {'started': False, 'running': True, **self.rebuild_status()}

        self._list_cache.clear()
        if not rel_path and force:
            self._invalidate_albums_cache()
            self._meta_cache = {}
            try:
                if self.meta_file.exists():
                    self.meta_file.unlink()
            except OSError:
                pass
            self.thumb_cache.clear()
            # 旧版散文件缩略图目录已不再使用，全量重建时一并清理。
            try:
                if self.thumb_dir.exists():
                    shutil.rmtree(self.thumb_dir)
            except OSError:
                pass
            self.thumb_dir.mkdir(parents=True, exist_ok=True)
            self._album_cache = {'version': 3, 'dirs': {}}
            try:
                if self.album_cache_file.exists():
                    self.album_cache_file.unlink()
            except OSError:
                pass

        images = self._collect_all_images(rel_path)
        task = BackgroundTask(kind='rebuild', extra={'rebuild_path': rel_path})
        task.update(total=len(images))
        task.start(self._rebuild_worker, args=(images,))
        self._rebuild = task
        return {'started': True, 'running': True, 'total': len(images)}

    def rebuild_folder(self, rel_path: str) -> Dict:
        """只重建指定文件夹下的缩略图（增量补齐，不清空已有缓存）。"""
        return self.rebuild_all(rel_path=rel_path, force=False)

    def _rebuild_worker(self, task: BackgroundTask, images: List[str]):
        """全量重建 worker：批量并行生成缩略图到 ThumbCache。"""
        def progress(processed, total, current, errors):
            task.update(processed=processed, total=total, current=current, errors=errors)

        try:
            items = [(rel, self.root_dir / rel) for rel in images]
            result = self.thumb_cache.generate_bulk(
                items,
                progress_cb=progress,
                stop_event=task.stop_event,
            )
            task.update(processed=result['processed'], errors=result['errors'])
        except Exception as e:
            task.add_error(f'重建异常: {e}')
        finally:
            task.update(current='')

    def rebuild_status(self) -> Dict:
        """返回全量重建后台任务的进度。"""
        task = self._rebuild
        if not task:
            return {
                'running': False,
                'done': False,
                'success': False,
                'cancelled': False,
                'rebuild_path': '',
                'total': 0,
                'processed': 0,
                'current': '',
                'error_count': 0,
                'errors': [],
            }
        status = task.status()
        return {
            'running': status['running'],
            'done': status['done'],
            'success': status['success'],
            'cancelled': status['cancelled'],
            'rebuild_path': status['extra'].get('rebuild_path', ''),
            'total': status['total'],
            'processed': status['processed'],
            'current': status['current'],
            'error_count': status['error_count'],
            'errors': status['errors'],
        }

    def rebuild_cancel(self) -> Dict:
        """请求取消当前全量重建任务；已生成的缩略图会保留。"""
        if self._rebuild and self._rebuild.state == 'running':
            self._rebuild.cancel()
            return {'success': True}
        return {'success': False, 'error': '没有正在运行的重建任务'}

    def get_settings(self, rel_path: str = '') -> Dict:
        """获取文件夹生效设置（含全局回退与逐级继承）。

        文件夹设置按「当前文件夹 → 父文件夹 → … → 全局 → 硬默认」逐级
        向上继承：在父文件夹（如 pixiv）上启用 time_name 后，其下所有子文件夹
        自动继承同一排序；某个子文件夹被单独修改（folders[该路径] 存在）时
        以它自己的设置优先，并继续向其子文件夹传播。

        `pixiv_explicit`：当前文件夹自身是否显式设置了「Pixiv 排序支持」
        （即它是 Pixiv 排序的配置点，如 pixiv 主文件夹——该层显示作者网格，
        继承它的子层才显示瀑布流）。
        """
        folders = self.setting('folders') or {}
        if not isinstance(folders, dict):
            folders = {}
        global_settings = folders.get('__global__', {})
        hard_defaults = {
            "row_height": 200,
            "per_page": 40,
            "sort_by": "mtime",
            "sort_order": "desc"
        }
        folder_settings = {}
        parts = [p for p in (rel_path or '').split('/') if p]
        for i in range(len(parts), 0, -1):
            key = '/'.join(parts[:i])
            if key in folders:
                folder_settings = folders[key]
                break
        result = {**hard_defaults, **global_settings, **folder_settings}
        result['root_dir'] = str(self.root_dir)
        # 自身条目显式设置了 Pixiv 排序才视为配置点（继承的不算）
        own_entry = folders.get(rel_path or '__global__', {})
        result['pixiv_explicit'] = (own_entry.get('sort_by') == 'time_name')
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
        if 'root_dir' in changed_keys:
            new_dir = self.setting('root_dir')
            if new_dir and Path(new_dir).is_dir():
                self.root_dir = Path(new_dir).resolve()
                self.cache_dir = self.root_dir / '.cache'
                self.thumb_dir = self.cache_dir / 'thumbs'
                self.thumb_db_path = self.cache_dir / 'thumbs.db'
                self.meta_file = self.cache_dir / 'image_meta.json'
                self.thumb_dir.mkdir(parents=True, exist_ok=True)
                self.thumb_cache = ThumbCache(self.thumb_db_path)
                self._meta_cache = self._load_meta()
                self._list_cache.clear()

    def clear_folder_settings(self, rel_path: str) -> Dict:
        """删除指定文件夹的独立设置，使其回退到全局设置"""
        folders = self.setting('folders') or {}
        if isinstance(folders, dict) and rel_path in folders:
            del folders[rel_path]
            self.update_setting('folders', folders)
            self._invalidate_albums_cache()
        return {"success": True}
