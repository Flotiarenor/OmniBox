"""上传：把一个本机文件投放到对端共享项。

状态自持：无。任务表与续传记录在 `UploadQueue`（它持有任务表那把锁），候选端点由
`MeshRegistry` 给。上传线程在本组件里起，但它落盘/收尾一律走队列的方法。

与取回对称，但方向相反：写权限由**对端**按 ACL 判定，落点也由对端决定。
本机这一侧只做三件事：拒掉不该上传的来源、把相对路径的越界拒在连接之前、
逐个尝试候选端点。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from shell.groupmesh import client as mesh_client
from shell.groupmesh.transport import RemoteError, TransportError

log = logging.getLogger(__name__)

#: 上传的 socket 超时（秒）。比探测用的 3 秒宽得多：一个分块要走 Noise 加密 + 落盘，
#: 慢盘或大分块下 3 秒会误判成"传输失败"。取消不靠超时兜底 —— 客户端在分块边界
#: 检查取消标志（见 `worker`）。
UPLOAD_TIMEOUT_SECONDS = 30.0


class Uploader:
    def __init__(self, storage: Any, registry: Any, queue: Any, rules: Any) -> None:
        self._storage = storage
        self._registry_store = registry
        self._queue = queue
        self._rules = rules

    def upload_remote(self, opts: Any = None, device_id: str = '', share_id: str = '',
                      local_path: str = '', remote_path: str = '',
                      overwrite: bool = False) -> Dict[str, Any]:
        """把本机文件上传到对端共享项（需要该共享项授予本机主体 `write`）。

        `remote_path` 留空时取本地文件名；可以带子目录（`sub/a.bin`），对端会自动建目录。
        目标已存在时默认**拒绝**，要覆盖必须显式 `overwrite=True` —— 写权限是可加的，
        覆盖等于让属主少一份内容，不能由上传方默认决定。
        """
        options = self._rules.opts(opts)
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
        # 的数据，传出去等于二次分发）。
        resolved = source.resolve()
        for guarded in (self._storage.identity_dir, self._storage.remote_cache_dir,
                        self._storage.staging_dir):
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
        self._queue.register(task)
        threading.Thread(target=self.worker, args=(task,), daemon=True,
                         name=f'group-mesh-upload-{task["task_id"]}').start()
        return {'success': True, 'task_id': task['task_id'], 'state': 'running',
                'size': size, 'share_id': share_id, 'path': target,
                'local_path': str(source)}

    # ── 上传线程 ──────────────────────────────────────────────────────────

    def worker(self, task: Dict[str, Any]) -> None:
        """后台把文件推上去：续传起点 → 分块 → 提交；进度写回 task。

        用**专用连接**（`open_connection`，不进复用池）：复用池里的连接会被界面线程
        同时用于列目录等请求，而一条 Noise 连接上的请求是严格串行的，两个线程交错
        发送会把帧拼坏。用完即关，代价只有一次握手。
        """
        queue = self._queue
        key = queue.upload_key(task['device_id'], task['share_id'], task['remote_path'],
                               task['local_path'])
        source = Path(task['local_path'])
        connection = None
        try:
            identity = self._storage.load_identity()
            roster = self._storage.load_roster()
            if identity is None or roster is None:
                queue.fail(task, '需要先创建身份与团体')
                return
            candidates = self._registry_store.candidates_for(task['device_id'], roster)
            if not candidates:
                queue.fail(task, '还没有可用的对端地址。请先在"对端"里登记一台设备的'
                                 '地址，或刷新一次让本机通过注册记录发现它')
                return

            last_error = ''
            for endpoint, label in candidates:
                if task['cancel'].is_set():
                    queue.finish(task, 'cancelled')
                    queue.save_record(key, *queue.local_stamp(source), task['sent'])
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
                    queue.fail(task, str(e))
                    return

                offset = queue.resume_offset(key, source, int(probe.get('staged') or 0))
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
                        queue.save_record(key, *queue.local_stamp(source), sent)
                        with queue.guard:
                            queue.persist()

                try:
                    sent = mesh_client.push_file(
                        connection, task['share_id'], source, task['remote_path'],
                        overwrite=task['overwrite'], offset=offset,
                        progress=on_progress, cancelled=task['cancel'].is_set)
                except mesh_client.UploadCancelled:
                    queue.finish(task, 'cancelled')
                    queue.save_record(key, *queue.local_stamp(source), task['sent'])
                    return
                except RemoteError as e:
                    # 无权限 / 已存在 / 超配额 / 越界 / 游标不符：对端的确定性拒绝
                    queue.fail(task, str(e))
                    return
                except (TransportError, OSError) as e:
                    last_error = f'传输失败: {e}'
                    # 传输断了但字节留在了对端：记下进度，下次续传
                    queue.save_record(key, *queue.local_stamp(source), task['sent'])
                    continue
                finally:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    connection = None

                task['sent'] = sent
                queue.clear_record(key)
                queue.finish(task, 'done')
                return

            queue.fail(task, last_error or '所有候选地址都连不上', offline=True)
        except Exception as e:      # 兜底：后台线程的异常不能让任务永远停在 running
            queue.fail(task, f'{type(e).__name__}: {e}')
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
