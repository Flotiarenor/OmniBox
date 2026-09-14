"""OmniBox 统一媒体目录浏览契约。

图片、视频、音乐等需要浏览本地媒体目录的插件都可以复用这里的路径安全、
breadcrumb 生成、目录枚举与**扩展名分类**，避免各自实现一套“文件浏览器”。
"""

import os
import string
from pathlib import Path
from typing import Dict, List, Optional, Set

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv'}
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.wav', '.aac', '.ogg', '.wma'}

#: 需要浏览本机目录的插件用这个标记表示「我的电脑」层（盘符/文件系统根）。
#: 前端把它当作一个特殊路径：接口仍按空路径处理，但返回 parent 指向自己，
#: 这样「上级」按钮不会越过顶层继续往上找。
DRIVES_SENTINEL = '__drives__'


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


def find_kinds(
    directory: Path,
    kinds: Optional[Set[str]] = None,
    extensions: Optional[Set[str]] = None,
) -> List[str]:
    """目录**直接下级**命中的媒体类型（按 kinds 顺序返回）。

    目录选择器用它提示「这个目录里有什么」（含图片 / 视频 / 音乐），
    因此只看一层、按类型短路，不做递归统计。
    """
    wanted = [k for k in (kinds or ['image', 'video', 'audio'])
              if kinds is None or k in kinds]
    found = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.startswith('.'):
                    continue
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                suffix = Path(entry.name).suffix.lower()
                if extensions is not None and suffix not in extensions:
                    continue
                found.add(classify_file(entry.name))
                if all(k in found for k in wanted):
                    break
    except OSError:
        return []
    return [k for k in wanted if k in found]


def list_system_roots() -> List[Path]:
    """文件系统根：Windows 列存在的盘符，其他平台为 `/`。"""
    if os.name == 'nt':
        return [Path(f'{letter}:\\') for letter in string.ascii_uppercase
                if Path(f'{letter}:\\').exists()]
    return [Path('/')]


def list_subdirectories(path: str = '',
                        kinds: Optional[Set[str]] = None) -> Dict:
    """浏览绝对路径下的子目录（本地目录选择器共用基建）。

    空路径（或 `DRIVES_SENTINEL`）返回「我的电脑」：列出盘符/文件系统根，
    `parent` 为 None 表示已经到顶。每个目录带 `kinds`，提示它**直接下级**里
    有哪些媒体类型（`kinds=None` 表示三类都探测，传集合可限定）。
    """
    if not str(path or '').strip() or str(path).strip() == DRIVES_SENTINEL:
        return {
            'path': '', 'parent': None,
            'entries': [{'name': str(root), 'path': str(root), 'is_dir': True,
                         'kinds': find_kinds(root, kinds)}
                        for root in list_system_roots()],
        }
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        return {'path': '', 'parent': None, 'entries': [], 'error': '路径无法解析'}
    if not target.is_dir():
        return {'path': '', 'parent': None, 'entries': [], 'error': '目录不存在'}
    parent = str(target.parent) if target.parent != target else None
    dirs = []
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                if entry.name.startswith('.') or entry.name == '.cache':
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                dirs.append(entry)
                if len(dirs) >= 2000:      # 极端目录（如整个盘符根）限流
                    break
    except OSError as e:
        return {'path': str(target), 'parent': parent, 'entries': [],
                'error': f'无法读取目录: {e}'}
    dirs.sort(key=lambda e: e.name.lower())
    return {
        'path': str(target),
        'parent': parent,
        'entries': [{'name': e.name, 'path': e.path, 'is_dir': True,
                     'kinds': find_kinds(Path(e.path), kinds)}
                    for e in dirs],
    }


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
