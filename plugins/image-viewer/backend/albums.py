"""相册树与全树索引的生命周期。

这里只回答"索引什么时候该重算、存到哪"：TTL 缓存、目录集合快照的获取、索引缓存的
版本与落盘、重建代次与各阶段耗时诊断。两份计算留在别的组件里：**单个目录的条目怎么
算、怎么复用**在 `entries.DirEntryCache`，**一次重建怎么把目录集合聚合成相册树**在
`tree.AlbumTreeBuilder` —— 条目存储同时被按需列表与全树重建写入，是两条路径互为缓存
的接点；聚合是纯计算，与缓存的失效判据分开才不会互相牵动。

**不预生成缩略图**：预生成上万次随机小文件 I/O 得不偿失，交给 `/thumbs` 按需生成。
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.json_cache import read_cached_json, write_cached_json

log = logging.getLogger(__name__)


class AlbumIndex:
    """相册树、全树聚合与索引缓存的生命周期。"""

    def __init__(self, paths: Any, snapshot: Any, entries: Any, config: Any,
                 limits: Any, tree: Any) -> None:
        self._paths = paths
        self._snapshot = snapshot
        self._entries = entries
        self._config = config
        self._tree = tree
        self._ttl = limits.albums_ttl
        self._version = limits.album_cache_version
        self._cached: Optional[Dict[str, Any]] = None
        self._cached_at = 0.0
        self._calls = 0
        self._last_ms = 0.0
        self._stage_ms: Dict[str, float] = {}
        self._last_build_scans = 0
        self._last_build_changed = 0
        self._breakdown: Dict[str, Any] = {}
        # 代次：每次作废 +1。一次重建跑在旧代次上时，它算出的条目可能已经落后于
        # 作废它的事件，写回会把新状态覆盖成旧的（见 `_finish`）。
        self._generation = 0
        self._load_index()

    # ===== 生命周期 =====

    def reset_index(self) -> None:
        """丢掉整个相册索引（换根、全量重建、校验重建时用）。"""
        self._generation += 1
        self._entries.clear()
        self._cached = None
        self._cached_at = 0.0

    def invalidate(self, keep_snapshot: bool = False) -> None:
        """让下一次 `list_albums` 强制重算（刷新/重建/相册配置变更时调用）。

        `keep_snapshot=False`（默认）连目录快照一起作废：调用点都是"目录集合可能变了"
        或"有已知的整份变化"（改根目录、删除/移动、校验、重建），此时重走一次目录
        枚举正是需要的，落盘的那份快照一并删掉，否则下次启动又会读到它。

        `keep_snapshot=True` 只丢条目级缓存与落盘索引：被动同步已经按"变化的目录 +
        祖先"精确丢过条目，目录集合本身没变，保住快照就不必重走全树；目录集合真变了
        由引擎的目录集合钩子单独调一次完整作废。

        顺带作废路径组件的根集合缓存：命名空间 token 要避开第一根的一级子目录名，
        目录集合变了 token 可能跟着变，否则缩略图库的键会与新 token 对不上。
        """
        self._generation += 1
        self._cached = None
        self._cached_at = 0.0
        if not keep_snapshot:
            self._snapshot.reset()
        self._drop_index_file()
        self._paths.invalidate()

    # ===== 落盘 =====

    def _load_index(self) -> None:
        """读取落盘的相册索引（版本不符时整份作废）。

        启动时就要读：条目里的目录 mtime 与全树快照对得上时，整棵树一次都不重扫 ——
        这是"重启后打开相册页 0.08s"的一半来源。
        """
        payload = read_cached_json(self._paths.album_cache_file, version=self._version)
        if payload is not None and isinstance(payload.get('dirs'), dict):
            self._entries.replace(payload['dirs'])

    def _drop_index_file(self) -> None:
        """删掉落盘的相册索引。

        内存里的条目会随"变化的目录 + 祖先"被丢掉（`DirEntryCache.drop_nodes`），
        落盘的那份却还带着旧行；进程此时退出，下次启动会读到一份缺了失效行的索引，
        表现是计数与封面停在旧状态且没有任何东西提示它过期。索引只是缓存，删掉即可，
        下一次重建会重新写一份。
        """
        try:
            if self._paths.album_cache_file.exists():
                self._paths.album_cache_file.unlink()
        except OSError as exc:
            log.warning('[ImageViewer] 删除相册索引失败: %s', exc)

    def _save_index(self) -> None:
        """把索引原子落盘（版本号写进同一份 JSON，读取时逐项核对）。

        序列化前先整体快照条目字典，再逐条浅拷贝：`json.dump` 遍历原字典时，另一条
        线程（API 线程 / 被动同步）可能正在写入，遍历活字典会在中途抛
        `RuntimeError: dictionary changed size during iteration` 并留下一次失败的
        落盘。`dict(映射)` 在 C 层一次取走键值，之后的遍历只碰这份副本；每个条目是
        扁平 dict，再按条目拷贝一层，`json.dump` 看到的也是稳定内容。
        """
        if not self._paths.cache_dir.is_dir():
            return
        entries = dict(self._entries.entries)
        snapshot = {key: dict(value) for key, value in entries.items()
                    if isinstance(value, dict)}
        if not write_cached_json(self._paths.album_cache_file,
                                 {'dirs': snapshot},
                                 version=self._version):
            log.error('[ImageViewer] 保存相册索引失败: %s', self._paths.album_cache_file)

    # ===== 相册树 =====

    def list_albums(self) -> Dict:
        """相册列表：**有目录快照就用它，没有才走盘**。

        走盘（全树 os.walk）在机械盘图库上实测 6-23s，而它换来的只是"有哪些目录 +
        各自 mtime"。所以策略是：

        - 进程内有结果 → 直接返回（TTL 内）；
        - 有全树目录快照（本进程走过盘，或启动时从 `dirs_index.json` 读回来的）→
          用它重建，**纯内存**，不碰磁盘；
        - 没有快照（首次运行 / 刚被作废 / 换过根）→ 当场走一趟再重建。这是唯一会
          等走盘的路径，而它只在"确实有变化"（被动同步作废、校验、删除移动）时出现。

        实测：正常启动进相册页 0.08-0.15s（读落盘快照）；只有"索引刚被作废"那一帧
        要等走盘。
        """
        now = time.time()
        self._calls += 1
        started = time.perf_counter()
        stages: Dict[str, float] = {}
        self._stage_ms = stages
        try:
            def mark(name: str, since: float) -> float:
                tick = time.perf_counter()
                stages[name] = round((tick - since) * 1000, 1)
                return tick

            tick = time.perf_counter()
            if self._cached is not None and (now - self._cached_at) < self._ttl:
                return {**self._cached, 'cached': True}
            tick = mark('ttl_check', tick)

            if self._config.prune():
                # 可见性标记被清理过 → 索引条目不可信，整份丢掉重扫
                self._entries.clear()
            tick = mark('prune_marks', tick)

            dirs = self._snapshot.snapshot(now)
            tick = mark('dirs_snapshot', tick)
            if dirs is None:
                # 没有可用快照：当场走一趟（唯一会等走盘的路径）
                dirs = self._snapshot.ensure()
            tick = mark('dirs_resolve', tick)

            result = self._finish(dirs, now)
            mark('finish_albums', tick)
            stages['build_scan_count'] = float(self._last_build_scans)
            stages['build_changed'] = float(self._last_build_changed)
            return result
        finally:
            self._last_ms = (time.perf_counter() - started) * 1000

    def _finish(self, dirs: dict, now: float) -> Dict:
        """跑 `_build` 并落缓存（`list_albums` 的两条分支共用）。

        取条目与回写之间不放锁：加锁会让同步线程阻塞在一次目录枚举上。代价是这段里
        可能被 `invalidate()` / `reset_index()` 打断 —— 那时代次已经变了，本次算出的
        条目落后于作废它的事件，写回会把新状态覆盖成旧的，并给旧结果盖上新的缓存
        时间戳（下一次读取因此再陈旧一整个 TTL）。所以回写前比对代次：对不上就只把
        结果返回给本次调用，不写条目、不落盘、不设缓存，下一帧自然重建。
        """
        generation = self._generation
        self._last_build_scans = 0
        self._entries.reset_misses()
        started = time.perf_counter()
        albums, entries, changed = self._build(dirs, self._entries.entries)
        build_ms = (time.perf_counter() - started) * 1000
        self._last_build_scans = self._entries.misses
        self._last_build_changed = changed
        result = {'albums': albums, 'config': self._config.data, 'changed': changed}
        if generation != self._generation:
            self._stage_ms.update({'build': round(build_ms, 1), 'stale': 1.0})
            return result
        assign_started = time.perf_counter()
        self._entries.replace(entries)
        assign_ms = (time.perf_counter() - assign_started) * 1000
        save_started = time.perf_counter()
        if changed:
            self._save_index()
        save_ms = (time.perf_counter() - save_started) * 1000
        self._cached_at = now
        self._cached = result
        self._stage_ms.update({
            'build': round(build_ms, 1),
            'assign': round(assign_ms, 1),
            'save_cache': round(save_ms, 1),
        })
        return result

    def _build(self, dirs: dict, cache_dirs: dict) -> Tuple[List[Dict], Dict, int]:
        """跑一次全树构建（实现与聚合规则见 `tree.AlbumTreeBuilder`）。"""
        albums, entries, changed, breakdown = self._tree.build(dirs, cache_dirs)
        self._breakdown = breakdown
        return albums, entries, changed

    # ===== 诊断 =====

    @property
    def cache_version(self) -> int:
        """当前索引缓存的版本号（用例与诊断断言用）。"""
        return self._version

    @property
    def dir_entries(self) -> Dict[str, Any]:
        """索引的目录级条目（只读诊断用途）。"""
        return self._entries.entries

    @property
    def cached_at(self) -> float:
        """上次成功重建索引的时间戳（诊断与"不该重扫"断言用）。"""
        return self._cached_at

    @property
    def albums_cached(self) -> bool:
        """TTL 缓存当前是否有效。"""
        return self._cached is not None

    def diagnostics(self) -> Dict[str, Any]:
        """诊断用：索引条目、调用与各阶段耗时、重建重扫目录数。"""
        return {
            'album_index_entries': len(self.dir_entries),
            'album_index_version': self.cache_version,
            'expected_version': self._version,
            'list_albums_calls': self._calls,
            'list_albums_last_ms': round(self._last_ms, 1),
            'list_albums_stages_ms': dict(self._stage_ms),
            'last_build_scans': self._last_build_scans,
            'build_breakdown': dict(self._breakdown),
            'albums_cached': self.albums_cached,
        }
