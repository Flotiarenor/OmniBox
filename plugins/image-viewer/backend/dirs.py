"""全树目录枚举与它的落盘快照。

为什么值得单独一个组件：相册索引按目录名索引，回答不了"有没有新增目录"，而那份答案
只能靠 `os.walk` 拿 —— 机械盘上 9167 个目录实测 6-23s。所以目录集合被当成一份独立
的、可落盘复用的快照：进程内用缓存，跨进程用 `dirs_index.json`，两者都靠"根集合一致
+ 各根 mtime 未变 + 距那次走盘不超过 TTL"三条判据决定能不能复用。

三条缺一不可（缺任何一条都会给出**错**的目录集合）：

* **根集合一致**（含路径本身）：索引/快照可能是别的图库或别的根集合留下的，只看
  mtime 会把"换了个库"当成"没变"；
* **各根 mtime 未变**：根内增删顶层条目会改根 mtime，落盘时记下、读回时比对；
* 距**那次走盘**不超过 `dirs_ttl`：落盘时记 `walked_at`，重启后从它算起，而不是从
  读盘时刻重新计时。

目录的增删由统一刷新基建的目录集合信号与校验负责发现：它们一发现就作废本快照，因此
常规浏览不会每 30 秒重走一次全树。

状态自持：快照、时间戳、根指纹、锁、诊断计数都在这里。
"""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.json_cache import read_cached_json, write_cached_json

log = logging.getLogger(__name__)


class DirSnapshot:
    """全部根目录的目录集合快照（虚拟路径 → 目录 mtime）。"""

    def __init__(self, paths: Any, limits: Any) -> None:
        self._paths = paths
        self._ttl = limits.dirs_ttl
        self._version = limits.album_cache_version
        self._lock = threading.Lock()
        self._dirs: Optional[Dict[str, float]] = None
        self._cached_at = 0.0
        self._roots_fp: Optional[tuple] = None
        self._root_paths: Optional[Tuple[str, ...]] = None
        self._prewarm_thread: Optional[threading.Thread] = None
        # 诊断计数（`diagnose()`）：全树走盘发生了几次、各花多久
        self._walk_count = 0
        self._walk_ms = 0.0
        self._restore()

    # ===== 落盘快照 =====

    def _load(self) -> Optional[Tuple[Dict[str, float], float]]:
        """读取落盘的全树目录快照，返回 (目录集合, 走盘时刻)；不可复用时返回 None。

        走盘时刻取自**落盘那一刻**，不是读盘时刻：否则每次启动都会把一份可能已经放了
        很久的快照当成刚走完的，TTL 变成"重启后再信 10 分钟"。落盘时还记下各根的虚拟
        前缀与 mtime，根内顶层增删会改根 mtime，读到不一致就整份作废 —— 重启后不必
        等 TTL。
        """
        payload = read_cached_json(
            self._paths.dirs_cache_file, version=self._version,
            fingerprint={'roots': [str(path) for path in self._paths.roots()]})
        if payload is None:
            return None
        dirs = payload.get('dirs')
        if not isinstance(dirs, dict) or not dirs:
            return None
        walked_at = payload.get('walked_at')
        if not isinstance(walked_at, (int, float)) or walked_at <= 0:
            return None
        saved_roots = payload.get('roots_mtime')
        if not isinstance(saved_roots, dict) or saved_roots != self.roots_mtime():
            return None
        return ({str(key): float(value or 0.0) for key, value in dirs.items()},
                float(walked_at))

    def _restore(self) -> None:
        """进程启动时把落盘快照读回来：有了它，"打开相册页"不必等任何走盘。"""
        loaded = self._load()
        if loaded is None:
            return
        dirs, walked_at = loaded
        self._dirs = dirs
        # 复用判据的起点是那次走盘，而不是本次启动
        self._cached_at = walked_at
        self._roots_fp = self.roots_fingerprint()
        self._root_paths = tuple(str(path) for path in self._paths.roots())

    def _save(self) -> None:
        """把全树目录快照落盘（原子写）。

        为什么值得单独存一份：重启时直接读文件即可 —— "打开相册页"不再需要等待任何
        走盘（实测读快照 0.08s，重走 6-23s）。走盘时刻与各根 mtime 一起落盘，读回时
        才能判断这份快照是否仍然可信。
        """
        # 缓存目录已经不在了就别再写：进程正在退出 / 数据根被换掉 / 测试清理临时目录
        # 时，这一笔会把刚删掉的目录重新创建出来（Windows 上 rmtree 会因此报 145）。
        if not self._paths.cache_dir.is_dir():
            return
        if not write_cached_json(
                self._paths.dirs_cache_file,
                {'dirs': self._dirs or {},
                 'walked_at': self._cached_at,
                 'roots_mtime': self.roots_mtime()},
                version=self._version,
                fingerprint={'roots': list(self._root_paths or ())}):
            log.error('[ImageViewer] 保存目录索引失败: %s', self._paths.dirs_cache_file)

    def record(self, dirs: Dict[str, float]) -> None:
        """把一趟走盘的结果记为当前快照并落盘。"""
        self._dirs = dirs
        self._roots_fp = self.roots_fingerprint()
        self._root_paths = tuple(str(path) for path in self._paths.roots())
        self._cached_at = time.time()
        self._save()

    # ===== 复用判据 =====

    def roots_fingerprint(self) -> tuple:
        """根集合指纹：(虚拟前缀, mtime)。前缀变化 = 换了根/命名空间 token 变了。"""
        marks = []
        for root in self._paths.roots():
            try:
                marks.append((self._paths.virtual_path(root, ''), root.stat().st_mtime))
            except OSError:
                marks.append((self._paths.virtual_path(root, ''), None))
        return tuple(marks)

    def roots_mtime(self) -> Dict[str, Optional[float]]:
        """各根的 mtime（键是虚拟前缀），供落盘快照与当前磁盘比对。"""
        return {prefix: (None if mtime is None else float(mtime))
                for prefix, mtime in self.roots_fingerprint()}

    def snapshot(self, now: float) -> Optional[Dict[str, float]]:
        """可复用的全树目录快照；不可复用返回 None（判据见模块 docstring）。"""
        if self._dirs is None or self._roots_fp is None:
            return None
        if self._root_paths != tuple(str(path) for path in self._paths.roots()):
            return None
        if self._roots_fp != self.roots_fingerprint():
            return None
        if (now - self._cached_at) > self._ttl:
            return None
        return self._dirs

    def ensure(self) -> Dict[str, float]:
        """取目录集合：命中快照就用它，否则当场走一趟（唯一会等走盘的路径）。"""
        now = time.time()
        cached = self.snapshot(now)
        if cached is not None:
            return cached
        # 拿锁**走**（而不是"等飞着的那趟再复用它的结果"）：本函数被调用就说明快照
        # 不可用（刚被作废 / 换过根 / 已过期），复用只会把刚作废的旧结果填回去。
        # 持锁还有个好处：机械盘上避免两趟并发走盘互相抢磁头（实测 6s 被拖到 13s），
        # 后来的调用者在这趟结束后直接命中它写下的快照。
        with self._lock:
            cached = self.snapshot(time.time())
            if cached is not None:
                return cached
            dirs = self.walk()
            self.record(dirs)
            return dirs

    # ===== 走盘 =====

    def walk(self) -> Dict[str, float]:
        """真正走盘的全树目录枚举（缓存未命中分支），顺带记诊断计数。"""
        started = time.perf_counter()
        try:
            return self._walk()
        finally:
            self._walk_count += 1
            self._walk_ms += (time.perf_counter() - started) * 1000
            log.info('[ImageViewer] 全树目录枚举 #%s 完成，累计 %.0fms',
                     self._walk_count, self._walk_ms)

    def _walk(self) -> Dict[str, float]:
        """遍历全部根目录树，返回 {虚拟路径: 目录 mtime}。

        第一根的键保持相对路径（与既有相册索引缓存、收藏路径一致），第二根起加
        `__<命名空间>` 前缀；空字符串键是整棵相册树的合成根节点。
        """
        dirs: Dict[str, float] = {}
        newest = 0.0
        for root in self._paths.roots():
            try:
                newest = max(newest, root.stat().st_mtime)
            except OSError:
                continue
            # 额外根目录的顶层是虚拟命名空间节点，本身不在磁盘上，单独补一条：
            # 相册索引靠它合成"子目录网格"用的容器条目
            if not self._paths.is_primary(root):
                dirs[self._paths.virtual_path(root, '')] = root.stat().st_mtime
            for current, dir_names, _files in os.walk(root):
                dir_names[:] = [name for name in dir_names
                                if not name.startswith('.') and name != '.cache']
                current_path = Path(current)
                if current_path == root:
                    continue
                try:
                    rel = self._paths.virtual_path(
                        root, current_path.relative_to(root).as_posix())
                    dirs[rel] = current_path.stat().st_mtime
                except (OSError, ValueError):
                    continue
        dirs[''] = newest
        return dirs

    # ===== 预热 =====

    def prewarm(self) -> None:
        """后台先把全树目录枚举跑好（可选，`_resolved_config['dirs_prewarm'] = True`）。

        常规情况不需要它：目录快照已经落盘，启动时读回来就能直接出结果。它只对"还没有
        快照"的场景有用 —— 例如换了数据根，第一趟怎么都要走，不如挪到后台。语义上它只
        补缓存，不影响正确性。
        """
        if self._prewarm_thread is not None and self._prewarm_thread.is_alive():
            return
        self._prewarm_thread = threading.Thread(
            target=self._prewarm_worker, name='image-viewer-dirs-prewarm', daemon=True)
        self._prewarm_thread.start()

    def _prewarm_worker(self) -> None:
        try:
            dirs = self.walk()
        except Exception as exc:                       # 只是"提前算"，失败不影响正事
            log.warning('[ImageViewer] 相册目录预热失败: %s', exc)
            return
        with self._lock:
            self.record(dirs)

    # ===== 生命周期与诊断 =====

    def reset(self) -> None:
        """丢弃快照并删掉落盘的那份（换根、目录变动、校验、重建时调用）。"""
        self._dirs = None
        self._roots_fp = None
        self._root_paths = None
        self._cached_at = 0.0
        with self._lock:
            try:
                if self._paths.dirs_cache_file.exists():
                    self._paths.dirs_cache_file.unlink()
            except OSError as exc:
                log.warning('[ImageViewer] 删除目录索引失败: %s', exc)

    @property
    def entries(self) -> int:
        """诊断用：当前快照的目录条目数。"""
        return len(self._dirs or {})

    @property
    def paths(self) -> List[str]:
        """诊断用：快照对应的根路径。"""
        return list(self._root_paths or ())

    def diagnostics(self) -> Dict[str, Any]:
        """诊断用：走盘次数与耗时、快照可用性。"""
        return {
            'dirs_cache_file': str(self._paths.dirs_cache_file),
            'dirs_cache_file_exists': self._paths.dirs_cache_file.exists(),
            'dirs_cache_entries': self.entries,
            'dirs_snapshot_usable': self.snapshot(time.time()) is not None,
            'dirs_walked_at': round(self._cached_at, 3),
            'roots_fingerprint': repr(self.roots_fingerprint()),
            'dirs_paths': self.paths,
            'album_dirs_walk_count': self._walk_count,
            'album_dirs_walk_ms': round(self._walk_ms, 1),
        }
