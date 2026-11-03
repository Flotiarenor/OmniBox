"""后台扫描任务：单飞、进度投影与取消。

状态自持：当前任务与"查-建-赋值"的锁都在这里。Flask 是 threaded=True，原先这段没有锁，
两个并发请求可以各起一个任务互相覆盖 —— 前一个仍在跑，取消就再也够不着它。

取消的兼容路径（用户点共享组件上的「取消」）通过入口注入的 `engine_provider` 取统一刷新
引擎：壳把引擎挂在插件实例上，组件不持有插件对象，只按需问"引擎在哪"。
"""

import threading
from typing import Any, Callable, Dict, Optional

from shell.backend.tasks import BackgroundTask


class ScanTaskRunner:
    """后台扫描任务的启动、状态投影与取消。"""

    def __init__(self, kind: str, pipeline: Any, results: Any,
                 check_cancel: Callable[[Any], None],
                 cancelled_error: Any,
                 engine_provider: Callable[[], Any]) -> None:
        self._kind = kind
        self._pipeline = pipeline
        self._results = results
        self._check_cancel = check_cancel
        # 取消点抛出的异常类型由入口注入：组件之间不 import，异常也就不能在这里命名。
        self._cancelled_error = cancelled_error
        self._engine_provider = engine_provider
        # 同时只允许一个任务在跑；锁保护"查-建-赋值"。
        self._task: Optional[BackgroundTask] = None
        self._lock = threading.Lock()

    def start(self, mode: str = 'dupe', threshold: Optional[int] = None) -> Dict:
        """把整库重新分组放进后台任务（兼容入口）。

        接入统一刷新后，用户点的是共享组件上的「校验」：它维护指纹缓存并在整趟结束时把
        两个模式都重新分组、写进结果缓存。这个方法保留给"只想重算某一个模式的分组"的
        调用方（老前端、调试脚本），语义与 worker 一致：算分组 → 写缓存 → 取消时不写。

        任务状态**不落盘**：本插件没有"断点续跑"语义，重启后重新校验即可
        （指纹与结果缓存都在）。
        """
        mode = 'similar' if str(mode) == 'similar' else 'dupe'
        with self._lock:
            if self._task is not None and self._task.state in ('running', 'queued'):
                return {'started': False, 'running': True, **self.status()}
            task = BackgroundTask(kind=self._kind, extra={'mode': mode, 'groups': 0})
            self._task = task
        task.start(self.worker, args=(mode, threshold))
        return {'started': True, 'running': True, 'mode': mode}

    def worker(self, task: BackgroundTask, mode: str,
               threshold: Optional[int]) -> None:
        """后台扫描 worker：算分组 → 写扫描缓存；取消时不写任何结果。"""
        try:
            files = self._pipeline.collect()
            self._check_cancel(task)
            groups, scanned = self._pipeline.group(mode, files, task, threshold)
        except Exception as error:
            if isinstance(error, self._cancelled_error):
                return
            raise
        self._results.save_result(mode, groups, scanned)
        task.update(current='', extra={'groups': len(groups), 'scanned': scanned})

    def status(self) -> Dict:
        """后台扫描任务的进度（`BackgroundTask.status()` 的投影）。"""
        task = self._task
        if task is None:
            return {'running': False, 'done': False, 'success': False, 'cancelled': False,
                    'mode': '', 'total': 0, 'processed': 0, 'current': '', 'groups': 0}
        status = task.status()
        return {
            'running': status['running'],
            'done': status['done'],
            'success': status['success'],
            'cancelled': status['cancelled'],
            'mode': status['extra'].get('mode', ''),
            'total': status['total'],
            'processed': status['processed'],
            'current': status['current'],
            'groups': int(status['extra'].get('groups', 0) or 0),
        }

    def cancel(self) -> Dict:
        """请求取消当前扫描；取消的任务不写结果缓存（没有"半份结果"）。"""
        if self._task is not None and self._task.state in ('running', 'queued'):
            self._task.cancel()
            return {'success': True}
        # 兼容：取消统一校验（用户点的是共享组件上的「取消」）
        engine = self._engine_provider()
        if engine is not None and engine.request_cancel():
            return {'success': True}
        return {'success': False, 'error': '没有正在运行的扫描任务'}

    def shutdown(self) -> None:
        """进程退出收尾：取消正在跑的扫描任务（已写出的结果缓存保留）。"""
        if self._task is not None and self._task.state in ('running', 'queued'):
            self._task.cancel()
