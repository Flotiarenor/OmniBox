"""下载中心：任务表、暂停 / 恢复 / 重试、工作线程与 `album_info.json` 落盘。

与漫画库共用数据根，但两份状态互不相干（书架在 `.cache/manga_state.json`，
下载记录在 `.jmcomic_state/download_state.json`）。

暂停语义：本组件只负责把任务的停止信号置上并改状态，**真正的中断发生在下载
循环里**（`downloader` 模块的块级检查点）—— 只置信号而循环不查，界面显示"已暂停"
而线程继续写盘，是这条链路原来的缺陷。下载执行器由入口注入，本组件不 import
兄弟模块（分层门禁 R3）。
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

#: 任务列表的排序权重：优先级高的在前，同优先级按开始时间。
_PRIORITY_ORDER = {'high': 0, 'normal': 1, 'low': 2}


class DownloadCenter:
    """下载任务表的唯一持有者。

    状态自持：任务字典、串行化锁、工作线程都在这里。换根由入口先
    `PathLayout.rebind()`，再调本类的 `rebind()` 重读任务表。
    """

    def __init__(self, paths: Any, task_cls: Any, load_tasks: Any, save_tasks: Any,
                 run_download: Any) -> None:
        self._paths = paths
        self._task_cls = task_cls
        self._load_tasks = load_tasks
        self._save_tasks = save_tasks
        self._run_download = run_download
        self._lock = threading.Lock()
        self._state_file = paths.download_state_file()
        self.tasks: Dict[str, Any] = self._load()

    # ===== 任务表读写 =====

    def _load(self) -> Dict[str, Any]:
        return self._load_tasks(self._state_file, self._task_cls, log)

    def _save(self) -> None:
        self._save_tasks(self.tasks, self._state_file, log)

    def rebind(self) -> None:
        """换根后重读任务表（由入口编排；锁不换 —— 换锁等于放弃互斥）。"""
        with self._lock:
            self._state_file = self._paths.download_state_file()
            self.tasks = self._load()

    # ===== 查询 =====

    def list_tasks(self) -> Dict:
        """下载任务列表摘要。"""
        with self._lock:
            tasks = [task.to_api_dict() for task in self.tasks.values()]
        tasks.sort(key=lambda task: (
            _PRIORITY_ORDER.get(task.get('priority'), 1),
            task.get('startTime') or '',
        ))
        return {'tasks': tasks}

    def album_info(self, album_id: str) -> Dict:
        """读取已下载漫画的 `album_info.json`。"""
        root = Path(self._paths.root)
        info_path = root / album_id / "album_info.json"
        try:
            info_path = info_path.resolve()
            if not info_path.is_relative_to(root.resolve()):
                return {'error': '非法路径', 'exists': False}
            if not info_path.exists():
                return {'error': '未找到漫画信息文件', 'exists': False}
            with open(info_path, 'r', encoding='utf-8') as handle:
                info = json.load(handle)
            info['exists'] = True
            return info
        except Exception as e:
            return {'error': f'读取漫画信息失败: {e}', 'exists': False}

    # ===== 任务生命周期 =====

    def submit(self, album_id: str, concurrency: int = 3,
               priority: str = 'normal', auto_start: bool = True) -> Dict:
        # album_id 直接用 os.path.join 拼下载目录，必须是纯数字专辑号：
        # 传 "../../../Users/Public/x" 会让 os.path.join 折叠越界，
        # 随后 os.makedirs(exist_ok=True) 就会在漫画根目录之外建目录并写入图片。
        album_id = str(album_id).strip()
        if not album_id.isdigit():
            return {'success': False,
                    'error': f'非法的专辑号: {album_id!r}（必须是纯数字）'}

        task_id = str(uuid.uuid4())
        task = self._task_cls(
            id=task_id,
            album_id=album_id,
            download_dir=os.path.join(str(self._paths.root), album_id),
            concurrency=max(1, min(10, int(concurrency))),
            priority=priority if priority in ('high', 'normal', 'low') else 'normal',
            status='queued' if auto_start else 'paused',
            start_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )
        with self._lock:
            self.tasks[task_id] = task
            self._save()
        if auto_start:
            self._start(task_id)
        return {'taskId': task_id, 'success': True}

    def pause(self, task_id: str) -> Dict:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}
            if task.status == 'downloading':
                task._stop_event.set()
                task.status = 'paused'
                self._save()
                return {'success': True}
            return {'success': False,
                    'error': f'任务状态为 {task.status}，无法暂停'}

    def resume(self, task_id: str) -> Dict:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}
            if task.status == 'paused':
                task._stop_event.clear()
                task.status = 'queued'
                self._save()
                self._start_locked(task_id)
                return {'success': True}
            return {'success': False,
                    'error': f'任务状态为 {task.status}，无法恢复'}

    def retry(self, task_id: str) -> Dict:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}
            if task.status == 'failed':
                task.status = 'queued'
                task.error = ''
                task.completed_images = 0
                task._stop_event.clear()
                self._save()
                self._start_locked(task_id)
                return {'success': True}
            return {'success': False,
                    'error': f'任务状态为 {task.status}，无法重试'}

    def delete(self, task_id: str) -> Dict:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}
            if task.status == 'downloading':
                task._stop_event.set()
            del self.tasks[task_id]
            self._save()
            return {'success': True}

    def start_all(self) -> Dict:
        with self._lock:
            for task_id, task in self.tasks.items():
                if task.status == 'paused':
                    task._stop_event.clear()
                    task.status = 'queued'
                    self._start_locked(task_id)
            self._save()
        return {'success': True}

    def pause_all(self) -> Dict:
        with self._lock:
            for task in self.tasks.values():
                if task.status == 'downloading':
                    task._stop_event.set()
                    task.status = 'paused'
            self._save()
        return {'success': True}

    def clear_completed(self) -> Dict:
        with self._lock:
            to_delete = [task_id for task_id, task in self.tasks.items()
                         if task.status == 'completed']
            for task_id in to_delete:
                del self.tasks[task_id]
            self._save()
        return {'success': True, 'deleted': len(to_delete)}

    # ===== 工作线程 =====

    def _start(self, task_id: str) -> None:
        with self._lock:
            self._start_locked(task_id)

    def _start_locked(self, task_id: str) -> None:
        """在**已持锁**的调用点里起工作线程。

        `resume` / `retry` / `start_all` 都在 `with self._lock:` 里调用它：锁是不可重入的
        `threading.Lock`，再进一次 `_start` 会把请求线程永久挂住（恢复 / 重试 / 全部开始
        这三个入口以前就是这样卡住的，与"暂停不可中断"是两条独立的缺陷）。
        """
        task = self.tasks.get(task_id)
        if not task or (task._thread and task._thread.is_alive()):
            return
        task._thread = threading.Thread(
            target=self._worker, args=(task_id,), daemon=True)
        task._thread.start()

    def _worker(self, task_id: str) -> None:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.status = 'downloading'
            self._save()

        try:
            album_info: Optional[Dict] = self._run_download(
                task, str(self._paths.root), self._paths.download_state_dir(),
                self._lock, log)
            with self._lock:
                task = self.tasks.get(task_id)
                if task and task._stop_event.is_set():
                    return
                if task:
                    task.status = 'completed'
                    task.complete_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self._save()
                    if album_info:
                        self._save_album_info(task, album_info)
        except Exception as e:
            log.error(f'下载任务 {task_id} 失败: {e}')
            with self._lock:
                task = self.tasks.get(task_id)
                if task:
                    task.status = 'failed'
                    task.error = str(e)
                    self._save()

    def _save_album_info(self, task: Any, album_info: Dict) -> None:
        try:
            download_dir = task.download_dir or os.path.join(
                str(self._paths.root), task.album_id)
            json_path = os.path.join(download_dir, "album_info.json")
            album_info['download_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 早先漫画的 page_count 恒为 0（api 客户端），只能拿"已完成数"充总数；
            # 现在 downloader 按章节累加出了真值，优先写它，避免续传时把总数写小。
            album_info['total_page_count'] = task.total_images or task.completed_images
            with open(json_path, 'w', encoding='utf-8') as handle:
                json.dump(album_info, handle, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            log.error(f"保存 album_info.json 失败: {e}", exc_info=True)
