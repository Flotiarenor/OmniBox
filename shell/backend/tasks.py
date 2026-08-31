'''
Copyright 2026 flotiarenor

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

"""通用后台任务（媒体插件共享基建·控制面）。

把「后台线程 + 进度上报 + 取消 + 状态查询 + 可选断点持久化」从各插件
（image-viewer 重建、pixiv-sync 下载等）抽成统一骨架。

- 任务壳只负责线程/状态/取消/进度，**不介入业务编排**：
  worker 函数自行决定「网络验证 → 本地比对 → 执行」等流程；
- 业务专属计数（downloaded/skipped 等）放 `extra` 扩展字典；
- 持久化可选：传入 `persist_path` 后每次 `persist()` 原子落盘，
  重启后 `BackgroundTask.load()` 恢复为 paused 可续跑（吸收 pixiv-sync
  tasks.py 的断点恢复经验）；不传则纯内存（image-viewer 重建场景）。

状态机：queued → running → done / cancelled；加载持久化时 running/queued → paused。
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_ERROR_CAP = 200  # 错误信息最多保留条数


class BackgroundTask:
    """通用后台任务：线程 + 状态机 + 取消 + 进度 + 可选原子持久化。"""

    def __init__(self, kind: str = 'task',
                 persist_path: Optional[Path] = None,
                 extra: Optional[dict] = None) -> None:
        self.kind = kind
        self.persist_path = Path(persist_path) if persist_path else None
        self._lock = threading.Lock()
        self._state = 'queued'
        self._stop = threading.Event()
        self._failed = False
        self._thread: Optional[threading.Thread] = None
        self._data: Dict[str, Any] = {
            'kind': kind,
            'total': 0,
            'processed': 0,
            'current': '',
            'error_count': 0,
            'errors': [],
            'started_at': time.time(),
            'finished_at': None,
            'extra': dict(extra or {}),
        }

    # ===== 只读属性 =====

    @property
    def cancelled(self) -> bool:
        """取消请求是否已发出（worker 内作为取消点检查）。"""
        return self._stop.is_set()

    @property
    def stop_event(self) -> threading.Event:
        """供线程池等长任务传入的取消事件。"""
        return self._stop

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    # ===== 进度更新（worker 内调用，线程安全） =====

    def update(self, *, total: Optional[int] = None, processed: Optional[int] = None,
               current: Optional[str] = None, errors: Optional[List[str]] = None,
               extra: Optional[dict] = None) -> None:
        with self._lock:
            d = self._data
            if total is not None:
                d['total'] = int(total)
            if processed is not None:
                d['processed'] = int(processed)
            if current is not None:
                d['current'] = str(current)
            if errors is not None:
                d['errors'] = list(errors)[-_ERROR_CAP:]
                d['error_count'] = len(d['errors'])
            if extra:
                d['extra'].update(extra)

    def add_error(self, msg: str) -> None:
        with self._lock:
            d = self._data
            d['errors'].append(str(msg))
            d['errors'] = d['errors'][-_ERROR_CAP:]
            d['error_count'] = len(d['errors'])

    # ===== 控制 =====

    def start(self, worker_fn: Callable, args: Tuple = ()) -> 'BackgroundTask':
        """启动后台线程执行 worker_fn(task, *args)。worker 正常返回且未被
        取消 → done(success)；被取消 → cancelled；抛异常 → done(失败)。"""
        with self._lock:
            if self._state == 'running':
                raise RuntimeError(f'任务 {self.kind} 已在运行')
            self._state = 'running'
            self._stop.clear()
            self._failed = False
            self._data['started_at'] = time.time()
            self._data['finished_at'] = None
            self._data['processed'] = 0
            self._data['error_count'] = 0

        def _run() -> None:
            try:
                worker_fn(self, *args)
            except Exception as e:
                self._failed = True
                self.add_error(f'任务异常: {e}')
            finally:
                with self._lock:
                    if self._stop.is_set():
                        self._state = 'cancelled'
                    else:
                        self._state = 'done'
                    self._data['finished_at'] = time.time()
                self.persist()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return self

    def cancel(self) -> bool:
        """请求取消（Event 置位）；worker 内检查 cancelled / stop_event 停止。"""
        self._stop.set()
        return True

    # ===== 状态查询 =====

    def status(self) -> Dict[str, Any]:
        with self._lock:
            d = self._data
            return {
                'kind': d['kind'],
                'state': self._state,
                'running': self._state == 'running',
                'done': self._state in ('done', 'cancelled'),
                'success': self._state == 'done' and not self._stop.is_set() and not self._failed,
                'cancelled': self._stop.is_set(),
                'total': int(d['total']),
                'processed': int(d['processed']),
                'current': d['current'],
                'error_count': int(d['error_count']),
                'errors': list(d['errors']),
                'started_at': d['started_at'],
                'finished_at': d['finished_at'],
                'extra': dict(d['extra']),
            }

    # ===== 可选持久化（断点恢复） =====

    def persist(self) -> bool:
        """原子写入任务状态；running/queued 落盘为 paused（重启后可续跑）。"""
        if not self.persist_path:
            return False
        try:
            with self._lock:
                payload = dict(self._data)
                payload['state'] = self._state
                payload['extra'] = dict(self._data['extra'])
            if payload['state'] in ('queued', 'running'):
                payload['state'] = 'paused'
            path = self.persist_path
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except Exception:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return True
        except Exception as e:
            print(f'[BackgroundTask] 持久化失败 {self.persist_path}: {e}')
            return False

    @classmethod
    def load(cls, path: Path, kind: Optional[str] = None) -> Optional['BackgroundTask']:
        """从持久化文件恢复任务；running/queued 恢复为 paused（等待 resume）。"""
        try:
            raw = json.loads(Path(path).read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                return None
            task = cls(kind=raw.get('kind') or kind or 'task',
                       persist_path=path,
                       extra=raw.get('extra') or {})
            task._data = {
                'kind': task.kind,
                'total': int(raw.get('total', 0) or 0),
                'processed': int(raw.get('processed', 0) or 0),
                'current': raw.get('current', '') or '',
                'error_count': int(raw.get('error_count', 0) or 0),
                'errors': list(raw.get('errors', []) or [])[-_ERROR_CAP:],
                'started_at': raw.get('started_at', time.time()) or time.time(),
                'finished_at': raw.get('finished_at'),
                'extra': dict(raw.get('extra', {}) or {}),
            }
            task._state = raw.get('state', 'paused')
            if task._state not in ('queued', 'running', 'paused', 'done', 'cancelled'):
                task._state = 'paused'
            return task
        except Exception:
            return None

    def resume(self, worker_fn: Callable, args: Tuple = ()) -> 'BackgroundTask':
        """从 paused 状态续跑（worker 内部应跳过已完成部分）。"""
        return self.start(worker_fn, args)
