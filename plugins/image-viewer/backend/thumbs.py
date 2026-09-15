"""元数据缓存与缩略图（ImageViewerPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 ImageViewerPlugin.__init__ 持有 ——
分片只把方法挂到同一个类上，因此方法与调用点都没有变。
"""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'image_viewer')
ensure_thumbnail = _common.ensure_thumbnail
get_image_size = _common.get_image_size
stat_mtime = _common.stat_mtime

class ThumbMixin:
    """元数据缓存与缩略图。"""

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
            log.error(f"[ImageViewer] 保存元数据失败: {e}")

    def _mark_meta_dirty(self):
        self._meta_dirty = True

    def get_data_root(self) -> Path:
        return self.root_dir

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
            abs_path, _ = self._resolve_path(rel_path)
            if abs_path is None:
                return None
            return self.thumb_cache.get(rel_path, abs_path)
        except Exception:
            return None

    def get_image_info(self, rel_path: str) -> Dict:
        """返回单张图片的存储大小与分辨率，供全屏查看器右侧信息面板使用。"""
        if not self._is_safe(rel_path):
            return {'success': False, 'error': '非法路径'}
        abs_path, _ = self._resolve_path(rel_path)
        if abs_path is None:
            return {'success': False, 'error': '非法路径'}
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
        root, inner = self._split_virtual(rel_path)
        if root is None:
            return 0.0
        return stat_mtime(root, inner)

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
            for it, fut in zip(images, futures, strict=True):
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
        root, inner = self._split_virtual(rel_path)
        base = root if root is not None else self.root_dir
        return ensure_thumbnail(base, inner, self.thumb_dir)
