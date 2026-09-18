"""按需取字节与远端缓存（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
_opts = _common._opts
DEFAULT_MAX_FETCH_BYTES = _common.DEFAULT_MAX_FETCH_BYTES

class FetchMixin:
    """`/file` 的占位判定与 ensure_file 钩子、取回并发去重，以及缓存用量与清理。"""

    def is_content_placeholder(self, path: Any) -> bool:
        """该路径是否"已物化但字节还没取回"（`/file` 每次请求都会问）。

        判定依据是"实际大小 != 索引里的远端大小"：物化时写下的是 0 字节占位，
        而索引里记的是远端真实大小。必须廉价 —— 一次 `stat` 加路径归属判断，
        索引走进程内缓存。

        **刻意不在这里比对 mtime**：拿本地文件的 mtime 去比索引里对端的 mtime，
        会因为文件系统的量化（`os.utime` 写进去的 ns 未必原样存下）而永远不相等，
        于是每个请求都触发一次取回。"对端换过内容"的发现点只有一个 ——
        `materialize_remote()` 的对账，那里比较的两个值**都来自对端**。
        """
        try:
            target = Path(path)
            rel = target.resolve().relative_to(self.remote_cache_dir.resolve())
        except (OSError, ValueError):
            return False
        parts = rel.parts
        if len(parts) < 3:
            return False
        index = self._cached_remote_index(parts[0], parts[1])
        meta = (index.get('entries') or {}).get('/'.join(parts[2:]))
        if not isinstance(meta, dict) or meta.get('dir'):
            return False
        remote_size = int(meta.get('size') or 0)
        if remote_size <= 0:
            return False   # 远端本来就是空文件：不必取
        try:
            return target.stat().st_size != remote_size
        except OSError:
            return True

    def ensure_file(self, path: Any) -> None:
        """`/file` 找不到内容时由 Shell 回调：把远端对应的那个文件取回本地。

        判定"这是不是一个还没取回的占位文件"：拿实际大小与索引里的真实大小比。
        占位文件是 0 字节，而索引里的真实大小来自远端 —— 两者不一致（或文件不存在）
        就取一次。

        **同大小的替换不在本方法里发现**：那需要"对端现在报的值"与"索引里的值"对比，
        而本方法只看本机磁盘。它由 `materialize_remote()` 的对账负责（mtime 变了就丢掉
        本地真字节，于是下次读到这里时本方法自然会重新取回）。
        """
        try:
            target = Path(path)
            rel = target.resolve().relative_to(self.remote_cache_dir.resolve())
        except (OSError, ValueError):
            return   # 不是远端缓存里的东西：本钩子不管
        parts = rel.parts
        if len(parts) < 3:
            return   # 期望 <设备ID>/<共享标识>/<相对路径…>
        device_id, share_id = parts[0], parts[1]
        relative = '/'.join(parts[2:])
        index = self._cached_remote_index(device_id, share_id)
        meta = (index.get('entries') or {}).get(relative)
        if not isinstance(meta, dict) or meta.get('dir'):
            return
        remote_size = int(meta.get('size') or 0)
        try:
            if target.is_file() and target.stat().st_size == remote_size:
                return   # 已经取回过了（大小一致）
        except OSError:
            pass
        if remote_size > self.max_fetch_bytes:
            log.info(f'[group-mesh] 跳过按需取回：{relative} 大小 {remote_size} 超过上限 '
                     f'{self.max_fetch_bytes}')
            return

        # 并发去重。Flask 是 threaded=True，浏览器一次会并发多个请求；内核的
        # `fetch_to_file` 用的是**按目标路径推导**的 `.part` 临时名，两个线程为同一个
        # 路径同时取字节就会撞在同一个临时文件上（交错写入 → 内容损坏）。
        # 锁内复核一次"是不是已经被别人取回了"，重复传输也一并省掉。
        with self._fetch_lock(device_id, share_id, relative):
            try:
                if target.is_file() and target.stat().st_size == remote_size:
                    return
            except OSError:
                pass
            staging = self.staging_dir / device_id / share_id / relative
            result = self._fetch_to(staging, device_id, share_id, relative)
            if not result.get('success'):
                self._discard_staging(staging)
                log.info(f'[group-mesh] 按需取回失败 {relative}: {result.get("error")}')
                return
            # 暂存文件与物化目标同在 `.cache` 下（同卷由构造保证），因此 os.replace 是
            # 原子的；不再经过用户下载目录（改动前那里会被浏览行为污染）。
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, target)
                self._apply_remote_mtime(target, meta.get('mtime_ns'))
                self._invalidate_remote_index(device_id, share_id)
            except OSError as e:
                self._discard_staging(staging)
                log.warning(f'[group-mesh] 物化落位失败 {relative}: {e}')

    def _fetch_lock(self, device_id: str, share_id: str, relative: str) -> threading.Lock:
        """取 (设备ID, 共享标识, 相对路径) 对应的一把锁（进程内）。

        锁对象不做回收：条目数就是"被并发读过的远端文件数"，与物化缓存同量级；
        回收需要引用计数，漏一处就退化成"锁静默失效"，不值得。
        """
        key = (device_id, share_id, relative)
        with self._fetch_locks_guard:
            lock = self._fetch_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._fetch_locks[key] = lock
            return lock

    @staticmethod
    def _discard_staging(staging: Path) -> None:
        """清掉失败的暂存文件（`fetch_to_file` 失败时会留下 `<目标>.part`）。"""
        for candidate in (staging, staging.with_suffix(staging.suffix + '.part')):
            try:
                candidate.unlink()
            except OSError:
                continue

    @property
    def max_fetch_bytes(self) -> int:
        """按需取回的单文件上限，避免一次浏览把磁盘塞满。"""
        try:
            configured = int(self.setting('max_fetch_mb', 0) or 0)
        except (TypeError, ValueError):
            configured = 0
        return configured * 1024 * 1024 if configured > 0 else DEFAULT_MAX_FETCH_BYTES

    def remote_cache(self) -> Dict[str, Any]:
        """已物化的远端内容一览（界面显示用了多少磁盘、可以清哪些）。"""
        root = self.remote_cache_dir
        items: List[Dict[str, Any]] = []
        if root.is_dir():
            for device_dir in sorted(root.iterdir()):
                if not device_dir.is_dir():
                    continue
                for share_dir in sorted(device_dir.iterdir()):
                    if not share_dir.is_dir():
                        continue
                    index = self._load_remote_index(device_dir.name, share_dir.name)
                    entries = index.get('entries') or {}
                    files = [rel for rel, meta in entries.items() if not meta.get('dir')]
                    fetched = 0
                    for rel in files:
                        candidate = share_dir / rel
                        try:
                            meta_size = int((entries.get(rel) or {}).get('size') or 0)
                            if candidate.is_file() and meta_size and \
                                    candidate.stat().st_size == meta_size:
                                fetched += 1
                        except OSError:
                            continue
                    items.append({
                        'device_id': device_dir.name,
                        'share_id': share_dir.name,
                        'entries': len(entries),
                        'files': len(files),
                        'fetched': fetched,
                        'pending': len(files) - fetched,
                        'bytes': self._tree_usage(share_dir)[0],
                        'materialized_at': index.get('materialized_at'),
                        'truncated': bool(index.get('truncated')),
                        'root': str(share_dir),
                    })
        return {'success': True, 'root': str(root), 'items': items,
                'total_bytes': self._tree_usage(root)[0] if root.is_dir() else 0}

    def clear_remote_cache(self, opts: Any = None, device_id: str = '',
                           share_id: str = '') -> Dict[str, Any]:
        """删除已物化的远端内容（磁盘回收）。不传参数时清空全部。

        暂存目录（`staging`）一起清：它是取字节的中转区，失败的取回会留下
        `<目标>.part`，不属于任何一份物化内容，留着就是无人回收的垃圾。
        """
        import shutil

        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
        share_id = str(options.get('share_id') or share_id or '')
        root = self.remote_cache_dir
        freed = 0
        if root.exists():
            if device_id and share_id:
                targets = [root / device_id / share_id]
            elif device_id:
                targets = [root / device_id]
            else:
                targets = [child for child in root.iterdir()]
            removed: List[str] = []
            for target in targets:
                if not target.exists():
                    continue
                freed += self._tree_usage(target)[0]
                shutil.rmtree(target, ignore_errors=True)
                removed.append(str(target))
        else:
            removed = []
        staging = self.staging_dir
        if staging.exists():
            freed += self._tree_usage(staging)[0]
            shutil.rmtree(staging, ignore_errors=True)
        self._invalidate_remote_index()
        return {'success': True, 'removed': removed, 'freed_bytes': freed}
