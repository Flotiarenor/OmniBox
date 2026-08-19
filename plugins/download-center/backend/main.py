import os
import json
import uuid
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling



_models = load_sibling(__file__, 'models', 'download_center')
_state_mod = load_sibling(__file__, 'state', 'download_center')
_downloader = load_sibling(__file__, 'downloader', 'download_center')

DownloadTask = _models.DownloadTask
load_tasks = _state_mod.load_tasks
save_tasks = _state_mod.save_tasks
execute_download = _downloader.execute_download

logger = logging.getLogger(__name__)


class DownloadCenterPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "下载根目录", "type": "text",
         "placeholder": "默认: ./data", "help": "漫画下载后保存的根目录（与漫画库共用）"},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir') or str(super().get_data_root())
        self.manga_dir = str(Path(root).resolve())
        self._state_dir = self._get_state_dir()
        self._state_file = os.path.join(self._state_dir, 'download_state.json')
        self._lock = threading.Lock()
        self.tasks: Dict[str, DownloadTask] = load_tasks(self._state_file, DownloadTask, logger)

    # ===== 文件服务根目录 =====

    def get_data_root(self) -> Path:
        return Path(self.manga_dir)

    # ===== 设置持久化 =====



    def on_settings_changed(self, changed_keys):
        if 'root_dir' in changed_keys:
            new_dir = self.setting('root_dir')
            if new_dir and Path(new_dir).is_dir():
                self._apply_root_dir(str(Path(new_dir).resolve()))

    def _apply_root_dir(self, new_dir: str):
        self.manga_dir = new_dir
        self._state_dir = self._get_state_dir()
        self._state_file = os.path.join(self._state_dir, 'download_state.json')
        self.tasks = load_tasks(self._state_file, DownloadTask, logger)

    def _get_state_dir(self) -> str:
        """获取状态存储目录（下载根目录下的隐藏文件夹，漫画库会忽略以.开头的目录）"""
        path = os.path.join(self.manga_dir, '.jmcomic_state')
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except (PermissionError, OSError):
            fallback = os.path.join(os.path.expanduser('~'), '.jmcomic_state')
            os.makedirs(fallback, exist_ok=True)
            return fallback

    # ===== API 注册 =====

    def register_api(self) -> dict:
        return {
            'download_submit': self.submit,
            'download_list': self.get_summary,
            'download_get_album_info': self.get_album_info,
            'download_pause': self.pause_task,
            'download_resume': self.resume_task,
            'download_retry': self.retry_task,
            'download_delete': self.delete_task,
            'download_start_all': self.start_all,
            'download_pause_all': self.pause_all,
            'download_clear_completed': self.clear_completed,
            'download_detail': self.get_detail,
            'dialog_select_directory': self.dialog_select_directory,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    # ===== 核心业务（由旧版 DownloadModule 迁移） =====

    def list_tasks(self) -> Dict[str, Any]:
        """获取所有任务列表"""
        with self._lock:
            tasks = []
            for task in self.tasks.values():
                tasks.append(task.to_api_dict())
            priority_order = {'high': 0, 'normal': 1, 'low': 2}
            tasks.sort(key=lambda t: (
                priority_order.get(t['priority'], 1),
                t.get('start_time', '') or ''
            ))
            return {'tasks': tasks}

    def add_task(self, album_id: str, download_dir: Optional[str] = None,
                 concurrency: int = 3, priority: str = 'normal',
                 auto_start: bool = True) -> Dict[str, Any]:
        """添加新的下载任务"""
        task_id = str(uuid.uuid4())

        if download_dir is None:
            download_dir = os.path.join(self.manga_dir, album_id)

        task = DownloadTask(
            id=task_id,
            album_id=album_id,
            download_dir=download_dir,
            concurrency=max(1, min(10, int(concurrency))),
            priority=priority if priority in ('high', 'normal', 'low') else 'normal',
            status='queued' if auto_start else 'paused',
            start_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )

        with self._lock:
            self.tasks[task_id] = task
            save_tasks(self.tasks, self._state_file, logger)

        if auto_start:
            self._start_download(task_id)

        return {'taskId': task_id, 'success': True}

    def pause_task(self, task_id: str) -> Dict[str, Any]:
        """暂停任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}

            if task.status == 'downloading':
                task._stop_event.set()
                task.status = 'paused'
                save_tasks(self.tasks, self._state_file, logger)
                return {'success': True}

            return {'success': False, 'error': f'任务状态为 {task.status}，无法暂停'}

    def resume_task(self, task_id: str) -> Dict[str, Any]:
        """恢复任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}

            if task.status == 'paused':
                task._stop_event.clear()
                task.status = 'queued'
                save_tasks(self.tasks, self._state_file, logger)
                self._start_download(task_id)
                return {'success': True}

            return {'success': False, 'error': f'任务状态为 {task.status}，无法恢复'}

    def retry_task(self, task_id: str) -> Dict[str, Any]:
        """重试失败任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}

            if task.status == 'failed':
                task.status = 'queued'
                task.error = ''
                task.completed_images = 0
                task._stop_event.clear()
                save_tasks(self.tasks, self._state_file, logger)
                self._start_download(task_id)
                return {'success': True}

            return {'success': False, 'error': f'任务状态为 {task.status}，无法重试'}

    def delete_task(self, task_id: str) -> Dict[str, Any]:
        """删除任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}

            if task.status == 'downloading':
                task._stop_event.set()

            del self.tasks[task_id]
            save_tasks(self.tasks, self._state_file, logger)
            return {'success': True}

    def start_all(self) -> Dict[str, Any]:
        """全部开始"""
        with self._lock:
            for task_id, task in self.tasks.items():
                if task.status == 'paused':
                    task._stop_event.clear()
                    task.status = 'queued'
                    self._start_download(task_id)
            save_tasks(self.tasks, self._state_file, logger)
        return {'success': True}

    def pause_all(self) -> Dict[str, Any]:
        """全部暂停"""
        with self._lock:
            for task in self.tasks.values():
                if task.status == 'downloading':
                    task._stop_event.set()
                    task.status = 'paused'
            save_tasks(self.tasks, self._state_file, logger)
        return {'success': True}

    def clear_completed(self) -> Dict[str, Any]:
        """清除所有已完成任务"""
        with self._lock:
            to_delete = [tid for tid, t in self.tasks.items() if t.status == 'completed']
            for tid in to_delete:
                del self.tasks[tid]
            save_tasks(self.tasks, self._state_file, logger)
        return {'success': True, 'deleted': len(to_delete)}

    def get_detail(self, task_id: str) -> Dict[str, Any]:
        """获取任务详情"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'error': '任务不存在'}
            return task.to_api_dict(detail=True)

    def submit(self, album_id: str, concurrency: int = 3,
               priority: str = 'normal', auto_start: bool = True) -> Dict[str, Any]:
        """提交下载任务"""
        task_id = str(uuid.uuid4())
        download_dir = os.path.join(self.manga_dir, album_id)
        task = DownloadTask(
            id=task_id,
            album_id=album_id,
            download_dir=download_dir,
            concurrency=max(1, min(10, int(concurrency))),
            priority=priority if priority in ('high', 'normal', 'low') else 'normal',
            status='queued' if auto_start else 'paused',
            start_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )
        with self._lock:
            self.tasks[task_id] = task
            save_tasks(self.tasks, self._state_file, logger)
        if auto_start:
            self._start_download(task_id)
        return {'taskId': task_id, 'success': True}

    def get_summary(self) -> Dict[str, Any]:
        """获取下载列表摘要（只读）"""
        with self._lock:
            tasks = []
            for task in self.tasks.values():
                tasks.append({
                    'id': task.id,
                    'albumId': task.album_id,
                    'title': task.title,
                    'thumbUrl': task.thumb_url,
                    'status': task.status,
                    'totalImages': task.total_images,
                    'completedImages': task.completed_images,
                    'speed': task.speed,
                    'eta': task.eta,
                })
            return {'tasks': tasks}

    def get_album_info(self, album_id: str) -> Dict[str, Any]:
        """读取已下载漫画的 album_info.json"""
        info_path = Path(self.manga_dir) / album_id / "album_info.json"
        try:
            info_path = info_path.resolve()
            if not info_path.is_relative_to(Path(self.manga_dir).resolve()):
                return {'error': '非法路径', 'exists': False}
            if not info_path.exists():
                return {'error': '未找到漫画信息文件', 'exists': False}
            with open(info_path, 'r', encoding='utf-8') as f:
                info = json.load(f)
            info['exists'] = True
            return info
        except Exception as e:
            return {'error': f'读取漫画信息失败: {e}', 'exists': False}

    def dialog_select_directory(self) -> str:
        """弹出目录选择对话框"""
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            directory = filedialog.askdirectory(title='选择下载目录')
            root.destroy()
            return directory if directory else ''
        except Exception:
            return ''

    # ===== 下载核心逻辑 =====

    def _start_download(self, task_id: str):
        """启动下载线程"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return

            if task._thread and task._thread.is_alive():
                return

            task._thread = threading.Thread(
                target=self._download_worker,
                args=(task_id,),
                daemon=True
            )
            task._thread.start()

    def _download_worker(self, task_id: str):
        """下载工作线程"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            task.status = 'downloading'
            save_tasks(self.tasks, self._state_file, logger)

        try:
            album_info = self._execute_download(task)

            with self._lock:
                task = self.tasks.get(task_id)
                if task and task._stop_event.is_set():
                    return
                if task:
                    task.status = 'completed'
                    task.complete_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    save_tasks(self.tasks, self._state_file, logger)

                    if album_info:
                        self._save_album_info(task, album_info)

        except Exception as e:
            logger.error(f'下载任务 {task_id} 失败: {e}')
            with self._lock:
                task = self.tasks.get(task_id)
                if task:
                    task.status = 'failed'
                    task.error = str(e)
                    save_tasks(self.tasks, self._state_file, logger)

    def _execute_download(self, task: DownloadTask) -> Optional[Dict]:
        """执行实际的下载逻辑，返回漫画信息（已拆到 downloader.py）。"""
        return execute_download(task, self.manga_dir, self._state_dir, self._lock, logger)

    def _save_album_info(self, task: DownloadTask, album_info: Dict):
        """下载完成后保存 album_info.json"""
        try:
            download_dir = task.download_dir or os.path.join(self.manga_dir, task.album_id)
            json_path = os.path.join(download_dir, "album_info.json")

            album_info['download_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            album_info['total_page_count'] = task.completed_images

            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(album_info, f, ensure_ascii=False, indent=2, default=str)

            logger.info(f"漫画信息已保存到: {json_path}")
        except Exception as e:
            logger.error(f"保存 album_info.json 失败: {e}", exc_info=True)

    # ===== 状态持久化 =====
