"""漫画目录扫描与封面选择。"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

try:
    from natsort import natsorted as _natsorted
except ImportError:
    _NUM_RE = re.compile(r'\d+')
    def _natsorted(seq):
        return sorted(seq, key=lambda x: _NUM_RE.sub(lambda m: m.group(0).zfill(16), str(x)))


IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}


def natural_sorted(items):
    return _natsorted(items)


def visible_subdirs(folder_path: Path) -> List[Path]:
    """一级子目录里算「章节」的那些：跳过隐藏目录与名为 `ai` 的目录。"""
    try:
        return [d for d in folder_path.iterdir()
                if d.is_dir() and not d.name.startswith('.') and d.name != 'ai']
    except OSError:
        return []


def direct_images(directory: Path) -> List[Path]:
    """目录**直接下级**的图片文件（不递归）。读不了时按空列表处理。"""
    try:
        return [f for f in directory.iterdir()
                if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
    except OSError:
        return []


def has_images(folder_path: Path) -> bool:
    """这部「漫画」里有没有阅读器真能读到的图片。

    口径与 `find_cover` / `list_pages` 一致：单章漫画的图片直接放在漫画目录里，
    多章漫画放在**一级子目录**（章节）里；更深的层级两个函数都看不到，因此不算。

    `scan_manga` 用它当入架门槛。此前只看目录名，于是漫画根下任何一级子目录都会被
    当成一部漫画 —— 包括**空目录**（典型是插件自己的数据目录，如
    `<数据根>/group-mesh`），它们在书架上是一张 0 页、无封面的空白卡片。
    读不了的目录按「没有图片」处理，与图片相册的 `readable == 0` 隐藏空相册同一口径。
    """
    if direct_images(folder_path):
        return True
    return any(direct_images(sub) for sub in visible_subdirs(folder_path))


def load_album_info(folder_path: Path) -> dict:
    info_path = folder_path / 'album_info.json'
    if not info_path.exists():
        return {}
    try:
        with open(info_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def find_cover(folder_path: Path, manga_dir: Path) -> str:
    """优先显式 cover/封面，其次数字命名第一页，最后自然序第一张。

    只看自然序第一个章节（与阅读器进入详情时的默认章节一致）：首章为空时返回 `''`，
    卡片显示占位块。目录是否入架由 `has_images` 判定，它检查的是全部章节。
    """
    search_dirs = []
    sub_dirs = visible_subdirs(folder_path)
    if sub_dirs:
        search_dirs.append(_natsorted(sub_dirs)[0])
    search_dirs.append(folder_path)

    for directory in search_dirs:
        images = direct_images(directory)
        if not images:
            continue

        for f in images:
            if 'cover' in f.stem.lower() or '封面' in f.stem:
                return f.relative_to(manga_dir).as_posix()

        numeric = [f for f in images if f.stem.isdigit()]
        if numeric:
            return _natsorted(numeric)[0].relative_to(manga_dir).as_posix()

        return _natsorted(images)[0].relative_to(manga_dir).as_posix()
    return ""


def scan_manga(manga_dir: Path, favorites: List[str]) -> List[Dict]:
    manga_list = []
    if not manga_dir.exists():
        return manga_list

    fav_set = set(favorites)
    for entry in os.scandir(manga_dir):
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if not is_dir or entry.name.startswith('.') or entry.name == 'ai':
            continue
        folder_path = Path(entry.path)
        # 先看目录里有没有图片，再决定要不要收进书架：漫画根常常就是全局数据根
        # （root_dir 未配置时回落 get_data_root()），那里放着插件自己的数据目录，
        # 只看目录名会把它们变成一部 0 页、无封面的空白漫画
        if not has_images(folder_path):
            continue
        info = load_album_info(folder_path)
        cover_url = find_cover(folder_path, manga_dir)
        manga_list.append({
            'comic_id': info.get('album_id', entry.name),
            'title': info.get('title', entry.name),
            'author': info.get('author', '未知'),
            'tags': info.get('tags', []),
            'page_count': info.get('total_page_count', 0),
            'cover_url': cover_url,
            'folder_name': entry.name,
            'is_fav': entry.name in fav_set,
        })
    return manga_list


def resolve_safe_path(manga_dir: Path, folder_name: str, chapter_path: str = "") -> Optional[Path]:
    """解析漫画文件夹/章节路径，并保证不会越出漫画根目录。"""
    try:
        base_dir = (manga_dir / folder_name).resolve()
        if not base_dir.is_relative_to(manga_dir.resolve()):
            return None
        if chapter_path:
            target = (base_dir / chapter_path).resolve()
            if not target.is_relative_to(base_dir):
                return None
        else:
            target = base_dir
        return target
    except Exception:
        return None


def list_pages(manga_dir: Path, folder_name: str, chapter_path: str = "") -> List[str]:
    target_dir = resolve_safe_path(manga_dir, folder_name, chapter_path)
    if target_dir is None or not target_dir.exists():
        return []

    all_files = []
    for ext in ('*.jpg', '*.jpeg', '*.png', '*.webp'):
        all_files.extend(target_dir.glob(ext))

    result = []
    for f in _natsorted(all_files):
        try:
            result.append(f.relative_to(manga_dir).as_posix())
        except ValueError:
            continue
    return result
