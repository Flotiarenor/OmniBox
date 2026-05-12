# backend/modules/download_module.py
import os
import json
import uuid
import time
import threading
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class DownloadTask:
    """下载任务数据模型"""
    id: str
    album_id: str
    title: str = ''
    thumb_url: str = ''
    status: str = 'queued'  # queued/downloading/paused/completed/failed
    total_images: int = 0
    completed_images: int = 0
    speed: float = 0.0  # bytes/s
    eta: int = 0  # seconds
    priority: str = 'normal'  # high/normal/low
    download_dir: str = ''
    error: str = ''
    start_time: str = ''
    complete_time: str = ''
    concurrency: int = 3
    chapters: List[Dict] = field(default_factory=list)
    _thread: Optional[threading.Thread] = None
    _stop_event: threading.Event = field(default_factory=threading.Event)


class DownloadModule:
    """
    下载管理模块
    负责管理漫画下载任务的创建、调度、监控和持久化
    """

    def __init__(self, manga_dir: str):
        self.manga_dir = manga_dir
        
        # 状态文件存储在运行目录的隐藏文件夹中
        self._state_dir = self._get_state_dir()
        self._state_file = os.path.join(self._state_dir, 'download_state.json')
        
        self.tasks: Dict[str, DownloadTask] = {}
        self._lock = threading.Lock()
        self._load_state()

    def _get_state_dir(self) -> str:
        """
        获取状态存储目录（运行目录下的隐藏文件夹）
        优先级：当前工作目录 > 用户目录
        """
        candidates = [
            os.path.join(os.getcwd(), '.jmcomic_state'),
            os.path.join(os.path.expanduser('~'), '.jmcomic_state'),
        ]
        
        for path in candidates:
            try:
                os.makedirs(path, exist_ok=True)
                # 测试写入权限
                test_file = os.path.join(path, '.write_test')
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                return path
            except (PermissionError, OSError):
                continue
        
        # 最后的备选
        fallback = os.path.join(os.getcwd(), '.jmcomic_state')
        os.makedirs(fallback, exist_ok=True)
        return fallback

    # ===== 任务管理 =====

    def list_tasks(self) -> Dict[str, Any]:
        """获取所有任务列表"""
        with self._lock:
            tasks = []
            for task in self.tasks.values():
                tasks.append(self._task_to_dict(task))
            # 按优先级和创建时间排序
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
        
        # 使用数字 ID 作为文件夹名
        if download_dir is None:
            download_dir = os.path.join(self.manga_dir, album_id)

        task = DownloadTask(
            id=task_id,
            album_id=album_id,
            download_dir=download_dir,
            concurrency=concurrency,
            priority=priority,
            status='queued' if auto_start else 'paused',
            start_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )

        with self._lock:
            self.tasks[task_id] = task
            self._save_state()

        # 如果自动开始，启动下载线程
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
                self._save_state()
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
                self._save_state()
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
                self._save_state()
                self._start_download(task_id)
                return {'success': True}
            
            return {'success': False, 'error': f'任务状态为 {task.status}，无法重试'}

    def delete_task(self, task_id: str) -> Dict[str, Any]:
        """删除任务"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}
            
            # 如果正在下载，先停止
            if task.status == 'downloading':
                task._stop_event.set()
            
            del self.tasks[task_id]
            self._save_state()
            return {'success': True}

    def start_all(self) -> Dict[str, Any]:
        """全部开始"""
        with self._lock:
            for task_id, task in self.tasks.items():
                if task.status == 'paused':
                    task._stop_event.clear()
                    task.status = 'queued'
                    self._start_download(task_id)
            self._save_state()
        return {'success': True}

    def pause_all(self) -> Dict[str, Any]:
        """全部暂停"""
        with self._lock:
            for task in self.tasks.values():
                if task.status == 'downloading':
                    task._stop_event.set()
                    task.status = 'paused'
            self._save_state()
        return {'success': True}

    def clear_completed(self) -> Dict[str, Any]:
        """清除所有已完成任务"""
        with self._lock:
            to_delete = [tid for tid, t in self.tasks.items() if t.status == 'completed']
            for tid in to_delete:
                del self.tasks[tid]
            self._save_state()
        return {'success': True, 'deleted': len(to_delete)}

    def get_detail(self, task_id: str) -> Dict[str, Any]:
        """获取任务详情"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return {'error': '任务不存在'}
            return self._task_to_dict(task, detail=True)

    # ===== 下载核心逻辑 =====

    def _start_download(self, task_id: str):
        """启动下载线程"""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            
            if task._thread and task._thread.is_alive():
                return  # 已经在下载中
            
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
            self._save_state()

        try:
            # 调用 jmcomic 库进行实际下载
            album_info = self._execute_download(task)
            
            with self._lock:
                task = self.tasks.get(task_id)
                if task and task._stop_event.is_set():
                    return  # 被暂停了
                if task:
                    task.status = 'completed'
                    task.complete_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self._save_state()
                    
                    # 下载完成后，保存 album_info.json
                    if album_info:
                        self._save_album_info(task, album_info)

        except Exception as e:
            logger.error(f'下载任务 {task_id} 失败: {e}')
            with self._lock:
                task = self.tasks.get(task_id)
                if task:
                    task.status = 'failed'
                    task.error = str(e)
                    self._save_state()

    def _execute_download(self, task: DownloadTask) -> Optional[Dict]:
        """执行实际的下载逻辑，返回漫画信息"""
        import jmcomic
        from jmcomic import JmModuleConfig, JmcomicText

        # 确保下载目录使用数字 ID
        download_dir = os.path.join(self.manga_dir, task.album_id)
        os.makedirs(download_dir, exist_ok=True)

        # 创建自定义的进度回调
        class ProgressCallback(jmcomic.DownloadCallback):
            def __init__(self, task_ref, lock_ref, state_dir):
                self.task = task_ref
                self.lock = lock_ref
                self.state_dir = state_dir
                self.last_update = time.time()
                self.last_count = 0
                self.album_info = None

            def before_album(self, album):
                with self.lock:
                    self.task.title = album.name
                    self.task.total_images = album.page_count
                    # 获取缩略图 URL
                    if hasattr(album, 'album_id'):
                        self.task.thumb_url = JmcomicText.get_album_cover_url(album.album_id)
                    
                    # 保存 album_info 到下载目录
                    self.album_info = self._build_album_info(album)
                    self._save_album_info_local()
                    self._save_state_locked()

            def after_image(self, image, img_save_path):
                with self.lock:
                    self.task.completed_images += 1
                    
                    # 计算速度（每5张图更新一次）
                    now = time.time()
                    if self.task.completed_images - self.last_count >= 5:
                        elapsed = now - self.last_update
                        if elapsed > 0:
                            # 估算每张图平均大小 500KB
                            bytes_downloaded = (self.task.completed_images - self.last_count) * 500 * 1024
                            self.task.speed = bytes_downloaded / elapsed
                            
                            # 估算剩余时间
                            remaining = self.task.total_images - self.task.completed_images
                            if self.task.speed > 0:
                                self.task.eta = int((remaining * 500 * 1024) / self.task.speed)
                            
                            self.last_update = now
                            self.last_count = self.task.completed_images
                        
                        self._save_state_locked()

            def _build_album_info(self, album) -> Dict:
                """构建漫画信息字典"""
                # 提取章节信息
                chapters = []
                for chap in album:
                    chapters.append({
                        'chapter_id': getattr(chap, 'photo_id', ''),
                        'title': getattr(chap, 'name', ''),
                        'page_count': len(chap) if hasattr(chap, 'page_arr') and chap.page_arr else 0
                    })

                return {
                    "oname": getattr(album, 'oname', 'unknown'),
                    "album_id": str(getattr(album, 'album_id', 'unknown')),
                    "actors": getattr(album, 'actors', []),
                    "title": getattr(album, 'name', '未知主标题'),
                    "author": getattr(album, 'author', '未知作者'),
                    "tags": list(getattr(album, 'tags', [])),
                    "chapter_count": len(album),
                    "total_page_count": getattr(album, 'page_count', 0),
                    "chapters": chapters,
                    "download_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }

            def _save_album_info_local(self):
                """保存 album_info.json 到下载目录"""
                if not self.album_info:
                    return
                try:
                    json_path = os.path.join(download_dir, "album_info.json")
                    with open(json_path, 'w', encoding='utf-8') as f:
                        json.dump(self.album_info, f, ensure_ascii=False, indent=2, default=str)
                except Exception as e:
                    logger.error(f'保存 album_info.json 失败: {e}')

            def _save_state_locked(self):
                """保存进度到状态目录（不是下载目录）"""
                try:
                    progress_file = os.path.join(self.state_dir, f'progress_{self.task.id}.json')
                    state = {
                        'task_id': self.task.id,
                        'album_id': self.task.album_id,
                        'completed_images': self.task.completed_images,
                        'total_images': self.task.total_images,
                        'speed': self.task.speed,
                        'eta': self.task.eta,
                        'status': self.task.status
                    }
                    with open(progress_file, 'w') as f:
                        json.dump(state, f)
                except Exception as e:
                    logger.error(f'保存进度状态失败: {e}')

        # 配置下载选项 - 使用数字 ID 作为文件夹名
        option_dict = {
            "dir_rule": {
                "base_dir": self.manga_dir,
                "rule": "Bd / Aid"  # 使用 Aid（album_id）作为文件夹名
            },
            "download": {
                "cache": True,
                "image": {
                    "decode": True,
                    "suffix": ".jpg"
                },
                "threading": {
                    "image": task.concurrency,
                    "photo": 4
                }
            },
            "client": {
                "impl": "api",
                "retry_times": 3,
                "postman": {
                    "type": "curl_cffi",
                    "meta_data": {
                        "impersonate": "chrome",
                        "proxies": None
                    }
                }
            }
        }

        option = jmcomic.JmOption.construct(option_dict)
        
        # 创建进度回调
        progress = ProgressCallback(task, self._lock, self._state_dir)
        
        # 执行下载
        with jmcomic.new_downloader(option) as downloader:
            # 注入进度回调
            downloader.before_album = progress.before_album
            downloader.after_image = progress.after_image
            
            # 检查是否被暂停
            if task._stop_event.is_set():
                return None
            
            album = downloader.download_album(task.album_id)
            
            # 返回漫画信息
            return progress.album_info

    def _save_album_info(self, task: DownloadTask, album_info: Dict):
        """下载完成后保存 album_info.json"""
        try:
            # 确保使用数字 ID 作为文件夹名
            download_dir = os.path.join(self.manga_dir, task.album_id)
            json_path = os.path.join(download_dir, "album_info.json")
            
            # 更新下载时间
            album_info['download_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            album_info['total_page_count'] = task.completed_images
            
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(album_info, f, ensure_ascii=False, indent=2, default=str)
            
            logger.info(f"漫画信息已保存到: {json_path}")
            
        except Exception as e:
            logger.error(f"保存 album_info.json 失败: {e}", exc_info=True)

    # ===== 状态持久化 =====

    def _save_state(self):
        """保存任务状态到隐藏目录"""
        try:
            state = {}
            for tid, task in self.tasks.items():
                state[tid] = {
                    'id': task.id,
                    'album_id': task.album_id,
                    'title': task.title,
                    'thumb_url': task.thumb_url,
                    'status': task.status,
                    'total_images': task.total_images,
                    'completed_images': task.completed_images,
                    'priority': task.priority,
                    'download_dir': task.download_dir,
                    'error': task.error,
                    'start_time': task.start_time,
                    'complete_time': task.complete_time,
                    'concurrency': task.concurrency
                }
            
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            logger.error(f'保存下载状态失败: {e}')

    def _load_state(self):
        """从隐藏目录加载任务状态"""
        try:
            if not os.path.exists(self._state_file):
                return
            
            with open(self._state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
            
            for tid, data in state.items():
                task = DownloadTask(
                    id=data['id'],
                    album_id=data['album_id'],
                    title=data.get('title', ''),
                    thumb_url=data.get('thumb_url', ''),
                    status=data.get('status', 'paused'),  # 恢复后默认为暂停
                    total_images=data.get('total_images', 0),
                    completed_images=data.get('completed_images', 0),
                    priority=data.get('priority', 'normal'),
                    download_dir=data.get('download_dir', ''),
                    error=data.get('error', ''),
                    start_time=data.get('start_time', ''),
                    complete_time=data.get('complete_time', ''),
                    concurrency=data.get('concurrency', 3)
                )
                # 如果之前是下载中，恢复为暂停状态
                if task.status == 'downloading':
                    task.status = 'paused'
                self.tasks[tid] = task
                
        except Exception as e:
            logger.error(f'加载下载状态失败: {e}')

    def _task_to_dict(self, task: DownloadTask, detail: bool = False) -> Dict:
        """将任务对象转为字典"""
        result = {
            'id': task.id,
            'albumId': task.album_id,
            'title': task.title,
            'thumbUrl': task.thumb_url,
            'status': task.status,
            'totalImages': task.total_images,
            'completedImages': task.completed_images,
            'speed': task.speed,
            'eta': task.eta,
            'priority': task.priority,
            'downloadDir': task.download_dir,
            'error': task.error,
            'startTime': task.start_time,
            'completeTime': task.complete_time,
        }
        
        if detail:
            result['chapters'] = task.chapters
            result['concurrency'] = task.concurrency
            
        return result