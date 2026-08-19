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
    """优先显式 cover/封面，其次数字命名第一页，最后自然序第一张。"""
    search_dirs = []
    try:
        sub_dirs = [d for d in folder_path.iterdir()
                    if d.is_dir() and not d.name.startswith('.') and d.name != 'ai']
    except OSError:
        sub_dirs = []
    if sub_dirs:
        search_dirs.append(_natsorted(sub_dirs)[0])
    search_dirs.append(folder_path)

    for directory in search_dirs:
        try:
            images = [f for f in directory.iterdir()
                      if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
        except OSError:
            continue
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
        info = load_album_info(Path(entry.path))
        cover_url = find_cover(Path(entry.path), manga_dir)
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
