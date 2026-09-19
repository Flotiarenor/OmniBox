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


def _windows_drive_letters() -> Optional[str]:
    r"""Windows 上可用的盘符字母（一次系统查询，不做任何落盘探测）。

    为什么不用 `Path('Z:\\').exists()`：断开的网络映射盘会让它等 SMB 超时
    （实测 9 秒以上），遍历 A:–Z: 就是几分钟。`GetLogicalDrives()` 只读内核里的
    盘符位图，**与盘是否可达无关**，因此不会阻塞。

    返回 None 表示这条路不可用（非 Windows、或 ctypes 调用失败），由调用方回退。
    """
    if os.name != 'nt':
        return None
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return None
    if not mask:
        return None
    return ''.join(letter for index, letter in enumerate(string.ascii_uppercase)
                   if mask & (1 << index))


def list_system_roots() -> List[Path]:
    """文件系统根：Windows 列存在的盘符，其他平台为 `/`。

    **不做落盘探测**（见 `_windows_drive_letters`）：断开的网络盘不该拖住枚举。
    代价是它也包含"映射还在、机器没开机"的盘 —— 那些盘在展开时才会失败，
    由 `list_subdirectories` 把错误如实报给调用方（那里已有 OSError 分支）。
    """
    letters = _windows_drive_letters()
    if letters is None:
        return [Path('/')] if os.name != 'nt' else _fallback_existing_roots()
    return [Path(f'{letter}:\\') for letter in letters]


def _fallback_existing_roots() -> List[Path]:
    """ctypes 不可用时的退路：老老实实逐个探（可能被网络盘拖慢，但至少能用）。"""
    roots = []
    for letter in string.ascii_uppercase:
        target = Path(f'{letter}:\\')
        try:
            if target.exists():
                roots.append(target)
        except OSError:
            continue
    return roots


def _scan_dirs(target: Path, limit: int = 2000) -> Optional[List]:
    r"""列出一层子目录；不可达的路径返回 None（而不是抛异常）。

    对断开的网络盘，`os.scandir` 会等 SMB 超时。这里**不做超时包装**：把这种调用
    丢进被遗弃的线程，会让解释器退出时卡死（实测：daemon 线程即使已放弃，进程也退
    不掉）。宁可让这一次浏览慢，也不要让进程收不了尾。调用方拿到 None 时如实报错。
    """
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
                if len(dirs) >= limit:     # 极端目录（如整个盘符根）限流
                    break
    except OSError:
        return None
    return dirs


def list_subdirectories(path: str = '',
                        kinds: Optional[Set[str]] = None) -> Dict:
    """浏览绝对路径下的子目录（本地目录选择器共用基建）。

    空路径（或 `DRIVES_SENTINEL`）返回「我的电脑」：列出盘符/文件系统根，
    `parent` 为 None 表示已经到顶。每个目录带 `kinds`，提示它**直接下级**里
    有哪些媒体类型（`kinds=None` 表示三类都探测，传集合可限定）。
    """
    if not str(path or '').strip() or str(path).strip() == DRIVES_SENTINEL:
        entries = []
        for root in list_system_roots():
            # 这里只报盘符本身，不去探它的内容：不可达的盘会让 find_kinds 卡在
            # SMB 超时上（展开该盘时才会真正去读，那时失败也只是一条错误条目）
            entries.append({'name': str(root), 'path': str(root), 'is_dir': True,
                            'kinds': []})
        return {'path': '', 'parent': None, 'entries': entries}
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        return {'path': '', 'parent': None, 'entries': [], 'error': '路径无法解析'}
    if not target.is_dir():
        return {'path': '', 'parent': None, 'entries': [], 'error': '目录不存在'}
    parent = str(target.parent) if target.parent != target else None
    dirs = _scan_dirs(target)
    if dirs is None:
        return {'path': str(target), 'parent': parent, 'entries': [],
                'error': '无法读取目录（网络盘可能已断开）'}
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
