"""相册树与全树索引的生命周期。

这里只回答"索引什么时候该重算、算什么、存到哪"：TTL 缓存、目录集合快照的获取、索引
缓存的版本与落盘、各阶段耗时诊断。**单个目录的条目怎么算、怎么复用**在
`entries.DirEntryCache` 里 —— 那份条目存储同时被按需列表与全树重建写入，是两条路径
互为缓存的接点。

全树聚合的规则（决定相册树的计数与封面）：

- 直接子目录有图片就取该目录的 p0；只有更深层才有图片就继续往下递归（否则
  「作者/作品/1.jpg」会被算成 0 张）；
- 代表封面按 Pixiv 作品号倒序取第一个非空封面（= 最近的作品）；非 Pixiv 目录取 mtime
  最新、同 mtime 比名字；
- 同深度时命名空间节点（虚拟）先算：它与合成根同为 depth 0，而合成根要把额外根目录的
  图片并进相册树总数，必须等它们的结果先出来；
- 除图片张数外还聚合**更新单元数**（`unit_count`）：同一目录里同作品号的多页、连号页码
  的一本书在时间线里只占一张卡，所以单元数要像张数一样自底向上汇总，且两者必须用同一份
  分组实现（`naming.unit_groups`），否则「最近更新」的单元总数与扫描出来的单元数会对不上。

**不预生成缩略图**：预生成上万次随机小文件 I/O 得不偿失，交给 `/thumbs` 按需生成。
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.json_cache import read_cached_json, write_cached_json

log = logging.getLogger(__name__)


class AlbumIndex:
    """相册树、全树聚合与索引缓存的生命周期。"""

    def __init__(self, paths: Any, snapshot: Any, entries: Any, settings: Any,
                 config: Any, naming: Any, limits: Any, books: Any = None) -> None:
        self._paths = paths
        self._snapshot = snapshot
        self._entries = entries
        self._settings = settings
        self._config = config
        self._naming = naming
        self._books = books
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
        self._load_index()

    # ===== 生命周期 =====

    def reset_index(self) -> None:
        """丢掉整个相册索引（换根、全量重建、校验重建时用）。"""
        self._entries.clear()
        self._cached = None
        self._cached_at = 0.0

    def invalidate(self) -> None:
        """让下一次 `list_albums` 强制重扫（刷新/重建/相册配置变更时调用）。

        调用点都是"已知有变化"（被动同步发现目录变动、校验、删除/移动、改根目录），
        此时重走一次目录枚举正是需要的；落盘的那份目录快照一并删掉，否则下次启动
        又会读到它。

        顺带作废路径组件的根集合缓存：命名空间 token 要避开第一根的一级子目录名，
        目录集合变了 token 可能跟着变，否则缩略图库的键会与新 token 对不上。
        """
        self._cached = None
        self._cached_at = 0.0
        self._snapshot.reset()
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

    def _save_index(self) -> None:
        """把索引原子落盘（版本号写进同一份 JSON，读取时逐项核对）。"""
        if not self._paths.cache_dir.is_dir():
            return
        if not write_cached_json(self._paths.album_cache_file,
                                 {'dirs': self._entries.entries},
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

        取条目与回写之间不放锁：被动同步在这段里作废索引时，本帧可能落到一份陈旧
        结果 —— 但它同时把 TTL 缓存清成 None，所以下一帧就会用新条目重建，不会一直
        陈旧下去。加锁反而会让同步线程阻塞在一次目录枚举上。
        """
        self._last_build_scans = 0
        self._entries.reset_misses()
        started = time.perf_counter()
        albums, entries, changed = self._build(dirs, self._entries.entries)
        build_ms = (time.perf_counter() - started) * 1000
        self._last_build_scans = self._entries.misses
        self._last_build_changed = changed
        assign_started = time.perf_counter()
        self._entries.replace(entries)
        assign_ms = (time.perf_counter() - assign_started) * 1000
        save_started = time.perf_counter()
        if changed:
            self._save_index()
        save_ms = (time.perf_counter() - save_started) * 1000
        result = {'albums': albums, 'config': self._config.data, 'changed': changed}
        self._cached_at = now
        self._cached = result
        self._stage_ms.update({
            'build': round(build_ms, 1),
            'assign': round(assign_ms, 1),
            'save_cache': round(save_ms, 1),
        })
        return result

    def _build(self, dirs: dict, cache_dirs: dict) -> Tuple[List[Dict], Dict, int]:
        """直接扫描变化目录，再自底向上聚合出递归统计。

        `dirs` 是全部根目录的 {虚拟路径: mtime}。额外根目录的顶层节点是**虚拟**的
        （`__<命名空间>`），磁盘上不存在，这里为它合成一个只含 children 的条目，
        使聚合循环与第一根走同一条路径。
        """
        cache_dirs = cache_dirs or {}
        entries: Dict[str, Dict] = {}
        changed = 0
        flags_started = time.perf_counter()
        # 一次算完全树（见 `SettingsResolver.compute_pixiv_flags`）：逐个问 `pixiv_mode()`
        # 会让每个目录各走一遍设置继承链，9168 个目录实测 2.7-3.4s
        pixiv_flags = self._settings.compute_pixiv_flags(list(dirs))
        flags_ms = (time.perf_counter() - flags_started) * 1000
        loop_started = time.perf_counter()
        # 先建合成根条目：它的 children 要包含命名空间节点，聚合循环才把额外根目录的
        # 图片算进相册树总数（albums[''].image_count）
        synthetic_children: List[str] = []
        for rel, mtime in dirs.items():
            if self._paths.is_namespace_node(rel) or rel != '':
                continue
            root_entry = cache_dirs.get('') if isinstance(cache_dirs.get(''), dict) else None
            if root_entry and root_entry.get('mtime') is not None \
                    and abs(float(root_entry.get('mtime', 0)) - float(mtime)) < 0.5 \
                    and not root_entry.get('virtual'):
                entries[''] = root_entry
            else:
                # 走带缓存的扫描：与列表路径共用同一份条目，避免"相册索引重建"与
                # "列一次目录"各扫一遍同一个目录
                entries[''] = self._entries.scan('', self._paths.root, mtime)
                changed += 1
            break
        for rel, mtime in dirs.items():
            if self._paths.is_namespace_node(rel):
                # 虚拟命名空间节点：没有直接图片，children = 该根的一级子目录
                root, _ = self._paths.split_virtual(rel)
                children: List[str] = []
                if root is not None:
                    try:
                        with os.scandir(root) as entries_iter:
                            children = sorted(
                                item.name for item in entries_iter
                                if item.is_dir() and not item.name.startswith('.')
                                and item.name != '.cache')
                    except OSError:
                        pass
                entries[rel] = {
                    'path': rel, 'name': rel[len(self._paths.marker):],
                    'depth': 0, 'parent': None,
                    'direct_count': 0, 'direct_units': 0,
                    'direct_cover': '', 'direct_mtime': 0.0,
                    'pixiv': pixiv_flags[rel], 'has_children': bool(children),
                    'children': children, 'mtime': mtime, 'virtual': True,
                }
                synthetic_children.append(rel)   # 完整虚拟路径（含 `__` 前缀）
                continue
            if rel == '':
                continue      # 合成根已在上面建好
            # 缓存命中条件除目录 mtime 外还要看 Pixiv 排序标志：封面挑选规则由它决定
            # （作品号最大 vs 自然序第一张），切换排序后旧封面必须重扫
            cached = cache_dirs.get(rel)
            if cached and cached.get('mtime') is not None \
                    and abs(float(cached.get('mtime', 0)) - float(mtime)) < 0.5 \
                    and cached.get('pixiv') == pixiv_flags[rel] \
                    and not cached.get('virtual'):
                entries[rel] = cached
                continue
            dir_path, _ = self._paths.resolve_dir(rel)
            if dir_path is None:
                continue
            entries[rel] = self._entries.scan(rel, dir_path, mtime)
            changed += 1
        loop_ms = (time.perf_counter() - loop_started) * 1000
        if synthetic_children and entries.get(''):
            entries['']['children'] = sorted({*entries['']['children'],
                                              *synthetic_children})

        # 自底向上聚合 image_count / cover / mtime。
        order_started = time.perf_counter()
        ordered = sorted(entries.values(),
                         key=lambda entry: (entry['depth'], 1 if entry.get('virtual') else 0),
                         reverse=True)
        order_ms = (time.perf_counter() - order_started) * 1000
        agg_started = time.perf_counter()
        # 漫画根下的一级子目录是「一本书」：它整本算一个更新单元，章（它的子目录）不再
        # 各自成单元。书的范围只在这里判定一次，条目里的 `direct_units` 不认识漫画根。
        book_paths = self._books.book_flags(list(dirs)) if self._books is not None else set()
        totals: Dict[str, Tuple[int, str, float, tuple, int]] = {}
        child_visits = 0
        for entry in ordered:
            rel = entry['path']
            pixiv = pixiv_flags[rel]
            total = entry['direct_count']
            units = entry['direct_units']
            cover = entry['direct_cover']
            newest = entry['direct_mtime']
            rank = self._naming.cover_rank(cover, newest, pixiv)
            for child_name in entry['children']:
                child_visits += 1
                child_rel = f'{rel}/{child_name}' if rel else child_name
                child_totals = totals.get(child_rel)
                if not child_totals:
                    continue
                total += child_totals[0]
                units += child_totals[4]
                if child_totals[2] > newest:
                    newest = child_totals[2]
                # 封面：Pixiv 树取作品号最大的子目录（画师最近的作品），非 Pixiv 目录
                # 仍是 mtime 最新的那个；mtime 聚合不受影响
                if child_totals[3] > rank:
                    rank = child_totals[3]
                    cover = child_totals[1]
            if rel in book_paths:
                # 整本一张卡：吞掉章与更深层目录各自算出的单元数，空书不占单元
                units = 1 if total else 0
            totals[rel] = (total, cover, newest, rank, units)
        agg_ms = (time.perf_counter() - agg_started) * 1000
        self._breakdown = {
            'flags': round(flags_ms, 1),
            'loop': round(loop_ms, 1),
            'order': round(order_ms, 1),
            'agg': round(agg_ms, 1),
            'entries': len(entries),
            'child_visits': child_visits,
        }

        albums: List[Dict] = []
        for entry in entries.values():
            total, cover, newest, _rank, units = totals[entry['path']]
            # 把全树聚合的结果写回缓存条目：`children_total`（列表时的按需聚合）与这里
            # 读的是同一组字段，两条路径互为缓存 —— 列一次目录便不必再走整棵子树，
            # 反过来相册索引重建也不重扫已按需算过的目录。
            entry['total_mtime'] = entry.get('mtime')
            entry['total_count'] = total
            entry['total_units'] = units
            entry['total_cover'] = cover
            albums.append({
                'name': entry['name'],
                'path': entry['path'],
                'parent': entry['parent'],
                'image_count': total,
                'direct_count': entry['direct_count'],
                # 递归更新单元数（按作品分组后的卡数）：时间线「按图片」的分页与总数
                # 用它，与 image_count 一起由同一次聚合算出
                'unit_count': units,
                # 这本书由漫画阅读插件的漫画根界定：时间线「按图片」把它整本列成一张卡，
                # 书的章不再单独成单元
                'book': entry['path'] in book_paths,
                'has_children': entry['has_children'],
                'cover': cover,
                'mtime': newest,
                'depth': entry['depth'],
                'use_time_name': pixiv_flags[entry['path']],
                # 额外根目录的顶层节点：前端把它当作 Pixiv 树的配置点，
                # 与第一根（path == ''）地位一致
                'root_scope': bool(entry.get('virtual')),
                # 递归可读图片数（= image_count）：0 表示整棵下级都没有可读图片，
                # 前端据此隐藏空目录
                'readable': total,
            })

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
