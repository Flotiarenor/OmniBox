"""图片浏览器的文件系统与图片处理工具（纯函数，无实例状态）。

缩略图缓存已迁移到共享基建 `shell/backend/thumb_cache.py`（ThumbCache），
本模块只保留路径安全、排序、尺寸元数据等无状态工具。
"""

import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.media_catalog import (
    is_safe_path,  # noqa: F401
    list_directory as _catalog_list_directory,
)

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}

_NUM_RE = re.compile(r'(\d+)')


def natural_sort_key(text: Any) -> Any:
    """自然排序键：p0 < p1 < p2 < ... < p10。优先使用 venv 的 natsort，缺失时回退内置实现。"""
    try:
        from natsort import natsort_keygen
        return natsort_keygen()(str(text))
    except Exception:
        return [int(part) if part.isdigit() else part.lower()
                for part in _NUM_RE.split(str(text))]


def pixiv_number(name: Any) -> int | None:
    """提取名称前导数字（Pixiv 作品 ID / 图片编号）；无前导数字返回 None。"""
    m = _NUM_RE.match(str(name))
    return int(m.group(1)) if m else None


def drop_image_meta(meta_cache: dict, abs_path: str):
    """删除某张图片的尺寸元数据缓存（文件被替换/重新生成缩略图时调用）。"""
    key = hashlib.md5(abs_path.encode()).hexdigest()
    meta_cache.pop(key, None)


def stat_mtime(root: Path, rel_path: str) -> float:
    try:
        return os.stat(root / rel_path).st_mtime
    except Exception:
        return 0.0


def get_image_size(abs_path: str, mtime: float, meta_cache: dict) -> tuple:
    """从元数据缓存读取图片尺寸，未命中时用 Pillow 读取并回写缓存。

    读取失败时不写缓存：临时不可读（文件被占用/写入中）的文件不会被
    永久缓存成 0×0，下次扫描会重试。
    """
    key = hashlib.md5(abs_path.encode()).hexdigest()
    if key in meta_cache:
        data = meta_cache[key]
        if data.get('mtime') == mtime:
            return data.get('width', 0), data.get('height', 0)
    try:
        from PIL import Image
        with Image.open(abs_path) as img:
            width, height = img.size
    except Exception:
        return 0, 0
    meta_cache[key] = {'mtime': mtime, 'width': width, 'height': height}
    return width, height


def ensure_thumbnail(root: Path, rel_path: str, thumb_dir: Path) -> Path:
    """旧版文件式缩略图入口，保留给其他兼容代码使用（如 image-cleaner 代理）；
    新代码请使用 shell.backend.thumb_cache.ThumbCache。"""
    thumb_path = thumb_dir / rel_path
    if thumb_path.exists():
        return thumb_path
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        img = Image.open(root / rel_path)
        img.thumbnail((300, 300))
        img.save(thumb_path)
    except Exception:
        try:
            shutil.copy(root / rel_path, thumb_path)
        except Exception:
            pass
    return thumb_path


def list_directory(root: Path, rel_path: str) -> List[Dict]:
    return _catalog_list_directory(root, rel_path, allowed_extensions=ALLOWED_EXTENSIONS, include_files=False)['dirs']
