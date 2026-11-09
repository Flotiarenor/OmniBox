"""OmniBox 统一媒体目录浏览契约。

图片、视频、音乐等需要浏览本地媒体目录的插件都可以复用这里的路径安全、
breadcrumb 生成、目录枚举、**扩展名分类**与**目录扫描原语**，避免各自实现一套
“文件浏览器”。
"""

import os
import re
import string
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, TypedDict

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


# ===== 目录扫描原语 =====
#
# 为什么要有这一节：插件的"扫描一个目录"看起来是一行 `os.scandir`，实际每个插件都
# 各自重写了一遍，而且每一遍都在不同的地方漏掉同一批细节 —— 跳过点文件、跳过
# `.cache`、按扩展名过滤、`entry.stat()` 抛 OSError 时不能整趟失败、自然序而不是
# 字典序（`p10` 排在 `p2` 后面）。这些细节漏一个就是"某个目录里的图片少了几张"或
# "顺序莫名其妙"，而它们与业务无关，只与"怎样读一个目录才算读对"有关。
#
# 这里只提供**读**：返回纯数据的描述（名字 / 绝对路径 / mtime / 体积 / 类型），
# 不做递归、不做缓存、不做业务富化（封面、尺寸、图标一类留给调用方）。想递归的
# 调用方在 `dirs` 上自己往下走 —— 递归策略（是否跟随符号链接、是否剪枝、是否限深）
# 是业务决定，抽进基建只会让每个人都继承一套不对的默认值。

#: 匹配连续数字段。自然序的实现靠它把名字切成「文字 / 数字」交替的片段。
_NUM_RE = re.compile(r'(\d+)')


def is_hidden_name(name: str) -> bool:
    """该文件名是否应当跳过：点开头的文件与目录（`.cache` 也在其中）。

    判据只有"点开头"一条，没有额外的黑名单：Unix 的点文件约定与 Windows 的
    `desktop.ini`、`Thumbs.db` 都能被各插件按需再过滤，而在这里硬编码平台黑名单
    会让"为什么这个文件不出现"变成查不到答案的问题。
    """
    return name.startswith('.')


def natural_sort_key(text: Any) -> Any:
    """自然排序键：`p2` 排在 `p10` 前面，`1.jpg` 排在 `10.jpg` 前面。

    优先用 `natsort`（`requirements.txt` 已声明，行为与既有插件一致）；它不可用时
    回退到内置实现。回退实现刻意返回**同构的 3 元组**而不是
    `[int(...) if part.isdigit() else part.lower() ...]`：后者在
    「`1.png` 与 `a.png` 同目录」时会拿 int 和 str 比较，直接抛 `TypeError`，
    于是"某个目录一排序就崩"。
    """
    raw = str(text)
    try:
        from natsort import natsort_keygen
        return natsort_keygen()(raw)
    except Exception:
        pass
    chunks: List[Tuple[int, int, str]] = []
    for index, part in enumerate(_NUM_RE.split(raw.lower())):
        if index % 2:
            chunks.append((0, int(part), ''))
        elif part:
            chunks.append((1, 0, part))
    return tuple(chunks)


class DirEntryInfo(TypedDict):
    """一个子目录的描述。`path` 是 `os.scandir` 给出的路径：目录参数为绝对路径时它才绝对。"""

    name: str
    path: str
    mtime: float


class FileEntryInfo(TypedDict):
    """一个文件条目的描述。`kind` 取 `classify_file` 的结果（image / video / audio / other）。"""

    name: str
    path: str
    mtime: float
    size: int
    kind: str


class ScanResult(TypedDict):
    """一次目录扫描的结果。读不到目录时 `error` 非 None、两个列表为空。"""

    dirs: List[DirEntryInfo]
    files: List[FileEntryInfo]
    truncated: bool
    error: Optional[str]


def scan_directory(
    directory: Path | str,
    allowed_extensions: Optional[Set[str]] = None,
    *,
    limit: Optional[int] = None,
) -> ScanResult:
    """扫描一个目录的**直接下级**，按自然序返回子目录与媒体文件。

    - 跳过点开头的条目（见 `is_hidden_name`）；
    - `allowed_extensions` 为 None 表示收下全部文件，传集合则按小写后缀过滤；
    - 单个条目 `stat` 失败（权限、竞态删除）只跳过该条目，不整趟失败；
    - `limit` 限制返回条目总数（目录 + 文件），超出时 `truncated` 为真。
      它是给"极端目录"（盘符根、十万文件的下载目录）用的护栏，不是分页：
      需要分页的调用方应当自己按 `limit` 反复扫或用别的索引方式。
    """
    dirs: List[DirEntryInfo] = []
    files: List[FileEntryInfo] = []
    truncated = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if is_hidden_name(entry.name):
                    continue
                if limit is not None and (len(dirs) + len(files)) >= limit:
                    truncated = True
                    break
                try:
                    if entry.is_dir():
                        dirs.append({
                            'name': entry.name,
                            'path': entry.path,
                            'mtime': entry.stat().st_mtime,
                        })
                    elif entry.is_file():
                        suffix = Path(entry.name).suffix.lower()
                        if allowed_extensions is not None and suffix not in allowed_extensions:
                            continue
                        stat = entry.stat()
                        files.append({
                            'name': entry.name,
                            'path': entry.path,
                            'mtime': stat.st_mtime,
                            'size': stat.st_size,
                            'kind': classify_file(entry.name),
                        })
                except OSError:
                    continue
    except OSError:
        return {'dirs': [], 'files': [], 'truncated': False,
                'error': '无法读取目录（网络盘可能已断开）'}

    dirs.sort(key=lambda item: natural_sort_key(item['name']))
    files.sort(key=lambda item: natural_sort_key(item['name']))
    return {'dirs': dirs, 'files': files, 'truncated': truncated, 'error': None}
