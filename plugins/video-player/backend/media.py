"""媒体文件目录扫描与路径安全。"""

import os
from pathlib import Path
from typing import Dict, List

ALLOWED_EXTENSIONS = {
    '.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv',
    '.mp3', '.flac', '.wav', '.aac', '.ogg', '.wma'
}


def is_safe_path(root: Path, rel_path: str) -> bool:
    """严格判断 rel_path 位于媒体根目录内。"""
    try:
        target = (root / rel_path).resolve()
        return target.is_relative_to(root.resolve())
    except Exception:
        return False


def list_directory(root: Path, rel_path: str = '') -> List[Dict]:
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
    children.sort(key=lambda x: x['name'].lower())
    return children


def list_media(root: Path, rel_path: str = '') -> Dict:
    if not is_safe_path(root, rel_path):
        return {"dirs": [], "files": [], "path": rel_path}
    target = root / rel_path
    if not target.exists() or not target.is_dir():
        return {"dirs": [], "files": [], "path": rel_path}
    dirs = []
    files = []
    try:
        for entry in os.scandir(target):
            if entry.name.startswith('.') or entry.name == '.cache':
                continue
            if entry.is_dir():
                child_path = (Path(rel_path) / entry.name).as_posix()
                dirs.append({"name": entry.name, "path": child_path})
            elif entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                files.append({
                    "name": entry.name,
                    "path": (Path(rel_path) / entry.name).as_posix(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
    except PermissionError:
        pass
    dirs.sort(key=lambda x: x['name'].lower())
    files.sort(key=lambda x: x['name'].lower())
    return {"dirs": dirs, "files": files, "path": rel_path}
