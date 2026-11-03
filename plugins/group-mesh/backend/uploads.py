"""上传任务表与续传记录：进度、状态、落盘与恢复。

状态自持：任务表（`tasks` / `order`）、一把独立的锁、以及卸载标志。**不持有线程、
不发网络请求** —— 上传线程在 `Uploader`，它只经由本类登记进度、落盘与读续传记录。

为什么任务表的锁要与别的锁分开：后台线程写、界面线程读，用一把独立的锁才不会让
上传线程持锁时把界面请求堵住（迁移前就是独立的 `_uploads_guard`，语义不变）。

任务表落盘的时机与原因：插件重载（改设置、升级、壳重启）会把内存里的任务表抹掉，
用户看到的是"上传凭空消失了"。落盘之后至少能如实告诉他"上一次传到 X 时被中断，
同一个文件再传会续传"。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

log = logging.getLogger(__name__)

#: 任务表里保留多少个**已结束**的任务。界面要能看到最后一次的结果（成功/失败原因），
#: 但也不能无限攒 —— 每个任务只是一小段字典，留 20 个足够翻看。
UPLOAD_KEEP = 20

#: 任务表落盘的文件名（在插件数据根下）。与 uploads.json（续传记录）分开：
#: 那份每次上传进度更新都可能要写，这份只记状态与快照，混在一起会让"进度写坏"牵连到
#: "任务历史"。
UPLOAD_TASK_FILE = 'upload-tasks.json'

#: 任务表进 JSON 前保留的字段（`cancel` 之类的对象不能进 JSON）。
_VIEW_KEYS = ('task_id', 'state', 'device_id', 'share_id', 'local_path', 'remote_path',
              'overwrite', 'total', 'sent', 'resumed_from', 'peer_device_id', 'peer_name',
              'error', 'started_at', 'finished_at')


class UploadQueue:
    def __init__(self, storage: Any, rules: Any) -> None:
        self._storage = storage
        self._rules = rules
        # 上传任务表：task_id -> 任务字典（进度、状态、取消标志）。
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []
        self.guard = threading.Lock()
        # 卸载后老实例的上传线程可能还在收尾（取消要等到分块边界）。它**不得**再写
        # 任务表：同一个数据根已经交给新实例了，写回去会把新实例刚记下的任务覆盖掉。
        self.unloading = False

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def begin_load(self) -> None:
        self.unloading = False

    def begin_unload(self) -> None:
        self.unloading = True

    # ── 任务表 ────────────────────────────────────────────────────────────

    def register(self, task: Dict[str, Any]) -> None:
        """登记任务，并只保留最近 `UPLOAD_KEEP` 个已结束的（界面轮询要看结果）。"""
        with self.guard:
            self.tasks[task['task_id']] = task
            self.order.append(task['task_id'])
            self.prune()
            self.persist()

    def prune(self) -> None:
        """按 `UPLOAD_KEEP` 淘汰最老的**已结束**任务（调用方持锁）。"""
        while len(self.order) > UPLOAD_KEEP:
            oldest = self.order[0]
            if self.tasks.get(oldest, {}).get('state') in ('running', 'cancelling'):
                break            # 还在传的不淘汰
            self.order.pop(0)
            self.tasks.pop(oldest, None)

    def task_file(self) -> Path:
        return self._storage.root / UPLOAD_TASK_FILE

    def persist(self) -> None:
        """把任务表快照落盘（调用方持锁）。

        卸载之后不再写：老实例的上传线程可能还在收尾，而此时新实例已经接管了同一个
        数据根（见 `unloading`）。
        """
        if self.unloading:
            return
        snapshot = {task_id: self.view(task)
                    for task_id, task in self.tasks.items()}
        try:
            self._storage.root.mkdir(parents=True, exist_ok=True)
            self.task_file().write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as e:
            log.warning(f'[group-mesh] 上传任务表写盘失败: {e}')

    def restore(self) -> None:
        """启动时读回任务表：**未结束的一律标记为"被中断"**。

        进程没了，线程就没了 —— 那些 `running` 是上次进程死亡时的快照，不能继续
        当作"正在上传"（界面会一直转圈等一个不存在的进度）。已结束的历史照旧保留，
        方便用户回看上次为什么失败。
        """
        path = self.task_file()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            log.warning('[group-mesh] 上传任务表读取失败，按空表处理')
            return
        if not isinstance(data, dict):
            return
        with self.guard:
            visited: List[str] = []
            for task_id in sorted(data, key=lambda t: int(
                    (data.get(t) or {}).get('started_at') or 0)):
                view = data.get(task_id)
                if not isinstance(view, dict):
                    continue
                view = dict(view)
                if view.get('state') in ('running', 'cancelling'):
                    view['state'] = 'interrupted'
                    view['error'] = ('插件重载导致上传中断；同一个文件再次上传会从断点续传')
                    view['finished_at'] = view.get('finished_at') or int(time.time())
                task = {key: view.get(key) for key in _VIEW_KEYS}
                task['task_id'] = task['task_id'] or task_id
                task['cancel'] = threading.Event()
                self.tasks[task['task_id']] = task
                visited.append(task['task_id'])
            self.order = visited
            self.prune()
            self.persist()

    def view(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """给界面看的副本：Event / 内部字段不能进 JSON。"""
        total = int(task.get('total') or 0)
        sent = int(task.get('sent') or 0)
        view = {key: task.get(key) for key in _VIEW_KEYS}
        view['percent'] = int(sent * 100 / total) if total else 100
        return view

    def finish(self, task: Dict[str, Any], state: str) -> None:
        with self.guard:
            task['state'] = state
            task['finished_at'] = int(time.time())
            self.persist()

    def fail(self, task: Dict[str, Any], error: str, offline: bool = False) -> None:
        with self.guard:
            task['state'] = 'failed'
            task['error'] = error
            task['offline'] = bool(offline)
            task['finished_at'] = int(time.time())
            self.persist()

    def cancel_all(self) -> None:
        with self.guard:
            running = [t for t in self.tasks.values()
                       if t['state'] in ('running', 'cancelling')]
        for task in running:
            task['cancel'].set()

    # ── 界面 API ──────────────────────────────────────────────────────────

    def status(self, opts: Any = None, task_id: str = '') -> Dict[str, Any]:
        """上传进度：不传 `task_id` 时返回本次运行的全部任务（新的在前）。"""
        options = self._rules.opts(opts)
        task_id = str(options.get('task_id') or task_id or '')
        with self.guard:
            if task_id:
                task = self.tasks.get(task_id)
                if task is None:
                    return {'success': False, 'error': f'没有这个上传任务: {task_id}'}
                return {'success': True, 'task': self.view(task)}
            tasks = [self.view(self.tasks[t]) for t in reversed(self.order)
                     if t in self.tasks]
        return {'success': True, 'tasks': tasks}

    def cancel(self, opts: Any = None, task_id: str = '') -> Dict[str, Any]:
        """取消一次上传。

        取消在**分块边界**生效：已经传给对端的字节留在 `<目标>.part` 里，不清除 ——
        它就是下次续传的起点（协议里没有远程删除，这也是刻意的）。因此取消的代价
        最多是"少传一点"，不是"白传一遍"。
        """
        options = self._rules.opts(opts)
        task_id = str(options.get('task_id') or task_id or '')
        if not task_id:
            return {'success': False, 'error': '必须给出 task_id'}
        with self.guard:
            task = self.tasks.get(task_id)
            if task is None:
                return {'success': False, 'error': f'没有这个上传任务: {task_id}'}
            if task['state'] in ('done', 'failed', 'cancelled'):
                return {'success': False, 'state': task['state'],
                        'error': f'任务已经结束（{task["state"]}）'}
            task['state'] = 'cancelling'
            task['cancel'].set()
            self.persist()
            view = self.view(task)
        return {'success': True, 'task': view}

    # ── 续传记录（本机）──────────────────────────────────────────────────

    def record_file(self) -> Path:
        return self._storage.root / 'uploads.json'

    @staticmethod
    def upload_key(device_id: str, share_id: str, remote_path: str,
                   local_path: str) -> str:
        return '|'.join((device_id, share_id, remote_path, local_path))

    def load_records(self) -> Dict[str, Any]:
        path = self.record_file()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_record(self, key: str, size: int, mtime_ns: int, sent: int) -> None:
        """记下"这个本地文件传到哪了"，供下次续传判断是不是同一份内容的前缀。"""
        with self.guard:
            records = self.load_records()
            records[key] = {'size': size, 'mtime_ns': mtime_ns, 'sent': sent,
                            'ts': int(time.time())}
            self._write_records(records)

    def clear_record(self, key: str) -> None:
        with self.guard:
            records = self.load_records()
            if records.pop(key, None) is not None:
                self._write_records(records)

    def _write_records(self, records: Dict[str, Any]) -> None:
        try:
            self._storage.root.mkdir(parents=True, exist_ok=True)
            self.record_file().write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as e:
            log.warning(f'[group-mesh] 续传记录写盘失败: {e}')

    def resume_offset(self, key: str, source: Path, staged: int) -> int:
        """决定这次从哪儿开始传。

        三个条件缺一不可：本机有记录、记录与**当前**本地文件的 (大小, mtime) 一致、
        对端暂存字节数与记录一致。任何一个不符都从头传 —— 拿一份对不上的 `.part`
        去续，产出的是拼接垃圾，而且没人会发现。
        """
        if staged <= 0:
            return 0
        record = self.load_records().get(key)
        if not isinstance(record, dict):
            return 0
        size, mtime_ns = self.local_stamp(source)
        if record.get('size') != size or record.get('mtime_ns') != mtime_ns:
            return 0
        if record.get('sent') != staged or staged >= size:
            return 0
        return staged


    @staticmethod
    def local_stamp(source: Path) -> Tuple[int, int]:
        """本地文件的 (大小, mtime_ns)：续传时必须与记录一致，否则从头传。"""
        try:
            stat = source.stat()
        except OSError:
            return 0, 0
        return stat.st_size, stat.st_mtime_ns
