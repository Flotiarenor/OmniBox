"""图片浏览器的文件系统与图片处理工具。"""

import hashlib
import os
import shutil
from pathlib import Path
from typing import Dict, List

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}


def is_safe_path(root: Path, rel_path: str) -> bool:
    """严格判断 rel_path 位于 root 内，避免字符串前缀误判（/data vs /data_evil）。"""
    try:
        target = (root / rel_path).resolve()
        return target.is_relative_to(root.resolve())
    except Exception:
        return False


def stat_mtime(root: Path, rel_path: str) -> float:
    try:
        return os.stat(root / rel_path).st_mtime
    except Exception:
        return 0.0


def get_image_size(abs_path: str, mtime: float, meta_cache: dict) -> tuple:
    """从元数据缓存读取图片尺寸，未命中时用 Pillow 读取并回写缓存。"""
    key = hashlib.md5(abs_path.encode()).hexdigest()
    if key in meta_cache:
        data = meta_cache[key]
        if data.get('mtime') == mtime:
            return data.get('width', 0), data.get('height', 0)
    width, height = 0, 0
    try:
        from PIL import Image
        with Image.open(abs_path) as img:
            width, height = img.size
    except Exception:
        pass
    meta_cache[key] = {'mtime': mtime, 'width': width, 'height': height}
    return width, height


def ensure_thumbnail(root: Path, rel_path: str, thumb_dir: Path) -> Path:
    """生成缩略图；Pillow 不可用或打开失败时退回原图复制。"""
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
    if not is_safe_path(root, rel_path):
        return []
    target = root / rel_path
    if not target.exists() or not target.is_dir():
        return []
    children = []
    try:
        for entry in os.scandir(target):
            if entry.is_dir() and not entry.name.startswith('.') and entry.name != '.cache':
                child_path = (Path(rel_path) / entry.name).as_posix()
                children.append({"name": entry.name, "path": child_path})
    except PermissionError:
        pass
    children.sort(key=lambda x: x['name'])
    return children
