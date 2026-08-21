"""OmniBox 统一媒体目录浏览契约。

图片、视频、音乐等需要浏览本地媒体目录的插件都可以复用这里的路径安全、
breadcrumb 生成和目录枚举，避免各自实现一套“文件浏览器”。
"""

import os
from pathlib import Path
from typing import Dict, List, Optional

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv'}
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.wav', '.aac', '.ogg', '.wma'}


def is_safe_path(root: Path, rel_path: str) -> bool:
    """严格判断 rel_path 是否位于 root 内。"""
    try:
        target = (root / rel_path).resolve()
        return target.is_relative_to(root.resolve())
    except Exception:
        return False


def resolve_safe_path(root: Path, rel_path: str) -> Optional[Path]:
    """解析并校验 rel_path；越界返回 None。"""
    try:
        target = (root / rel_path).resolve()
        if not target.is_relative_to(root.resolve()):
            return None
        return target
    except Exception:
        return None


def build_breadcrumbs(root: Path, rel_path: str) -> List[Dict[str, str]]:
    """从相对路径生成面包屑：根目录 -> a -> b。"""
    crumbs = [{'name': '根目录', 'path': ''}]
    rel_path = (rel_path or '').strip('/\\')
    if not rel_path:
        return crumbs
    parts = [p for p in rel_path.replace('\\', '/').split('/') if p]
    path = ''
    for part in parts:
        path = f'{path}/{part}'.lstrip('/')
        crumbs.append({'name': part, 'path': path})
    return crumbs


def classify_file(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return 'image'
    if suffix in VIDEO_EXTENSIONS:
        return 'video'
    if suffix in AUDIO_EXTENSIONS:
        return 'audio'
    return 'other'


def list_directory(root: Path, rel_path: str = '',
                   allowed_extensions: Optional[set] = None,
                   include_files: bool = True) -> Dict:
    """枚举一个目录，返回 dirs + files + breadcrumbs。"""
    root = Path(root).resolve()
    if not is_safe_path(root, rel_path):
        return {'dirs': [], 'files': [], 'breadcrumbs': build_breadcrumbs(root, rel_path)}

    target = resolve_safe_path(root, rel_path)
    if target is None or not target.exists() or not target.is_dir():
        return {'dirs': [], 'files': [], 'breadcrumbs': build_breadcrumbs(root, rel_path)}

    dirs: List[Dict] = []
    files: List[Dict] = []
    try:
        for entry in os.scandir(target):
            if entry.name.startswith('.') or entry.name == '.cache':
                continue
            rel = (Path(rel_path) / entry.name).as_posix()
            try:
                stat = entry.stat()
            except OSError:
                stat = None

            if entry.is_dir():
                dirs.append({
                    'name': entry.name,
                    'path': rel,
                    'mtime': stat.st_mtime if stat else 0,
                })
            elif include_files and entry.is_file():
                suffix = Path(entry.name).suffix.lower()
                if allowed_extensions is None or suffix in allowed_extensions:
                    files.append({
                        'name': entry.name,
                        'path': rel,
                        'size': stat.st_size if stat else 0,
                        'mtime': stat.st_mtime if stat else 0,
                        'kind': classify_file(entry.name),
                    })
    except PermissionError:
        pass

    dirs.sort(key=lambda x: x['name'].lower())
    files.sort(key=lambda x: x['name'].lower())
    return {
        'dirs': dirs,
        'files': files,
        'breadcrumbs': build_breadcrumbs(root, rel_path),
    }
