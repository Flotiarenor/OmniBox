'''统一新鲜度基建：同步（被动增量）/ 校验（手动全量）。

与另外两块共享基建并列的第三块（契约见 docs/plugin-guide.md §3.4
「统一刷新（同步 / 校验）」）：

- 控制面 `shell/backend/tasks.py`：线程 + 状态机 + 取消 + 进度 + 持久化；
- 数据面 `shell/backend/thumb_cache.py`：派生数据的 SQLite 缓存 + 指纹失效；
- **决策面**（本模块）：什么时候扫、哪些算变化、什么算幽灵。

## 为什么需要（现状取证）

同一件事——"发现变化 → 失效派生数据 → 清理幽灵"——在 8 个插件里被各写了一遍，
判据互不相同，用户可见名字有 6 种：

| 插件 | 现状判据 | 后果 |
| --- | --- | --- |
| image-viewer | 目录 mtime + 30s TTL | 删除/移动不清相册缓存；无孤儿清理入口 |
| image-cleaner | 无（key 只有 mode） | 缓存永不失效，看不到新文件 |
| media-player | mtime+size（只增不减） | 删除的媒体永久留在索引里 |
| manga-library | 不缓存（`_cache` 是死代码） | 每次切视图 2 遍全树遍历 |
| document-reader | mtime+size+id 集合 | 判据最合理，但派生数据不回写 |
| pixiv-sync | 每次全盘 walk + 整体换对象 | 与并发任务分叉、丢记录 |

## 统一后的对外语义只有两个词

- **同步（sync）**：被动、增量、便宜。目录级短路，只对"变了"的目录逐条比对，
  昂贵解析只发生在新增/变化的条目上。前端进入视图或切目录时调用，壳侧去抖。
- **校验（verify）**：手动、全量、彻底。全量遍历 + 逐项指纹校验 + 幽灵清理
  + 派生缓存对账。走后台任务，有进度、可取消。

## 插件只回答三个问题

```python
def freshness_spec(self):
    return {
        'roots': lambda: self._roots(),      # ① 管哪些文件（可调用：设置变更后重新求值）
        'include': ('.jpg', '.png'),         #    关心的后缀（空 = 全部常规文件）
        'prefixes': ('', '__额外图库'),       #    根 → 虚拟路径前缀（默认见 _default_prefix）
        'content_version': 4,                # ② 解析规则版本：变了整体作废派生数据
        'derive': self._freshness_derive,    # ③ 哪些条目需要重活（只对变化项调用）
        'prune': self._freshness_prune,      #    条目消失时清派生缓存
        'on_verified': self._freshness_audit,#    全量校验后按"有效键集合"对账孤儿
        'rebuild': self._freshness_rebuild,  #    逃生门：丢弃派生缓存重建
        'unit': '张',
    }
```

派生数据（尺寸 / 标签 / 章节 / 封面 / dHash）**仍然留在插件自己的数据面里**：
基建只决定"什么时候调 derive"，不接管插件的业务存储。这是迁移面最小的切法——
8 个插件的索引结构、字段、落盘格式都不用改。

## 性能设计（本模块存在的理由）

稳态（无变化）下每次同步的开销 = **每个目录一次 `os.scandir` + 名字过滤**，
零次 `stat`：目录指纹先比 `dir_mtime`，再比"纳入索引的名字个数"（只数名字，
不需要 stat），两者都没变就整目录跳过。只有变了才逐条 `DirEntry.stat()`
（Windows 上由目录枚举顺带给出，不额外发系统调用）。

**代价与边界**：原地覆盖写（同名、新内容）不改目录 mtime 也不改名字个数，
被动同步看不见它——这类陈旧由"派生缓存自己按 mtime/size 失效"兜住，权威结论
由**校验**给出（全量 stat）。这正是两个动作的分工：同步便宜且基本正确，
校验慢但彻底。

其余性能约束：

- 去抖：同一插件 `min_sync_interval` 秒内只真扫一次；
- 预算：单次被动同步最多 `max_dirs_per_sync` 个目录，超出返回 `partial`，
  由调用方转后台校验续完（不阻塞 API 线程）；
- 广度优先：部分同步也先覆盖浅层，用户马上能看到目录级变化；
- 单飞：同一插件的同步/校验同时只有一个在跑（现状里"查-建-赋值"无锁，
  双击按钮就是两趟全盘 walk）；
- 错误进报告（截断保留），不抛给调用方。
'''

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from shell.backend.tasks import BackgroundTask

log = logging.getLogger(__name__)

# ===== 调参：默认值一律"性能优先"，插件可在 spec 里覆盖 =====
DEFAULT_MIN_SYNC_INTERVAL = 5.0   # 被动同步最小间隔（秒）：进视图/切目录都会触发，必须去抖
DEFAULT_MAX_DIRS = 400            # 单次被动同步最多**处理**的目录数，超出转后台校验
_VISITED_DIR_FACTOR = 8           # 单次被动同步最多**走过**的目录数 = max_dirs × 该系数
DEFAULT_SKIP_DIRS = ('.cache',)   # 缓存/数据目录一律不进索引
_ERROR_CAP = 20                   # 报告里最多保留多少条错误（报告要走 HTTP，与 tasks.py 的 200 条同理）

STATE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS dirs (
    key         TEXT PRIMARY KEY,
    dir_mtime   REAL NOT NULL DEFAULT 0,
    file_count  INTEGER NOT NULL DEFAULT 0,
    size_sum    INTEGER NOT NULL DEFAULT 0,
    max_mtime   REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entries (
    key      TEXT PRIMARY KEY,
    dir_key  TEXT NOT NULL,
    mtime    REAL NOT NULL DEFAULT 0,
    size     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entries_dir ON entries(dir_key);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
'''


def _default_prefix(index: int, root: Path) -> str:
    """第一个根用空前缀（沿用相对路径），其余用 `__<目录名>`（与 image-viewer 的命名空间同形）。"""
    return '' if index == 0 else f'__{root.name}'


# ============================================================================
# 来源：文件系统
# ============================================================================

@dataclass(frozen=True)
class FsSource:
    """受管的文件系统范围。

    `roots` 与 `prefixes` 一一对应：`prefixes[i]` 是该根在"虚拟路径"里的前缀
    （第一根为空串）。条目键 = `前缀/根内相对路径`，与插件自己对外用的路径同形，
    这样派生缓存（缩略图键、索引键）可以原样复用，不需要额外映射表。
    """

    roots: Tuple[Path, ...]
    prefixes: Tuple[str, ...] = ()
    include: Tuple[str, ...] = ()
    # 要**纳入指纹**的完整文件名（不区分大小写）：例如目录封面 `folder.jpg`。
    # 为什么单独一项：它既不是插件索引里的"条目"，又确实会影响派生数据
    # （media-player 的 `has_cover`、专辑封面都取决于同目录有没有封面图）。
    # 不纳入时"后补一张 folder.jpg"不会改变任何指纹，派生数据永远不刷新。
    include_names: Tuple[str, ...] = ()
    skip_dirs: Tuple[str, ...] = DEFAULT_SKIP_DIRS

    def __post_init__(self) -> None:
        if self.prefixes and len(self.prefixes) != len(self.roots):
            raise ValueError('prefixes 必须与 roots 一一对应')
        if not self.prefixes:
            object.__setattr__(
                self, 'prefixes',
                tuple(_default_prefix(i, r) for i, r in enumerate(self.roots)))
        object.__setattr__(self, 'include', tuple(x.lower() for x in self.include))
        object.__setattr__(self, 'include_names',
                           frozenset(x.lower() for x in self.include_names))

    # ----- 键 -----

    def key_of(self, index: int, rel: str) -> str:
        prefix = self.prefixes[index] if index < len(self.prefixes) else ''
        rel = (rel or '').strip('/')
        return f'{prefix}/{rel}' if prefix and rel else (prefix or rel)

    def wants(self, name: str) -> bool:
        """该文件是否纳入索引（只看名字，不 stat —— 稳态零系统调用的前提）。"""
        if not name or name.startswith('.'):
            return False
        if self.include_names and name.lower() in self.include_names:
            return True
        return not self.include or Path(name).suffix.lower() in self.include

    def skips(self, name: str) -> bool:
        return name in self.skip_dirs or name.startswith('.')

    # ----- 作用域 → 起始目录 -----

    def scope_start(self, scope: str) -> Optional[Tuple[int, str]]:
        """把前端给的作用域解析成 (根序号, 根内相对目录)；越界/未知返回 None。

        作用域来自前端，必须当成不可信输入：`..`、绝对路径、未知命名空间一律拒绝，
        解析结果还要再确认落在该根之内（本方法只做字符串层，物理层由 resolve_scope 做）。
        """
        scope = (scope or '').strip().strip('/')
        if scope in ('', '.'):
            return (0, '')
        if os.path.isabs(scope) or scope.startswith('..') or '/../' in f'/{scope}/':
            return None
        for index, prefix in enumerate(self.prefixes):
            if not prefix:
                continue
            if scope == prefix:
                return (index, '')
            if scope.startswith(f'{prefix}/'):
                return (index, scope[len(prefix) + 1:])
        return (0, scope)

    def resolve_scope(self, scope: str) -> Optional[Tuple[str, Path]]:
        """解析成 (起始目录键, 物理目录)；不存在或逃出根之外返回 None。"""
        parsed = self.scope_start(scope)
        if parsed is None:
            return None
        index, rel = parsed
        root = self.roots[index]
        target = (root / rel) if rel else root
        try:
            target = target.resolve()
            if not target.is_relative_to(Path(root).resolve()) or not target.is_dir():
                return None
        except OSError:
            return None
        return (self.key_of(index, rel), target)


# ============================================================================
# 声明：插件怎么描述自己的新鲜度
# ============================================================================

def _resolve_decl(value: Any) -> Any:
    """声明项：值本身，或返回它的可调用对象（根/前缀允许在运行时变化）。"""
    return value() if callable(value) else value


def _as_sequence(value: Any) -> Tuple[Any, ...]:
    """声明项 → 元组。单值（`str` / `Path`）算一项 —— 否则会被逐字符遍历。"""
    if value is None:
        return ()
    if isinstance(value, (str, Path)):
        return (value,)
    return tuple(value)


@dataclass
class FreshnessSpec:
    """规范化后的插件声明（由 `freshness_spec()` 的 dict 归一而来）。"""

    #: 受管根：**值或返回值的可调用对象**（设置能在运行时变，见 `source`）。
    roots_decl: Any = ()
    #: 与根一一对应的虚拟路径前缀，同样是值或可调用对象。
    prefixes_decl: Any = ()
    include: Tuple[str, ...] = ()
    include_names: Tuple[str, ...] = ()
    skip_dirs: Tuple[str, ...] = DEFAULT_SKIP_DIRS
    derive: Optional[Callable[..., Any]] = None
    prune: Optional[Callable[..., Any]] = None
    on_verified: Optional[Callable[..., Any]] = None
    # 整趟结束（同步与校验都会调）：适合把"一次遍历攒下来的改动"落盘。
    # 为什么要它：索引是**单文件**的插件（media-player 的媒体索引 JSON）不能每处理
    # 一个目录就整体重写一遍，需要"这一趟结束了再写"的时机。
    on_pass_end: Optional[Callable[..., Any]] = None
    rebuild: Optional[Callable[..., Any]] = None
    # 条目键的算法：入参是**绝对物理路径**，返回插件自己用的条目键。
    # 默认用虚拟路径（`前缀/根内相对路径`），适合"路径就是键"的插件；
    # 用 md5(path) 之类做稳定 id 的插件（media-player 的 item id）在这里换算法，
    # 这样派生缓存（缩略图库、索引）不需要再做一层映射表。
    key_of: Optional[Callable[[str], str]] = None
    # 来源类型：`fs`（默认，本地文件树）或 `remote`（远端 API）。
    # 远端没有文件指纹可用，判据只能是**时间**：`ttl_seconds` 内视为新鲜，
    # 过期才让插件丢弃本地缓存重取。手动「校验」一律忽略 TTL。
    mode: str = 'fs'
    ttl_seconds: float = 0.0
    # remote：丢弃插件的本地派生缓存（URL 缓存之类）。远端数据本身由插件按需重取。
    invalidate: Optional[Callable[..., Any]] = None
    content_version: int = 1
    unit: str = '项'
    min_sync_interval: float = DEFAULT_MIN_SYNC_INTERVAL
    max_dirs_per_sync: int = DEFAULT_MAX_DIRS
    # 被动同步是否逐条 stat。False（默认）= 目录级短路，零 stat，代价是看不见
    # "原地写入"（同名、新内容，不改目录 mtime）：只有校验能发现它。目录不大、
    # 且"文件被替换"必须立刻反映到派生数据的插件把它设为 True。
    stat_entries: bool = False

    @property
    def source(self) -> FsSource:
        """每次**遍历时**现取来源。

        不能在建引擎时定死：`roots` 往往是"当前设置"的函数（image-viewer 的额外
        图片目录、media-player 的媒体根都能在运行时增删），引擎却是长期缓存的
        对象 —— 定死会让"新加的根要重启才生效"。声明非法时退回上一次可用的来源，
        绝不让整趟遍历炸掉。
        """
        roots = _as_sequence(_resolve_decl(self.roots_decl))
        roots = tuple(Path(p).expanduser() for p in roots)
        prefixes = _as_sequence(_resolve_decl(self.prefixes_decl))
        prefixes = tuple(str(p or '') for p in prefixes)
        try:
            source = FsSource(roots=roots, prefixes=prefixes,
                              include=tuple(self.include),
                              include_names=tuple(self.include_names),
                              skip_dirs=tuple(self.skip_dirs))
        except ValueError as e:
            log.error(f'freshness: roots/prefixes 声明不一致（{e}），退回默认前缀')
            source = FsSource(roots=roots, include=tuple(self.include),
                              include_names=tuple(self.include_names),
                              skip_dirs=tuple(self.skip_dirs))
        self._last_source = source
        return source

    @classmethod
    def normalize(cls, plugin: Any, raw: Optional[dict]) -> Optional['FreshnessSpec']:
        """把插件的声明归一成 spec；`None` / 空 dict 表示"本插件不参与"。"""
        if not raw:
            return None
        if not isinstance(raw, dict):
            raise ValueError('freshness_spec() 必须返回 dict 或 None')

        roots_decl = raw.get('roots')
        roots = tuple(Path(p).expanduser()
                      for p in _as_sequence(_resolve_decl(roots_decl)))
        mode = str(raw.get('mode') or 'fs').strip().lower()
        if mode not in ('fs', 'remote'):
            raise ValueError("freshness_spec() 的 mode 只能是 'fs' 或 'remote'")
        if mode == 'remote':
            # 远端来源没有文件树：roots 不参与，也不做"根为空"的判断
            roots = ()
        # 根为空**不算"不参与"**：根往往是运行时配置出来的（group-mesh 的共享目录
        # 默认一个都没有）。把它当"不支持"，界面就只能显示"本插件未参与统一刷新"，
        # 而正确状态是"已接入、当前没有受管目录"。所以照样给引擎，走空遍历。
        prefixes_decl: Any = raw.get('prefixes') or ()
        if not callable(prefixes_decl):
            prefixes_decl = tuple(str(p or '') for p in _as_sequence(prefixes_decl))
            # 静默声明错误在装载期就报出来（作者当场能看见）；运行期再不一致时
            # `source` 属性会退回默认前缀并记日志，不让整趟遍历炸掉。
            if prefixes_decl and len(prefixes_decl) != len(roots):
                raise ValueError('prefixes 必须与 roots 一一对应')

        def _hook(name: str) -> Optional[Callable[..., Any]]:
            fn = raw.get(name)
            if fn is None:
                return None
            if not callable(fn):
                raise ValueError(f'freshness_spec() 的 {name} 必须可调用')
            return fn

        return cls(
            roots_decl=roots_decl,
            prefixes_decl=prefixes_decl,
            include=tuple(str(x).lower() for x in (raw.get('include') or ())),
            include_names=tuple(str(x) for x in (raw.get('include_names') or ())),
            skip_dirs=tuple(raw.get('skip_dirs') or DEFAULT_SKIP_DIRS),
            derive=_hook('derive'),
            prune=_hook('prune'),
            on_verified=_hook('on_verified'),
            on_pass_end=_hook('on_pass_end'),
            rebuild=_hook('rebuild'),
            key_of=_hook('key_of'),
            mode=mode,
            ttl_seconds=float(raw.get('ttl_seconds') or 0.0),
            invalidate=_hook('invalidate'),
            content_version=int(raw.get('content_version') or 1),
            unit=str(raw.get('unit') or '项'),
            min_sync_interval=float(raw.get('min_sync_interval') or DEFAULT_MIN_SYNC_INTERVAL),
            max_dirs_per_sync=int(raw.get('max_dirs_per_sync') or DEFAULT_MAX_DIRS),
            stat_entries=bool(raw.get('stat_entries')),
        )


# ============================================================================
# 数据面：目录指纹 + 条目指纹
# ============================================================================

class FreshnessStore:
    """一个插件的指纹库（`<缓存目录>/freshness.db`）。

    只存"键 / 指纹 / 状态"，不存业务数据 —— 业务数据仍在插件自己的索引里。

    连接策略与 ThumbCache 一致：**不持有长连接**，每次操作短连接。好处是插件卸载
    不需要额外收尾（`on_unload` 是插件的钩子，基建不该要求插件记得关我）。
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.executescript(STATE_SCHEMA)
        return conn

    # ----- 目录 -----

    def dirs(self, conn: sqlite3.Connection) -> Dict[str, Tuple[float, int, int, float]]:
        try:
            rows = conn.execute(
                'SELECT key, dir_mtime, file_count, size_sum, max_mtime FROM dirs').fetchall()
        except sqlite3.Error:
            return {}
        return {r[0]: (float(r[1]), int(r[2]), int(r[3]), float(r[4])) for r in rows}

    def entries_of(self, conn: sqlite3.Connection, dir_key: str) -> Dict[str, Tuple[float, int]]:
        try:
            rows = conn.execute(
                'SELECT key, mtime, size FROM entries WHERE dir_key = ?', (dir_key,)).fetchall()
        except sqlite3.Error:
            return {}
        return {r[0]: (float(r[1]), int(r[2])) for r in rows}

    def replace_dir(self, conn: sqlite3.Connection, dir_key: str,
                    fingerprint: Tuple[float, int, int, float],
                    entries: Sequence[Tuple[str, float, int]]) -> None:
        """整目录替换（指纹 + 该目录的全部条目）—— 单事务，不留半份状态。"""
        with conn:
            conn.execute(
                'INSERT OR REPLACE INTO dirs(key, dir_mtime, file_count, size_sum, max_mtime)'
                ' VALUES (?,?,?,?,?)', (dir_key, *fingerprint))
            conn.execute('DELETE FROM entries WHERE dir_key = ?', (dir_key,))
            conn.executemany(
                'INSERT OR REPLACE INTO entries(key, dir_key, mtime, size) VALUES (?,?,?,?)',
                [(k, dir_key, m, s) for k, m, s in entries])

    def drop_dir(self, conn: sqlite3.Connection, dir_key: str) -> List[str]:
        """目录消失：删指纹与该目录的条目，返回被删掉的条目键（供插件 prune 派生缓存）。"""
        with conn:
            rows = conn.execute('SELECT key FROM entries WHERE dir_key = ?', (dir_key,)).fetchall()
            conn.execute('DELETE FROM entries WHERE dir_key = ?', (dir_key,))
            conn.execute('DELETE FROM dirs WHERE key = ?', (dir_key,))
        return [r[0] for r in rows]

    def entry_keys(self, conn: sqlite3.Connection) -> Set[str]:
        try:
            return {r[0] for r in conn.execute('SELECT key FROM entries').fetchall()}
        except sqlite3.Error:
            return set()

    def counts(self, conn: sqlite3.Connection) -> Dict[str, int]:
        try:
            entries = int(conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0])
            dirs = int(conn.execute('SELECT COUNT(*) FROM dirs').fetchone()[0])
        except (sqlite3.Error, TypeError):
            return {'entries': 0, 'dirs': 0}
        return {'entries': entries, 'dirs': dirs}

    # ----- 元信息 -----

    def meta_get(self, conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
        try:
            row = conn.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
        except sqlite3.Error:
            return default
        return row[0] if row else default

    def meta_set(self, conn: sqlite3.Connection, key: str, value: Any) -> None:
        try:
            with conn:
                conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)',
                             (key, str(value)))
        except sqlite3.Error:
            pass

    def clear(self, conn: sqlite3.Connection) -> None:
        """清空指纹库（重建派生缓存时用；不动插件的业务索引）。"""
        try:
            with conn:
                conn.execute('DELETE FROM entries')
                conn.execute('DELETE FROM dirs')
                conn.execute('DELETE FROM meta')
        except sqlite3.Error:
            pass


# ============================================================================
# 决策面：同步 / 校验
# ============================================================================

class FreshnessEngine:
    """一个插件的新鲜度决策器。由壳按需创建（见 `engine_for`），插件不直接持有。"""

    def __init__(self, plugin: Any, spec: FreshnessSpec, store: FreshnessStore) -> None:
        self.plugin = plugin
        self.name = getattr(plugin, 'name', 'unknown')
        self.spec = spec
        self.store = store
        self._lock = threading.Lock()
        # 同步的单飞：verify 有 BackgroundTask 的状态可查，同步没有（它必须同步返回），
        # 所以单独一把非阻塞锁 —— 现状里"查-建-赋值"无锁，双击就是两趟全盘 walk。
        self._sync_lock = threading.Lock()
        self._sync_active = False
        self._last_sync_at = 0.0
        self._task: Optional[BackgroundTask] = None
        self._last_report: Optional[dict] = None

    # ----- 对外状态 -----

    def state(self) -> Dict[str, Any]:
        """给前端的状态投影（不扫盘，只读指纹库与内存标志）。

        指纹库还不存在时**不建库**：否则"看一眼状态"就会给每个声明了 spec 的插件
        凭空造出一个空数据库（与 ThumbCache.stats 同一条理由）。

        远端来源没有条目与目录：额外给出 `mode` / `ttl_seconds` / `ttl_remaining`，
        界面按"数据新鲜期"显示，而不是"已索引 N 项"。
        """
        with self._lock:
            task = self._task
            busy = self._busy_locked()
        base = {
            'plugin': self.name,
            'available': True,
            'busy': busy,
            'task': task.status() if task is not None else None,
            'entries': 0,
            'dirs': 0,
            'content_version': 0,
            'version_stale': True,
            'last_sync_at': 0.0,
            'last_verify_at': 0.0,
            'unit': self.spec.unit,
            'mode': self.spec.mode,
            'ttl_seconds': float(self.spec.ttl_seconds or 0.0),
            'ttl_remaining': 0.0,
            'last_report': self._last_report,
        }
        if not self.store.db_path.exists():
            return base
        try:
            conn = self.store.connect()
        except sqlite3.Error as e:
            return {**base, 'available': False, 'error': str(e)}
        try:
            counts = self.store.counts(conn)
            version = int(self.store.meta_get(conn, 'content_version', 0) or 0)
            last_sync = float(self.store.meta_get(conn, 'last_sync_at', 0) or 0)
            ttl = float(self.spec.ttl_seconds or 0.0)
            base.update({
                'entries': counts['entries'],
                'dirs': counts['dirs'],
                'content_version': version,
                'version_stale': version != self.spec.content_version,
                'last_sync_at': last_sync,
                'last_verify_at': float(self.store.meta_get(conn, 'last_verify_at', 0) or 0),
                'ttl_remaining': (max(0.0, ttl - (time.time() - last_sync)) if ttl > 0 else 0.0),
            })
        finally:
            conn.close()
        return base

    def request_cancel(self) -> bool:
        task = self._task
        if task is not None and task.state in ('queued', 'running'):
            task.cancel()
            return True
        return False

    def progress(self, text: str = '') -> None:
        """插件钩子里的进度文本：写进当前任务（没有任务时忽略）。

        收尾类钩子（`on_pass_end`）跑在任务结束之前、却拿不到任务对象，而它们的
        收尾工作（重建索引之类）可能耗时数秒。没有这句文本，界面会停在"进度满了"
        却仍未结束的状态，看起来像卡住。
        """
        with self._lock:
            task = self._task
        if task is not None:
            task.update(current=text or '')

    def valid_keys(self) -> Set[str]:
        """当前指纹库里的全部有效条目键（一次查询）。

        给"派生数据不是按条目算的"插件用：例如 pixiv-sync 的"已下载作品集合"要从
        **整个**键集合反推（哪些作品下全了、哪些缺页），而不是逐个文件回调。
        在 `on_pass_end` 里调用前先看报告：部分遍历（`partial`）或读取有错时集合
        并不完整，据此重算会把没走到的部分当成"消失了"。
        """
        try:
            conn = self.store.connect()
        except sqlite3.Error:
            return set()
        try:
            return self.store.entry_keys(conn)
        finally:
            conn.close()

    def reset_throttle(self) -> None:
        """清掉去抖时间戳，让下一次同步立即真扫。

        设置变更（根目录增删、解析规则切换）后调用：否则用户改完设置回到视图，
        会被"5 秒内不重复扫"挡掉，表现成"设置要等一会儿才生效"。
        """
        with self._lock:
            self._last_sync_at = 0.0

    # ----- 同步（被动增量） -----

    def sync(self, scope: str = '', force: bool = False) -> Dict[str, Any]:
        """被动增量：目录级短路 + 只对变化的目录逐条比对。

        返回动作报告：`action` ∈ `sync`（真扫了）/ `skip`（去抖或非法作用域）/
        `partial`（预算用尽，剩 `remaining` 个目录交给校验续完）。
        远端来源（`mode='remote'`）没有文件可扫，走 TTL 判定，见 `_remote_pass`。
        """
        if self.spec.mode == 'remote':
            return self._remote_pass(force=force)
        now = time.time()
        with self._lock:
            if self._busy_locked():
                return {'action': 'skip', 'reason': 'busy'}
            if not force and (now - self._last_sync_at) < self.spec.min_sync_interval:
                return {'action': 'skip', 'reason': 'throttled',
                        'retry_after': round(self.spec.min_sync_interval - (now - self._last_sync_at), 2)}
            self._last_sync_at = now
            if not self._sync_lock.acquire(blocking=False):
                return {'action': 'skip', 'reason': 'busy'}
            self._sync_active = True

        try:
            if (scope or '').strip().strip('/'):
                resolved = self.spec.source.resolve_scope(scope)
                if resolved is None:
                    return {'action': 'skip', 'reason': 'bad_scope', 'scope': scope}
                start_key, start_dir = resolved
            else:
                # 空作用域 = 全部根：交给 _pass 自己播种，不要求第一根此刻存在
                start_key, start_dir = '', None
            conn = self.store.connect()
            try:
                known = self.store.dirs(conn)
                # 解析规则变了 → 本趟把全部条目当"新增"，让插件重算派生数据；
                # 只有**整趟走完**才盖章，否则下一趟仍会重算未覆盖的部分（幂等）。
                version = int(self.store.meta_get(conn, 'content_version', 0) or 0)
                stale_version = version != self.spec.content_version
                report = self._pass(conn, start_key, start_dir, known,
                                    max_dirs=self.spec.max_dirs_per_sync, task=None,
                                    audit=False, force_all=stale_version)
                self.store.meta_set(conn, 'last_sync_at', time.time())
                if stale_version and not report['partial'] and start_dir is None:
                    # 只有"全部根、整趟走完"才盖章：作用域是子目录时盖章会让其他目录
                    # 永远等不到重算（版本升级后用户随手点开一个文件夹就会踩到）。
                    self.store.meta_set(conn, 'content_version', self.spec.content_version)
            finally:
                conn.close()
        finally:
            with self._lock:
                self._sync_active = False
                self._sync_lock.release()
        report['action'] = 'partial' if report['partial'] else 'sync'
        report['scope'] = scope
        report['force_all'] = bool(stale_version)
        # `report['reason']` 由 `_pass` 填：`budget`（还有活没干完，值得续跑）/
        # `dir_cap`（只是没走完，不该反复起任务）/ `cancelled` / 空串
        self._last_report = report
        return report

    # ----- 校验（手动全量） -----

    def start_verify(self) -> Dict[str, Any]:
        """启动全量校验的后台任务（单飞；已在跑时返回它的状态而不是再起一个）。"""
        with self._lock:
            task = self._task
            if task is not None and task.state in ('queued', 'running'):
                return {'started': False, 'running': True, **task.status()}
            task = BackgroundTask(kind=f'freshness.{self.name}', extra={'plugin': self.name})
            self._task = task
        # BackgroundTask.start 的约定是 `worker_fn(task, *args)`：任务对象由它注入，
        # 这里**不能**再手动传一次（会变成 verify(task, task) → TypeError，
        # 而异常只落在任务的 errors 里，表现为"校验点了没反应而且状态是 done"）。
        task.start(self.verify)
        return {'started': True, 'running': True, **task.status()}

    def verify(self, task: Optional[BackgroundTask] = None) -> Dict[str, Any]:
        """全量校验：全量遍历 + 逐项指纹校验 + 幽灵清理 + 派生缓存对账。

        也是 `BackgroundTask` 的 worker 本体（`start_verify` 就是把它丢进后台），
        因此可以直接调用做测试 —— 走的是同一条路径。
        远端来源：忽略 TTL，直接丢弃插件的本地缓存（见 `_remote_pass`）。
        """
        if self.spec.mode == 'remote':
            return self._remote_pass(force=True, task=task)
        with self._lock:
            if task is None:
                if self._busy_locked():
                    return {'action': 'skip', 'reason': 'busy'}
                # 同步调用（测试/脚本）：用一个游离任务承载进度语义，**不**占 self._task，
                # 否则一个从未 start() 的任务会让 state() 永远报 busy。
                task = BackgroundTask(kind=f'freshness.{self.name}')
        conn = self.store.connect()
        try:
            known = self.store.dirs(conn)
            # 解析规则变了 → 全部条目视为"变化"，让插件重算派生数据
            version = int(self.store.meta_get(conn, 'content_version', 0) or 0)
            stale_version = version != self.spec.content_version
            report = self._pass(conn, '', None, known, max_dirs=None, task=task,
                                audit=True, force_all=stale_version)
            if task.cancelled:
                report['cancelled'] = True
                return report
            if stale_version:
                self.store.meta_set(conn, 'content_version', self.spec.content_version)
            self.store.meta_set(conn, 'last_verify_at', time.time())
        finally:
            conn.close()
        report['action'] = 'verify'
        report['content_version'] = self.spec.content_version
        report['force_all'] = stale_version
        self._last_report = report
        if task is not None:
            # 任务的 extra 带上整趟摘要：既有通用计数，也有插件 pass_end 钩子返回的
            # 业务计数（media-player 的 audio/video —— 前端与调试脚本按它显示"完成"）
            extra = {k: report[k] for k in ('added', 'changed', 'removed', 'pruned')}
            end = report.get('pass_end') or {}
            extra.update(end if isinstance(end, dict) else {})
            task.update(total=report['dirs'], processed=report['dirs'],
                        current='', extra=extra)
        return report

    # ----- 逃生门：重建派生缓存 -----

    def rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """丢弃派生缓存后重跑一次全量校验（插件的 `rebuild` 钩子负责清）。

        为什么保留这个入口：指纹不可信的场景（外部工具保留了 mtime、缓存写入
        被截断）只能靠"重算一遍"解决。但它**不是**工具栏按钮 —— 见设计文档。
        """
        with self._lock:
            if self._busy_locked():
                return {'started': False, 'running': True}
        cleared: Any = None
        if self.spec.rebuild is not None:
            try:
                cleared = self.spec.rebuild(kind)
            except Exception as e:  # 插件钩子不可信
                log.error(f'[{self.name}] freshness.rebuild 钩子失败: {e}')
                return {'started': False, 'error': str(e)}
        conn = self.store.connect()
        try:
            self.store.clear(conn)
            self.store.meta_set(conn, 'content_version', self.spec.content_version)
        finally:
            conn.close()
        return {'started': True, 'cleared': cleared, **self.start_verify()}

    # ----- 内部 -----

    def _remote_pass(self, *, force: bool, task: Optional[BackgroundTask] = None) -> Dict[str, Any]:
        """远端来源的一趟：新鲜期内跳过；过期或 `force` 时让插件丢弃本地缓存。

        语义必须在文档里说清：远端数据**发现不了"变化"**，判据只有时间 ——
        `sync` 是"到时重取"，`verify` 是"立刻重取"（忽略 TTL）。二者都不联网：
        真正取数由插件在用户进入相应视图时按需做，`invalidate` 只负责让那份取数
        不再命中旧缓存。
        """
        conn = self.store.connect()
        try:
            last = float(self.store.meta_get(conn, 'last_sync_at', 0) or 0)
            ttl = float(self.spec.ttl_seconds or 0.0)
            left = max(0.0, ttl - (time.time() - last)) if ttl > 0 else 0.0
            if not force and left > 0:
                return {'action': 'skip', 'reason': 'fresh', 'ttl_remaining': round(left, 1)}
            cleared: Any = None
            if self.spec.invalidate is not None:
                if task is not None:
                    task.update(total=1, processed=0, current='丢弃本地缓存')
                try:
                    cleared = self.spec.invalidate()
                except Exception as e:
                    log.error(f'[{self.name}] freshness.invalidate 失败: {e}')
                    return {'action': 'error', 'error': str(e)}
            stamp = time.time()
            self.store.meta_set(conn, 'last_sync_at', stamp)
            if force:
                self.store.meta_set(conn, 'last_verify_at', stamp)
        finally:
            conn.close()
        report = {'action': 'verify' if force else 'sync', 'mode': 'remote',
                  'invalidated': True, 'cleared': cleared, 'ttl_seconds': ttl}
        if task is not None:
            task.update(total=1, processed=1, current='', extra={'invalidated': True})
        self._last_report = report
        return report

    def _busy_locked(self) -> bool:
        """是否有同步或校验在跑（调用方必须已持有 self._lock）。"""
        task = self._task
        return self._sync_active or (task is not None and task.state in ('queued', 'running'))

    def _pass(self, conn: sqlite3.Connection, start_key: str, start_dir: Optional[Path],
              known: Dict[str, Tuple[float, int, int, float]], *, max_dirs: Optional[int],
              task: Optional[BackgroundTask], audit: bool,
              force_all: bool = False) -> Dict[str, Any]:
        """一趟遍历：广度优先，目录级短路，变了才逐条比对。

        `max_dirs=None` 表示不限（校验）；否则到预算就停，返回 `partial=True`。
        `audit=True` 时额外做"目录消失"的清理与派生缓存对账（需要整趟走完才算数，
        所以部分遍历不做）。
        """
        source = self.spec.source
        report: Dict[str, Any] = {'added': 0, 'changed': 0, 'removed': 0, 'dirs': 0,
                                  'skipped_dirs': 0, 'pruned': 0, 'errors': [],
                                  'partial': False, 'reason': '',
                                  'truncated_errors': 0}
        queue: deque = deque()
        if start_dir is not None:
            queue.append((start_key, start_dir))
        else:
            for index, root in enumerate(source.roots):
                queue.append((source.key_of(index, ''), Path(root)))
        seen: Set[str] = set()
        changed_entries: List[Dict[str, Any]] = []
        removed_keys: List[str] = []
        # 预算按**干了活的目录**算，不按走过的目录算：短路目录只花一次 scandir，
        # 而"还有活没干"才是 `partial` 要表达的意思。按走过的目录算会让"目录数 >
        # max_dirs"的库**永远**报 partial（每次进视图都弹一次"未覆盖全部目录"），
        # 而稳态下其实一个目录都没变。
        work_dirs = 0
        visited_cap = None if max_dirs is None else max_dirs * _VISITED_DIR_FACTOR

        while queue:
            if task is not None and task.cancelled:
                report['partial'] = True
                report['reason'] = 'cancelled'
                break
            if max_dirs is not None and work_dirs >= max_dirs:
                report['partial'] = True
                report['reason'] = 'budget'
                break
            if visited_cap is not None and report['dirs'] >= visited_cap:
                # 硬上限：一次同步请求不能无限走。这里只是"没走完"，不代表还有活
                # 在排队（走到的目录都校对过），因此前端不该为它自动起全量校验
                report['partial'] = True
                report['reason'] = 'dir_cap'
                break
            dir_key, dir_path = queue.popleft()
            if dir_key in seen:
                continue
            seen.add(dir_key)
            report['dirs'] += 1
            # 进度对**每个走过的目录**推进：只在"条目有增删改"的分支里更新，
            # 会让稳态下的校验整趟停在 0/0（用户看到进度条不动，以为卡死）
            if task is not None:
                task.update(processed=report['dirs'],
                            total=report['dirs'] + len(queue),
                            current=dir_key or '/')

            try:
                with os.scandir(dir_path) as it:
                    subdirs: List[Tuple[str, Path]] = []
                    names: List[str] = []
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                if not source.skips(entry.name):
                                    subdirs.append((entry.name, Path(entry.path)))
                            elif source.wants(entry.name):
                                names.append(entry.name)
                        except OSError:
                            continue
            except OSError as e:
                self._add_error(report, f'{dir_key}: {e}')
                continue

            for name, path in subdirs:
                queue.append((f'{dir_key}/{name}' if dir_key else name, path))

            stored = None if force_all else known.get(dir_key)
            # 目录级短路：mtime 与"纳入索引的名字个数"都没变 → 整目录跳过，零 stat。
            # **校验不做短路**（audit=True）：它的定义就是"逐项 stat 一遍"，短路会让
            # 原地写入永远查不出来。stat_entries=True 的插件放弃这条快路。
            if not audit and stored is not None and not self.spec.stat_entries \
                    and self._unchanged(dir_path, stored, len(names)):
                report['skipped_dirs'] += 1
                continue

            entries, fingerprint, error = self._collect(dir_key, dir_path, names)
            work_dirs += 1        # 这一趟真花了钱：算进预算（短路目录不算）
            if error:
                self._add_error(report, f'{dir_key}: {error}')

            old = self.store.entries_of(conn, dir_key) if stored is not None else {}
            added = [k for k in entries if k not in old]
            changed = [k for k in entries if k in old and old[k] != entries[k][1:]]
            gone = [k for k in old if k not in entries]
            removed_keys.extend(gone)
            report['added'] += len(added)
            report['changed'] += len(changed)
            report['removed'] += len(gone)
            if not (added or changed or gone):
                # 指纹变了但条目没变（例如只是 touched / 子目录增删）：只更新指纹
                self.store.replace_dir(conn, dir_key, fingerprint,
                                       [(k, v[1], v[2]) for k, v in entries.items()])
                continue

            if audit:
                # 校验：逐个差异条目交给插件重算（这是"昂贵解析只发生在变化项"的落点）
                for key in added + changed:
                    mtime, size = entries[key][1], entries[key][2]
                    changed_entries.append({'key': key, 'path': entries[key][0],
                                            'mtime': mtime, 'size': size, 'dir': dir_key})
            else:
                # 同步：本趟只处理当前作用域，把差异交给插件；预算内不落"半份指纹"
                for key in added + changed:
                    changed_entries.append({'key': key, 'path': entries[key][0],
                                            'mtime': entries[key][1], 'size': entries[key][2],
                                            'dir': dir_key})
            self.store.replace_dir(conn, dir_key, fingerprint,
                                   [(k, v[1], v[2]) for k, v in entries.items()])

        # 目录消失：全部根、整趟走完、无读取错误才做。
        # 为什么同步也要做（不只是校验）：目录改名/整目录删除时，新目录的条目是
        # "新增"，旧目录的条目只会在这一步被清掉 —— 少了它，被动同步会报"新增了 3 个"
        # 却把旧的 3 个永远留在库里。部分遍历（预算用尽）与子目录作用域都不能做：
        # "没见到"不等于"没了"。
        if start_dir is None and not report['partial'] and not report['errors']:
            vanished = [k for k in known if k not in seen]
            dropped_entries = 0
            for dir_key in vanished:
                keys = self.store.drop_dir(conn, dir_key)
                removed_keys.extend(keys)
                dropped_entries += len(keys)
            report['removed'] += dropped_entries
            report['vanished_dirs'] = len(vanished)

        report['pruned'] = self._call_prune(removed_keys)
        report['derived'] = self._call_derive(changed_entries, task)
        # 对账只在"整趟走完且没有读取错误"时做：keys 不完整时按它清孤儿，
        # 会把只是暂时读不到的目录下的有效派生缓存一起删掉。
        if audit and not report['partial'] and not report['errors']:
            report['audited'] = self._call_verified(conn)
        report['pass_end'] = self._call_pass_end(report, audit)
        return report

    def _unchanged(self, dir_path: Path, stored: Tuple[float, int, int, float],
                   name_count: int) -> bool:
        """目录级短路判据：目录 mtime + 纳入索引的名字个数。

        不 stat 任何文件 —— 这是稳态开销的全部来源。原地覆盖写（同名、新内容）
        不改这两项，被动同步看不见它（见模块头部的"代价与边界"）。
        """
        try:
            dir_mtime = dir_path.stat().st_mtime
        except OSError:
            return False
        return abs(dir_mtime - stored[0]) < 0.5 and stored[1] == name_count

    def _entry_key(self, abs_path: Path, virtual: str) -> str:
        """条目键：插件的 `key_of(绝对路径)` 优先，默认用虚拟路径。"""
        if self.spec.key_of is None:
            return virtual
        try:
            return str(self.spec.key_of(str(abs_path)))
        except Exception as e:
            log.error(f'[{self.name}] freshness.key_of 失败，退回虚拟路径: {e}')
            return virtual

    def _collect(self, dir_key: str, dir_path: Path, names: Sequence[str]
                 ) -> Tuple[Dict[str, Tuple[str, float, int]], Tuple[float, int, int, float], str]:
        """逐条 stat（只在目录短路失败后发生）。

        键是**插件对外用的条目键**（默认虚拟路径 `目录键/文件名`，或 `key_of()` 的
        结果）：派生缓存的键（缩略图键、索引键）就是它，用裸文件名会让所有子目录的
        条目互相覆盖。

        返回 ({键: (物理路径, mtime, size)}, 目录指纹, 最后一个错误)。
        """
        entries: Dict[str, Tuple[str, float, int]] = {}
        size_sum = 0
        max_mtime = 0.0
        error = ''
        for name in names:
            path = dir_path / name
            try:
                st = path.stat()
            except OSError as e:
                error = str(e)
                continue
            virtual = f'{dir_key}/{name}' if dir_key else name
            entries[self._entry_key(path, virtual)] = (str(path), st.st_mtime, st.st_size)
            size_sum += st.st_size
            max_mtime = max(max_mtime, st.st_mtime)
        try:
            dir_mtime = dir_path.stat().st_mtime
        except OSError:
            dir_mtime = 0.0
        return entries, (dir_mtime, len(entries), size_sum, max_mtime), error

    def _call_derive(self, items: List[Dict[str, Any]],
                     task: Optional[BackgroundTask]) -> Dict[str, Any]:
        if not items or self.spec.derive is None:
            return {'count': 0, 'errors': []}
        if task is not None:
            task.update(current=f'更新派生数据 {len(items)} 项')
        try:
            out = self.spec.derive(items)
        except Exception as e:
            log.error(f'[{self.name}] freshness.derive 失败: {e}')
            return {'count': 0, 'errors': [str(e)]}
        if isinstance(out, dict):
            return {'count': int(out.get('count') or len(items)),
                    'errors': list(out.get('errors') or [])[: _ERROR_CAP]}
        return {'count': len(items), 'errors': []}

    def _call_prune(self, keys: List[str]) -> int:
        if not keys or self.spec.prune is None:
            return 0
        try:
            out = self.spec.prune(keys)
        except Exception as e:
            log.error(f'[{self.name}] freshness.prune 失败: {e}')
            return 0
        if isinstance(out, int):
            return out
        return len(keys)

    def _call_verified(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """全量对账：把"当前有效键集合"交给插件，由它清理自己数据面里的孤儿。

        与 `prune` 的区别：prune 处理"这一趟发现的消失"，对账兜住"插件没运行时
        就被删掉的文件"留下的历史孤儿（media-player 的深度扫描就是这么做的，
        现在所有插件共用同一条路径）。
        """
        if self.spec.on_verified is None:
            return {}
        keys = self.store.entry_keys(conn)
        try:
            out = self.spec.on_verified(keys)
        except Exception as e:
            log.error(f'[{self.name}] freshness.on_verified 失败: {e}')
            return {'error': str(e)}
        return out if isinstance(out, dict) else {}

    def _call_pass_end(self, report: Dict[str, Any], audit: bool) -> Dict[str, Any]:
        """整趟结束的收尾钩子（同步与校验都会调，含部分遍历）。"""
        if self.spec.on_pass_end is None:
            return {}
        try:
            out = self.spec.on_pass_end(report, audit)
        except Exception as e:
            log.error(f'[{self.name}] freshness.on_pass_end 失败: {e}')
            return {'error': str(e)}
        return out if isinstance(out, dict) else {}

    @staticmethod
    def _add_error(report: Dict[str, Any], message: str) -> None:
        if len(report['errors']) < _ERROR_CAP:
            report['errors'].append(message)
        else:
            report['truncated_errors'] += 1


# ============================================================================
# 注册表：壳按插件名取引擎（与 thumb_cache.all_caches 同一模式）
# ============================================================================

_ENGINES: 'weakref.WeakValueDictionary[str, FreshnessEngine]' = weakref.WeakValueDictionary()
_ENGINES_LOCK = threading.Lock()


def engine_for(plugin: Any) -> Optional[FreshnessEngine]:
    """取（或按需创建）某个插件的引擎；插件未声明 `freshness_spec()` 时返回 None。

    为什么缓存在插件实例上：规格里的 `roots` 往往是"当前设置"的函数，引擎必须跟着
    插件实例走；同时引擎又不能吊住插件（弱引用表只用于壳级遍历）。
    """
    existing = getattr(plugin, '_freshness_engine', None)
    if existing is not None:
        return existing
    try:
        spec = FreshnessSpec.normalize(plugin, plugin.freshness_spec())
    except Exception as e:
        log.error(f'[{getattr(plugin, "name", "?")}] freshness_spec 非法: {e}')
        return None
    if spec is None:
        return None
    try:
        store = FreshnessStore(Path(plugin.get_cache_dir()) / 'freshness.db')
    except Exception as e:
        # 缓存目录取不到（例如插件的数据根来自自己的设置项、而配置里没有
        # `directories.data_root`）：退化成"本插件不参与"，而不是把异常抛进壳的
        # API 线程。日志留痕，插件作者照着 `get_cache_dir()` 覆写即可。
        log.error(f'[{getattr(plugin, "name", "?")}] 无法确定缓存目录，跳过刷新基建: {e}')
        return None
    engine = FreshnessEngine(plugin, spec, store)
    try:
        plugin._freshness_engine = engine
    except Exception:  # __slots__ 之类：退化成每次新建也比报错好
        pass
    with _ENGINES_LOCK:
        _ENGINES[engine.name] = engine
    return engine


def all_engines() -> List[FreshnessEngine]:
    """当前进程内所有引擎的快照（壳级统计/批量状态用）。"""
    with _ENGINES_LOCK:
        return list(_ENGINES.values())


def drop_engine(plugin_name: str) -> None:
    """插件卸载时把引擎移出注册表（指纹库是磁盘文件，不需要额外收尾）。"""
    with _ENGINES_LOCK:
        _ENGINES.pop(plugin_name, None)
