import os
import json
import hashlib
from pathlib import Path
from typing import Dict, List

from PIL import Image


ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}


class ImageModule:
    def __init__(self, base_dir: str):
        self.image_dir = Path(base_dir).resolve()
        self.thumb_dir = self.image_dir / '.cache' / 'thumbs'
        self.meta_dir = self.image_dir / '.cache' / 'meta'
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)

    def _is_safe(self, rel_path: str) -> bool:
        try:
            target = (self.image_dir / rel_path).resolve()
            return str(target).startswith(str(self.image_dir))
        except Exception:
            return False

    def _get_image_size(self, abs_path: str, mtime: float) -> tuple:
        """从缓存获取图片尺寸，缓存未命中则读取"""
        file_hash = hashlib.md5(str(abs_path).encode('utf-8')).hexdigest()
        cache_file = self.meta_dir / f"{file_hash}.json"

        if cache_file.exists():
            try:
                with open(cache_file, 'r') as f:
                    data = json.load(f)
                    if data.get('mtime') == mtime:
                        return data.get('width', 0), data.get('height', 0)
            except Exception:
                pass

        width, height = 0, 0
        try:
            with Image.open(abs_path) as img:
                width, height = img.size
        except Exception:
            pass

        try:
            with open(cache_file, 'w') as f:
                json.dump({'mtime': mtime, 'width': width, 'height': height}, f)
        except Exception:
            pass

        return width, height

    def list_images(self, rel_path: str, page: int = 1,
                    per_page: int = 40, sort_by: str = 'mtime',
                    sort_order: str = 'desc') -> Dict:
        if not self._is_safe(rel_path):
            return {"images": [], "page": 1, "total": 0}

        target_dir = self.image_dir / rel_path
        images: List[Dict] = []

        try:
            with os.scandir(target_dir) as entries:
                for entry in entries:
                    if entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                        mtime = entry.stat().st_mtime
                        url_path = (Path(rel_path) / entry.name).as_posix()
                        width, height = self._get_image_size(entry.path, mtime)
                        images.append({
                            'url': url_path,
                            'mtime': mtime,
                            'width': width,
                            'height': height
                        })
        except FileNotFoundError:
            pass

        reverse = (sort_order == 'desc')
        if sort_by == 'name':
            images.sort(key=lambda x: x['url'].lower(), reverse=reverse)
        else:
            images.sort(key=lambda x: x['mtime'], reverse=reverse)

        total = len(images)
        start = (page - 1) * per_page
        end = start + per_page

        return {
            "images": images[start:end],
            "page": page,
            "total": total,
            "has_next": end < total,
            "has_prev": page > 1
        }