"""上传（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from shell.backend.plugin_utils import load_sibling
from shell.groupmesh import client as mesh_client
from shell.groupmesh.transport import RemoteError, TransportError

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
_opts = _common._opts
UPLOAD_TIMEOUT_SECONDS = _common.UPLOAD_TIMEOUT_SECONDS
UPLOAD_KEEP = _common.UPLOAD_KEEP
UPLOAD_TASK_FILE = _common.UPLOAD_TASK_FILE

class UploadMixin:
    """把本机文件投放到对端共享项：任务表、续传记录与上传线程。"""

    # ── 上传：把一个本机文件投放到对端共享项 ──────────────────────────────
    #
    # 与取回对称，但方向相反：写权限由**对端**按 ACL 判定，落点也由对端决定。
    # 本机这一侧只做三件事：拒掉不该上传的来源、把相对路径的越界拒在连接之前、
    # 逐个尝试候选端点。

    def upload_remote(self, opts: Any = None, device_id: str = '', share_id: str = '',
                      local_path: str = '', remote_path: str = '',
                      overwrite: bool = False) -> Dict[str, Any]:
        """把本机文件上传到对端共享项（需要该共享项授予本机主体 `write`）。

        `remote_path` 留空时取本地文件名；可以带子目录（`sub/a.bin`），对端会自动建目录。
        目标已存在时默认**拒绝**，要覆盖必须显式 `overwrite=True` —— 写权限是可加的
        （设计文档 §6.2），覆盖等于让属主少一份内容，不能由上传方默认决定。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '')
        share_id = str(options.get('share_id') or share_id or '')
        local_path = str(options.get('local_path') or local_path or '')
        remote_path = str(options.get('remote_path') or remote_path or '')
        overwrite = bool(options.get('overwrite', overwrite))

        if not share_id:
            return {'success': False, 'error': '必须给出 share_id'}
        if not local_path:
            return {'success': False, 'error': '必须给出要上传的本机文件路径'}

        source = Path(local_path).expanduser()
        if not source.is_file():
            return {'success': False, 'error': f'本机文件不存在: {source}'}
        # 两类不许上传的来源：身份目录（明文私钥）与远端物化缓存（那是**其他成员**
        # 的数据，传出去等于二次分发，见 .dsh/group-mesh-materialize.md §2.2）。
        resolved = source.resolve()
        for guarded in (self.identity_dir, self.remote_cache_dir, self.staging_dir):
            try:
                resolved.relative_to(guarded.resolve())
            except ValueError:
                continue
            return {'success': False, 'error': f'不允许上传 {guarded} 之内的文件'}

        target = remote_path.strip().replace('\\', '/') or source.name
        if target.startswith('/') or target.endswith('/') \
                or any(part == '..' for part in Path(target).parts):
            return {'success': False, 'error': f'目标路径非法: {target!r}'}

        try:
            size = source.stat().st_size
        except OSError as e:
            return {'success': False, 'error': f'读不到本机文件: {e}'}

        task: Dict[str, Any] = {
            'task_id': uuid.uuid4().hex[:12],
            'state': 'running',
            'device_id': device_id,
            'share_id': share_id,
            'local_path': str(source),
            'remote_path': target,
            'overwrite': overwrite,
            'total': size,
            'sent': 0,
            'resumed_from': 0,
            'peer_device_id': '',
            'peer_name': '',
            'error': '',
            'started_at': int(time.time()),
            'finished_at': None,
            'cancel': threading.Event(),
        }
        self._register_upload(task)
        threading.Thread(target=self._upload_worker, args=(task,), daemon=True,
                         name=f'group-mesh-upload-{task["task_id"]}').start()
        return {'success': True, 'task_id': task['task_id'], 'state': 'running',
                'size': size, 'share_id': share_id, 'path': target,
                'local_path': str(source)}

    # ── 上传任务表 ────────────────────────────────────────────────────────

    def _register_upload(self, task: Dict[str, Any]) -> None:
        """登记任务，并只保留最近 `UPLOAD_KEEP` 个已结束的（界面轮询要看结果）。"""
        with self._uploads_guard:
            self._uploads[task['task_id']] = task
            self._upload_order.append(task['task_id'])
            self._prune_uploads()
            self._persist_uploads()

    def _prune_uploads(self) -> None:
        """按 `UPLOAD_KEEP` 淘汰最老的**已结束**任务（调用方持锁）。"""
        while len(self._upload_order) > UPLOAD_KEEP:
            oldest = self._upload_order[0]
            if self._uploads.get(oldest, {}).get('state') in ('running', 'cancelling'):
                break            # 还在传的不淘汰
            self._upload_order.pop(0)
            self._uploads.pop(oldest, None)

    def _upload_task_file(self) -> Path:
        return self.get_data_root() / UPLOAD_TASK_FILE

    def _persist_uploads(self) -> None:
        """把任务表快照落盘（调用方持锁）。

        为什么值得落盘：插件重载（改设置、升级、壳重启）会把内存里的任务表抹掉，
        用户看到的是"上传凭空消失了"。落盘之后至少能如实告诉他"上一次传到 X 时被
        中断，同一个文件再传会续传"。

        卸载之后不再写：老实例的上传线程可能还在收尾，而此时新实例已经接管了同一个
        数据根（见 `_unloading`）。
        """
        if self._unloading:
            return
        snapshot = {task_id: self._upload_view(task)
                    for task_id, task in self._uploads.items()}
        try:
            self.get_data_root().mkdir(parents=True, exist_ok=True)
            self._upload_task_file().write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as e:
            log.warning(f'[group-mesh] 上传任务表写盘失败: {e}')

    def _restore_uploads(self) -> None:
        """启动时读回任务表：**未结束的一律标记为"被中断"**。

        进程没了，线程就没了 —— 那些 `running` 是上次进程死亡时的快照，不能继续
        当作"正在上传"（界面会一直转圈等一个不存在的进度）。已结束的历史照旧保留，
        方便用户回看上次为什么失败。
        """
        path = self._upload_task_file()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            log.warning('[group-mesh] 上传任务表读取失败，按空表处理')
            return
        if not isinstance(data, dict):
            return
        with self._uploads_guard:
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
                task = {key: view.get(key) for key in (
                    'task_id', 'state', 'device_id', 'share_id', 'local_path', 'remote_path',
                    'overwrite', 'total', 'sent', 'resumed_from', 'peer_device_id', 'peer_name',
                    'error', 'started_at', 'finished_at')}
                task['task_id'] = task['task_id'] or task_id
                task['cancel'] = threading.Event()
                self._uploads[task['task_id']] = task
                visited.append(task['task_id'])
            self._upload_order = visited
            self._prune_uploads()
            self._persist_uploads()

    def _upload_view(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """给界面看的副本：Event / 内部字段不能进 JSON。"""
        total = int(task.get('total') or 0)
        sent = int(task.get('sent') or 0)
        view = {key: task.get(key) for key in
                ('task_id', 'state', 'device_id', 'share_id', 'local_path', 'remote_path',
                 'overwrite', 'total', 'sent', 'resumed_from', 'peer_device_id', 'peer_name',
                 'error', 'started_at', 'finished_at')}
        view['percent'] = int(sent * 100 / total) if total else 100
        return view

    def upload_status(self, opts: Any = None, task_id: str = '') -> Dict[str, Any]:
        """上传进度：不传 `task_id` 时返回本次运行的全部任务（新的在前）。"""
        options = _opts(opts)
        task_id = str(options.get('task_id') or task_id or '')
        with self._uploads_guard:
            if task_id:
                task = self._uploads.get(task_id)
                if task is None:
                    return {'success': False, 'error': f'没有这个上传任务: {task_id}'}
                return {'success': True, 'task': self._upload_view(task)}
            tasks = [self._upload_view(self._uploads[t]) for t in reversed(self._upload_order)
                     if t in self._uploads]
        return {'success': True, 'tasks': tasks}

    def cancel_upload(self, opts: Any = None, task_id: str = '') -> Dict[str, Any]:
        """取消一次上传。

        取消在**分块边界**生效：已经传给对端的字节留在 `<目标>.part` 里，不清除 ——
        它就是下次续传的起点（协议里没有远程删除，这也是刻意的）。因此取消的代价
        最多是"少传一点"，不是"白传一遍"。
        """
        options = _opts(opts)
        task_id = str(options.get('task_id') or task_id or '')
        if not task_id:
            return {'success': False, 'error': '必须给出 task_id'}
        with self._uploads_guard:
            task = self._uploads.get(task_id)
            if task is None:
                return {'success': False, 'error': f'没有这个上传任务: {task_id}'}
            if task['state'] in ('done', 'failed', 'cancelled'):
                return {'success': False, 'state': task['state'],
                        'error': f'任务已经结束（{task["state"]}）'}
            task['state'] = 'cancelling'
            task['cancel'].set()
            self._persist_uploads()
            view = self._upload_view(task)
        return {'success': True, 'task': view}

    # ── 续传记录（本机） ──────────────────────────────────────────────────

    def _upload_record_file(self) -> Path:
        return self.get_data_root() / 'uploads.json'

    def _upload_key(self, device_id: str, share_id: str, remote_path: str,
                    local_path: str) -> str:
        return '|'.join((device_id, share_id, remote_path, local_path))

    def _load_upload_records(self) -> Dict[str, Any]:
        path = self._upload_record_file()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_upload_record(self, key: str, size: int, mtime_ns: int, sent: int) -> None:
        """记下"这个本地文件传到哪了"，供下次续传判断是不是同一份内容的前缀。"""
        with self._uploads_guard:
            records = self._load_upload_records()
            records[key] = {'size': size, 'mtime_ns': mtime_ns, 'sent': sent,
                            'ts': int(time.time())}
            self._write_upload_records(records)

    def _clear_upload_record(self, key: str) -> None:
        with self._uploads_guard:
            records = self._load_upload_records()
            if records.pop(key, None) is not None:
                self._write_upload_records(records)

    def _write_upload_records(self, records: Dict[str, Any]) -> None:
        try:
            self.get_data_root().mkdir(parents=True, exist_ok=True)
            self._upload_record_file().write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as e:
            log.warning(f'[group-mesh] 续传记录写盘失败: {e}')

    def _resume_offset(self, key: str, source: Path, staged: int) -> int:
        """决定这次从哪儿开始传。

        三个条件缺一不可：本机有记录、记录与**当前**本地文件的 (大小, mtime) 一致、
        对端暂存字节数与记录一致。任何一个不符都从头传 —— 拿一份对不上的 `.part`
        去续，产出的是拼接垃圾，而且没人会发现。
        """
        if staged <= 0:
            return 0
        record = self._load_upload_records().get(key)
        if not isinstance(record, dict):
            return 0
        size, mtime_ns = self._local_stamp(source)
        if record.get('size') != size or record.get('mtime_ns') != mtime_ns:
            return 0
        if record.get('sent') != staged or staged >= size:
            return 0
        return staged

    def _local_stamp(self, source: Path) -> tuple:
        """本地文件的 (大小, mtime_ns)：续传时必须与记录一致，否则从头传。"""
        try:
            stat = source.stat()
        except OSError:
            return 0, 0
        return stat.st_size, stat.st_mtime_ns

    # ── 上传线程 ──────────────────────────────────────────────────────────

    def _upload_worker(self, task: Dict[str, Any]) -> None:
        """后台把文件推上去：续传起点 → 分块 → 提交；进度写回 task。

        用**专用连接**（`open_connection`，不进复用池）：复用池里的连接会被界面线程
        同时用于列目录等请求，而一条 Noise 连接上的请求是严格串行的，两个线程交错
        发送会把帧拼坏。用完即关，代价只有一次握手。
        """
        key = self._upload_key(task['device_id'], task['share_id'], task['remote_path'],
                               task['local_path'])
        source = Path(task['local_path'])
        connection = None
        try:
            identity = self._load_identity()
            roster = self._load_roster()
            if identity is None or roster is None:
                self._fail_upload(task, '需要先创建身份与团体')
                return
            candidates = self._manual_endpoints(task['device_id'], roster)
            if not candidates:
                self._fail_upload(task, '还没有可用的对端地址。请先在"对端"里登记一台设备的'
                                        '地址，或刷新一次让本机通过注册记录发现它')
                return

            last_error = ''
            for endpoint, label in candidates:
                if task['cancel'].is_set():
                    self._finish_upload(task, 'cancelled')
                    self._save_upload_record(key, *self._local_stamp(source), task['sent'])
                    return
                try:
                    connection = mesh_client.open_connection(
                        endpoint[0], endpoint[1], identity, roster,
                        timeout=UPLOAD_TIMEOUT_SECONDS)
                except (RemoteError, TransportError, OSError) as e:
                    last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                    continue
                task['peer_device_id'] = connection.peer.device_key.hex()
                task['peer_name'] = label or connection.peer.name

                try:
                    probe = mesh_client.probe_upload(connection, task['share_id'],
                                                     task['remote_path'])
                except RemoteError as e:
                    self._fail_upload(task, str(e))
                    return

                offset = self._resume_offset(key, source, int(probe.get('staged') or 0))
                task['resumed_from'] = offset
                task['sent'] = offset
                task['saved'] = offset

                def on_progress(sent: int, total: int) -> None:
                    task['sent'] = sent
                    task['total'] = total
                    # 每 4 MiB 落一次盘：断在"进程被杀"上时也能续，而每分块写一次
                    # JSON 会把时间都花在无关的 IO 上。任务表一起刷新 —— 重载后
                    # 界面看到的进度就是这里最后一次落盘的值。
                    if sent - int(task.get('saved') or 0) >= 4 * 1024 * 1024:
                        task['saved'] = sent
                        self._save_upload_record(key, *self._local_stamp(source), sent)
                        with self._uploads_guard:
                            self._persist_uploads()

                try:
                    sent = mesh_client.push_file(
                        connection, task['share_id'], source, task['remote_path'],
                        overwrite=task['overwrite'], offset=offset,
                        progress=on_progress, cancelled=task['cancel'].is_set)
                except mesh_client.UploadCancelled:
                    self._finish_upload(task, 'cancelled')
                    self._save_upload_record(key, *self._local_stamp(source), task['sent'])
                    return
                except RemoteError as e:
                    # 无权限 / 已存在 / 超配额 / 越界 / 游标不符：对端的确定性拒绝
                    self._fail_upload(task, str(e))
                    return
                except (TransportError, OSError) as e:
                    last_error = f'传输失败: {e}'
                    # 传输断了但字节留在了对端：记下进度，下次续传
                    self._save_upload_record(key, *self._local_stamp(source), task['sent'])
                    continue
                finally:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    connection = None

                task['sent'] = sent
                self._clear_upload_record(key)
                self._finish_upload(task, 'done')
                return

            self._fail_upload(task, last_error or '所有候选地址都连不上', offline=True)
        except Exception as e:      # 兜底：后台线程的异常不能让任务永远停在 running
            self._fail_upload(task, f'{type(e).__name__}: {e}')
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _finish_upload(self, task: Dict[str, Any], state: str) -> None:
        with self._uploads_guard:
            task['state'] = state
            task['finished_at'] = int(time.time())
            self._persist_uploads()

    def _fail_upload(self, task: Dict[str, Any], error: str, offline: bool = False) -> None:
        with self._uploads_guard:
            task['state'] = 'failed'
            task['error'] = error
            task['offline'] = bool(offline)
            task['finished_at'] = int(time.time())
            self._persist_uploads()

    def _cancel_all_uploads(self) -> None:
        with self._uploads_guard:
            running = [t for t in self._uploads.values()
                       if t['state'] in ('running', 'cancelling')]
        for task in running:
            task['cancel'].set()
