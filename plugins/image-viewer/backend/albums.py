"""相册树、封面挑选与相册缓存（ImageViewerPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 ImageViewerPlugin.__init__ 持有 ——
分片只把方法挂到同一个类上，因此方法与调用点都没有变。
"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, List

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'image_viewer')
NAMESPACE_MARKER = _common.NAMESPACE_MARKER
ALLOWED_EXTENSIONS = _common.ALLOWED_EXTENSIONS
_pick_cover = _common.pick_cover
_cover_rank = _common.cover_rank
_same_path = _common.same_path

class AlbumMixin:
    """相册树、封面挑选与相册缓存。"""

    def _load_album_cache(self) -> dict:
        if self.album_cache_file.exists():
            try:
                with open(self.album_cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # 版本 4：Pixiv 树封面改为作品号最大的那张（画师最近的作品，
                # 同作品内仍取 p0）；版本 3 缓存里 Pixiv 封面是自然序第一张
                # （= 最老的作品），必须作废重扫。
                # 版本 3：封面改为文件名自然序第一张（p0），比 mtime 封面更稳定。
                if isinstance(data, dict) and data.get('version') == self._ALBUM_CACHE_VERSION:
                    return data
            except Exception:
                pass
        return {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}

    def _save_album_cache(self):
        # 缓存目录已不在（进程退出 / 换根 / 测试清理）时别再写：否则会把刚删掉的
        # 目录重新创建出来（表现为 rmtree 报 WinError 145）
        if not self.cache_dir.is_dir():
            return
        try:
            self.album_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.album_cache_file, 'w', encoding='utf-8') as f:
                json.dump(self._album_cache, f, ensure_ascii=False)
        except Exception as e:
            log.error(f'[ImageViewer] 保存相册索引失败: {e}')

    def _load_album_config(self) -> dict:
        defaults = {'collapsed': [], 'promoted': [], 'expanded': [],
                    'visible_empty_dirs': []}
        if self.album_config_file.exists():
            try:
                with open(self.album_config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {**defaults, **data}
            except Exception:
                pass
        return defaults

    def _save_album_config(self):
        try:
            self.album_config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.album_config_file, 'w', encoding='utf-8') as f:
                json.dump(self._album_config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f'[ImageViewer] 保存相册配置失败: {e}')

    def _list_album_dirs(self) -> dict:
        """遍历全部根目录树，返回 {虚拟路径: dir_mtime}。

        第一根的键保持相对路径（与既有相册索引缓存、收藏路径一致），第二根起
        加 `__<命名空间>` 前缀；空字符串键是整棵相册树的合成根节点。

        **结果带缓存**：机械盘上 9167 个目录的 os.walk 要 6-15s（与壳的被动同步抢盘
        时更久），而它只是枚举"有哪些目录"。命中条件是 `_dirs_snapshot(dir)` 那三条
        （根集合一致 + 根 mtime 未变 + TTL 内），任一不满足就重走。

        目录的增删由统一刷新基建的被动同步负责发现（它一发现就
        `_invalidate_albums_cache()` 作废本缓存），因此常规浏览不会 30 秒重走一次全树。
        """
        now = time.time()
        cached = self._dirs_snapshot(now)
        if cached is not None:
            return cached
        # 拿锁**走**（而不是"等飞着的那趟再复用它的结果"）：本函数被调用就说明快照
        # 不可用（刚被作废 / 换过根 / 已过期），复用只会把刚作废的旧结果填回去。
        # 持锁还有个好处：机械盘上避免两趟并发走盘互相抢磁头（实测 6s 被拖到 13s），
        # 后来的调用者在这趟结束后直接命中它写下的快照。
        with self._dirs_lock:
            cached = self._dirs_snapshot(time.time())
            if cached is not None:
                return cached
            dirs = self._walk_album_dirs()
            self._record_dirs(dirs)
            return dirs

    def _dirs_snapshot(self, now: float):
        """可复用的全树目录快照；不可复用返回 None。

        三条同时满足才算可复用（缺任何一条都会给出**错**的目录集合）：

        - **根集合一致**（含路径本身）：索引/快照可能是别的图库或别的根集合留下的，
          只看 mtime 会把"换了个库"当成"没变"；
        - **各根 mtime 未变**：根内增删顶层条目会改根 mtime；
        - 距上次走盘不超过 `_DIRS_TTL`。
        """
        if self._dirs_cache is None or self._dirs_roots is None:
            return None
        if self._dirs_paths != tuple(str(p) for p in self._roots()):
            return None
        if self._dirs_roots != self._roots_fingerprint():
            return None
        if (now - self._dirs_cached_at) > self._DIRS_TTL:
            return None
        return self._dirs_cache

    def _save_dirs_index(self) -> None:
        """把全树目录快照落盘（原子写）。

        为什么值得单独存一份：`albums_index.json` 的条目**不带全树目录列表**（它按
        目录名索引，无法回答"有没有新增目录"），所以每次进程重启都要重走一遍
        os.walk（机械盘 9167 个目录实测 6-23s）才能确认目录集合。把这份快照落盘后，
        重启时直接读文件即可 —— "打开相册页"不再需要等待任何走盘。
        """
        # 缓存目录已经不在了就别再写：进程正在退出 / 数据根被换掉 / 测试清理临时目录
        # 时，这一笔会把刚删掉的目录重新创建出来（表现为 rmtree 报 WinError 145）。
        if not self.cache_dir.is_dir():
            return
        try:
            payload = {
                'version': self._ALBUM_CACHE_VERSION,
                'roots': self._dirs_paths or [],
                'dirs': self._dirs_cache or {},
            }
            tmp = self.dirs_cache_file.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.dirs_cache_file)
        except Exception as e:
            log.error(f'[ImageViewer] 保存目录索引失败: {e}')

    def _record_dirs(self, dirs: dict) -> None:
        """把一趟走盘的结果记为当前快照并落盘（`_list_album_dirs` 与后台刷新共用）。"""
        self._dirs_cache = dirs
        self._dirs_roots = self._roots_fingerprint()
        self._dirs_paths = tuple(str(p) for p in self._roots())
        self._dirs_cached_at = time.time()
        self._save_dirs_index()

    def _load_dirs_index(self):
        """读取落盘的全树目录快照；根集合不一致（换库）或版本不符时返回 None。"""
        if not self.dirs_cache_file.exists():
            return None
        try:
            with open(self.dirs_cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return None
        if not isinstance(data, dict) or data.get('version') != self._ALBUM_CACHE_VERSION:
            return None
        dirs = data.get('dirs')
        if not isinstance(dirs, dict) or not dirs:
            return None
        if tuple(data.get('roots') or ()) != tuple(str(p) for p in self._roots()):
            return None
        return {str(k): float(v or 0.0) for k, v in dirs.items()}

    def _roots_fingerprint(self) -> tuple:
        """根集合指纹：(虚拟前缀, mtime)。前缀变化 = 换了根/命名空间 token 变了。"""
        marks = []
        for root in self._roots():
            try:
                marks.append((self._virtual_path(root, ''), root.stat().st_mtime))
            except OSError:
                marks.append((self._virtual_path(root, ''), None))
        return tuple(marks)

    def prewarm_album_dirs(self):
        """后台先把全树目录枚举跑好（可选，`_resolved_config['dirs_prewarm'] = True`）。

        常规情况不需要它：目录快照已经落盘（`dirs_index.json`），启动时读回来就能
        直接出结果（实测 0.08s）。它只对"还没有快照"的场景有用 —— 例如换了数据根，
        第一趟怎么都要走，不如挪到后台。语义上它只补缓存，不影响正确性。
        """
        if self._dirs_prewarm is not None and self._dirs_prewarm.is_alive():
            return
        self._dirs_prewarm = threading.Thread(
            target=self._prewarm_worker, name='image-viewer-dirs-prewarm', daemon=True)
        self._dirs_prewarm.start()

    def _prewarm_worker(self):
        try:
            dirs = self._walk_album_dirs()
        except Exception as e:                       # 只是"提前算"，失败不影响正事
            log.warning(f'[ImageViewer] 相册目录预热失败: {e}')
            return
        with self._dirs_lock:
            self._record_dirs(dirs)

    def _walk_album_dirs(self) -> dict:
        """真正走盘的全树目录枚举（`_list_album_dirs` 的缓存未命中分支）。"""
        started = time.perf_counter()
        try:
            return self._walk_album_dirs_inner()
        finally:
            self._dirs_walk_count += 1
            self._dirs_walk_ms += (time.perf_counter() - started) * 1000
            log.info(f'[ImageViewer] 全树目录枚举 #{self._dirs_walk_count} 完成，'
                     f'累计 {self._dirs_walk_ms:.0f}ms')

    def _walk_album_dirs_inner(self) -> dict:
        dirs = {}
        newest = 0.0
        for root in self._roots():
            try:
                newest = max(newest, root.stat().st_mtime)
            except OSError:
                continue
            # 额外根目录的顶层是虚拟命名空间节点，本身不在磁盘上，单独补一条：
            # `_build_albums` 靠它合成「子目录网格」用的容器条目
            if not _same_path(root, self.root_dir):
                dirs[self._virtual_path(root, '')] = root.stat().st_mtime
            for current, dir_names, _files in os.walk(root):
                dir_names[:] = [d for d in dir_names
                                if not d.startswith('.') and d != '.cache']
                current = Path(current)
                if current == root:
                    continue
                try:
                    rel = self._virtual_path(root, current.relative_to(root).as_posix())
                    dirs[rel] = current.stat().st_mtime
                except (OSError, ValueError):
                    continue
        dirs[''] = newest
        return dirs

    def _scan_dir_direct(self, dir_path: Path, rel_path: str) -> dict:
        """只扫描一个目录的直接图片（一次 os.scandir，开销可控）。

        目录名语义（name/depth/parent）按**根内**相对路径计算，所以额外根目录
        下的作者/作品与第一根处在同一层级，两层 Pixiv 布局不会被命名空间顶掉；
        文件 `rel` 仍是虚拟路径（`__命名空间/...`），可直接当作缩略图/图片 URL。
        """
        images = []
        children = []
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    if entry.name.startswith('.') or entry.name == '.cache':
                        continue
                    if entry.is_dir():
                        children.append(entry.name)
                    elif entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                        try:
                            stat = entry.stat()
                        except OSError:
                            continue
                        images.append({
                            'rel': (Path(rel_path) / entry.name).as_posix() if rel_path else entry.name,
                            'mtime': stat.st_mtime,
                        })
        except OSError:
            pass
        # 封面：Pixiv 树取作品号最大的一张（p0），其余取文件名自然序第一张；
        # 相册新旧仍按最新 mtime 计算
        newest = max((img['mtime'] for img in images), default=0.0)
        pixiv = self._pixiv_mode(rel_path)
        cover = _pick_cover(images, pixiv)
        root, inner = self._split_virtual(rel_path)
        parent = self._virtual_path(root, '/'.join(inner.split('/')[:-1])) if inner else None
        return {
            'path': rel_path,
            'name': dir_path.name,
            'depth': inner.count('/') + (1 if inner else 0),
            'parent': parent,
            'direct_count': len(images),
            'direct_cover': cover,
            'direct_mtime': newest,
            'pixiv': pixiv,      # 封面挑选依据，缓存复用时要核对（见 _build_albums）
            'has_children': len(children) > 0,
            'children': sorted(children),
        }

    def _build_albums(self, dirs: dict, cache_dirs: dict) -> tuple:
        """直接扫描变化目录，再自底向上聚合出递归统计。

        `dirs` 是全部根目录的 {虚拟路径: mtime}（见 `_list_album_dirs`）。额外根
        目录的顶层节点是**虚拟**的（`__<命名空间>`），磁盘上不存在，这里为它
        合成一个只含 children 的条目，使聚合循环与第一根走同一条路径。
        """
        cache_dirs = cache_dirs or {}
        entries = {}
        changed = 0
        t_flags = time.perf_counter()
        # 一次算完全树（见 `compute_pixiv_flags`）：逐个问 `_pixiv_mode()` 会让每个
        # 目录各走一遍设置继承链，9168 个目录实测 2.7-3.4s
        pixiv_flags = self.compute_pixiv_flags(list(dirs))
        t_flags = time.perf_counter() - t_flags
        # 先建合成根条目：它的 children 要包含命名空间节点，聚合循环才把
        # 额外根目录的图片算进相册树总数（albums[''].image_count）
        synthetic_children: List[str] = []
        for rel, mtime in dirs.items():
            if self._is_namespace_node(rel) or rel != '':
                continue
            root_entry = cache_dirs.get('') if isinstance(cache_dirs.get(''), dict) else None
            if root_entry and root_entry.get('mtime') is not None \
                    and abs(float(root_entry.get('mtime', 0)) - float(mtime)) < 0.5 \
                    and not root_entry.get('virtual'):
                entries[''] = root_entry
            else:
                # 走带缓存的扫描：与列表路径共用同一份条目，避免"相册索引重建"
                # 与"列一次目录"各扫一遍同一个目录
                entry = self._scan_dir_cached('', self.root_dir, mtime)
                entries[''] = entry
                changed += 1
            break
        for rel, mtime in dirs.items():
            if self._is_namespace_node(rel):
                # 虚拟命名空间节点：没有直接图片，children = 该根的一级子目录
                root, _ = self._split_virtual(rel)
                children = []
                if root is not None:
                    try:
                        with os.scandir(root) as it:
                            children = sorted(
                                e.name for e in it
                                if e.is_dir() and not e.name.startswith('.')
                                and e.name != '.cache')
                    except OSError:
                        pass
                entries[rel] = {
                    'path': rel, 'name': rel[len(NAMESPACE_MARKER):],
                    'depth': 0, 'parent': None,
                    'direct_count': 0, 'direct_cover': '', 'direct_mtime': 0.0,
                    'pixiv': pixiv_flags[rel], 'has_children': bool(children),
                    'children': children, 'mtime': mtime, 'virtual': True,
                }
                synthetic_children.append(rel)   # 完整虚拟路径（含 `__` 前缀）
                continue
            if rel == '':
                continue      # 合成根已在上面建好
            # 缓存命中条件除目录 mtime 外还要看 Pixiv 排序标志：封面挑选规则由它
            # 决定（作品号最大 vs 自然序第一张），切换排序后旧封面必须重扫
            cached = cache_dirs.get(rel)
            if cached and cached.get('mtime') is not None \
                    and abs(float(cached.get('mtime', 0)) - float(mtime)) < 0.5 \
                    and cached.get('pixiv') == pixiv_flags[rel] \
                    and not cached.get('virtual'):
                entries[rel] = cached
                continue
            dir_path, _ = self._resolve_dir(rel)
            if dir_path is None:
                continue
            entries[rel] = self._scan_dir_cached(rel, dir_path, mtime)
            changed += 1
        if synthetic_children and entries.get(''):
            entries['']['children'] = sorted({*entries['']['children'],
                                             *synthetic_children})

        # 自底向上聚合 image_count / cover / mtime。
        # 同深度时命名空间节点（virtual）先算：它与合成根同为 depth 0，而合成根
        # 要把额外根目录的图片并进相册树总数，必须等它们的结果先出来。
        t_loop = time.perf_counter() - t_flags
        t_order = time.perf_counter()
        ordered = sorted(entries.values(),
                         key=lambda e: (e['depth'], 1 if e.get('virtual') else 0),
                         reverse=True)
        t_order = time.perf_counter() - t_order
        t_agg = time.perf_counter()
        totals = {}
        child_visits = 0
        for entry in ordered:
            rel = entry['path']
            pixiv = pixiv_flags[rel]
            total = entry['direct_count']
            cover = entry['direct_cover']
            newest = entry['direct_mtime']
            rank = _cover_rank(cover, newest, pixiv)
            for child_name in entry['children']:
                child_visits += 1
                child_rel = f"{rel}/{child_name}" if rel else child_name
                child_totals = totals.get(child_rel)
                if not child_totals:
                    continue
                total += child_totals[0]
                if child_totals[2] > newest:
                    newest = child_totals[2]
                # 封面：Pixiv 树取作品号最大的子目录（画师最近的作品），
                # 非 Pixiv 目录仍是 mtime 最新的那个；mtime 聚合不受影响
                if child_totals[3] > rank:
                    rank = child_totals[3]
                    cover = child_totals[1]
            totals[rel] = (total, cover, newest, rank)
        t_agg = time.perf_counter() - t_agg
        self._build_breakdown = {
            'flags': round(t_flags * 1000, 1),
            'loop': round(t_loop * 1000, 1),
            'order': round(t_order * 1000, 1),
            'agg': round(t_agg * 1000, 1),
            'entries': len(entries),
            'child_visits': child_visits,
        }

        albums = []
        for entry in entries.values():
            total, cover, newest, _rank = totals[entry['path']]
            # 把全树聚合的结果写回缓存条目：`_children_total`（列表时的按需聚合）
            # 与这里读的是同一组字段，两条路径互为缓存 —— 列一次目录便不必再走
            # 整棵子树，反过来相册索引重建也不重扫已按需算过的目录。
            entry['total_mtime'] = entry.get('mtime')
            entry['total_count'] = total
            entry['total_cover'] = cover
            albums.append({
                'name': entry['name'],
                'path': entry['path'],
                'parent': entry['parent'],
                'image_count': total,
                'direct_count': entry['direct_count'],
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

        # 不再预生成所有封面缩略图：交给 /thumbs 按需生成，避免上万次随机小文件 I/O。
        return albums, entries, changed

    def list_albums(self) -> Dict:
        """相册列表：**有目录快照就用它，没有才走盘**。

        走盘（全树 os.walk）在机械盘图库上实测 6-23s，而它换来的只是"有哪些目录 +
        各自 mtime"。所以策略是：

        - 进程内有结果 → 直接返回（30s TTL）；
        - 有全树目录快照（本进程走过盘，或启动时从 `dirs_index.json` 读回来的）→
          用它重建，**纯内存**，不碰磁盘；
        - 没有快照（首次运行 / 刚被作废 / 换过根）→ 当场走一趟再重建。这是唯一会
          等走盘的路径，而它只在"确实有变化"（被动同步作废、校验、删除移动）时出现。

        实测：正常启动进相册页 0.08-0.15s（读落盘快照）；只有"索引刚被作废"那一帧
        要等走盘。
        """
        now = time.time()
        self._albums_calls += 1
        started = time.perf_counter()
        stages: Dict[str, float] = {}
        self._albums_stage_ms = stages
        try:
            def mark(name: str, t0: float) -> float:
                t = time.perf_counter()
                stages[name] = round((t - t0) * 1000, 1)
                return t

            t = time.perf_counter()
            if self._albums_cached is not None \
                    and (now - self._albums_cached_at) < self._ALBUMS_TTL:
                return {**self._albums_cached, 'cached': True}
            t = mark('ttl_check', t)

            if self._prune_visible_marks():
                # 可见性标记被清理过 → 索引条目不可信，整份丢掉重扫
                self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}
            t = mark('prune_marks', t)

            dirs = self._dirs_snapshot(now)
            t = mark('dirs_snapshot', t)
            if dirs is None:
                # 没有可用快照：当场走一趟（唯一会等走盘的路径）
                dirs = self._list_album_dirs()
            t = mark('dirs_resolve', t)

            result = self._finish_albums(dirs, self._album_cache.get('dirs', {}), now)
            t = mark('finish_albums', t)
            stages['build_scan_count'] = float(self._last_build_scans)
            stages['build_changed'] = float(self._last_build_changed)
            return result
        finally:
            self._albums_last_ms = (time.perf_counter() - started) * 1000

    def _finish_albums(self, dirs: dict, cache_dirs: dict, now: float) -> Dict:
        """跑 `_build_albums` 并落缓存（`list_albums` 的两条分支共用）。

        取 `cache_dirs` 与回写之间不放锁：被动同步在这段里作废索引时，本帧可能落到
        一份陈旧结果 —— 但它同时把 `_albums_cached` 清成 None，所以下一帧就会用新
        条目重建，不会一直陈旧下去。加锁反而会让同步线程阻塞在一次目录枚举上。
        """
        self._last_build_scans = 0
        t_build = time.perf_counter()
        albums, entries, changed = self._build_albums(dirs, cache_dirs)
        t_build = time.perf_counter() - t_build
        self._last_build_scans = self._scan_miss_count
        self._last_build_changed = changed
        t_assign = time.perf_counter()
        self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': entries}
        t_assign = time.perf_counter() - t_assign
        t_save = time.perf_counter()
        if changed:
            self._save_album_cache()
        t_save = time.perf_counter() - t_save
        result = {'albums': albums, 'config': self._album_config, 'changed': changed}
        self._albums_cached_at = now
        self._albums_cached = result
        self._albums_stage_ms.update({
            'build': round(t_build * 1000, 1),
            'assign': round(t_assign * 1000, 1),
            'save_cache': round(t_save * 1000, 1),
        })
        return result

    def _invalidate_albums_cache(self):
        """让 list_albums 下一次调用强制重扫（刷新/重建/相册配置变更时调用）。

        同时作废全树目录枚举缓存：调用点都是"已知有变化"（被动同步发现目录变动、
        校验、删除/移动、改根目录），此时重走一次 os.walk 正是需要的。
        落盘的那份目录快照一并删掉，否则下次启动又会读到它。
        """
        self._albums_cached_at = 0.0
        self._albums_cached = None
        self._dirs_cache = None
        self._dirs_roots = None
        self._dirs_paths = None
        self._dirs_cached_at = 0.0
        with self._dirs_lock:
            try:
                if self.dirs_cache_file.exists():
                    self.dirs_cache_file.unlink()
            except OSError as e:
                log.warning(f'[ImageViewer] 删除目录索引失败: {e}')

    def get_album_config(self) -> Dict:
        return self._album_config

    def set_album_config(self, rel_path: str, action: str) -> Dict:
        """album 层级控制：collapse/expand（收纳/展开子相册）、promote/unpromote（提升到全部相册）。

        子相册**默认折叠**（见前端 `_isCollapsed`），所以「展开」要落进 `expanded`
        白名单；「收纳」则撤销展开并记进 `collapsed`，覆盖历史配置。
        """
        rel_path = (rel_path or '').strip().strip('/')
        collapsed = set(self._album_config.get('collapsed', []))
        promoted = set(self._album_config.get('promoted', []))
        expanded = set(self._album_config.get('expanded', []))
        if action == 'collapse':
            collapsed.add(rel_path)
            expanded.discard(rel_path)
        elif action == 'expand':
            collapsed.discard(rel_path)
            expanded.add(rel_path)
        elif action == 'promote':
            promoted.add(rel_path)
        elif action == 'unpromote':
            promoted.discard(rel_path)
        else:
            return {'success': False, 'error': f'未知操作: {action}'}
        self._album_config = {
            **self._album_config,
            'collapsed': sorted(collapsed),
            'promoted': sorted(promoted),
            'expanded': sorted(expanded),
        }
        self._save_album_config()
        self._invalidate_albums_cache()
        return {'success': True, 'config': self._album_config}
