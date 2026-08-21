import os
import json
import uuid
import threading
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling



_scanner = load_sibling(__file__, 'scanner', 'manga_library')
find_cover = _scanner.find_cover
list_pages = _scanner.list_pages
natural_sorted = _scanner.natural_sorted
resolve_safe_path = _scanner.resolve_safe_path
scan_manga = _scanner.scan_manga

_dmodels = load_sibling(__file__, 'download_models', 'manga_library_download')
_dstate = load_sibling(__file__, 'download_state', 'manga_library_download')
_downloader = load_sibling(__file__, 'downloader', 'manga_library_download')

DownloadTask = _dmodels.DownloadTask
load_tasks = _dstate.load_tasks
save_tasks = _dstate.save_tasks
execute_download = _downloader.execute_download

logger = logging.getLogger(__name__)

class MangaLibraryPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "漫画根目录", "type": "text",
         "placeholder": "默认: ./data", "help": "存放漫画文件夹的根目录"},
        {"key": "recent_count", "label": "最近阅读显示数量", "type": "number",
         "default": 10, "min": 1, "max": 50, "help": "首页「最近阅读」展示的漫画数量"},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir') or str(super().get_data_root())
        self.recent_count = int(self.setting('recent_count', 10))
        self.manga_dir = Path(root).resolve()
        self.cover_dir = self.manga_dir / '.cache' / 'covers'
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.manga_dir / '.cache' / 'manga_state.json'
        self._cache = None
        self._state = self._load_state()

        # 下载中心（与漫画库共用根目录）
        self._download_lock = threading.Lock()
        self._download_state_dir = self._get_download_state_dir()
        self._download_state_file = os.path.join(self._download_state_dir, 'download_state.json')
        self.download_tasks: Dict[str, DownloadTask] = load_tasks(self._download_state_file, DownloadTask, logger)

    # ===== 文件服务根目录 =====

    def get_data_root(self) -> Path:
        return self.manga_dir

    # ===== 设置持久化 =====



    def get_settings(self) -> Dict:
        return {
            "root_dir": str(self.manga_dir),
            "recent_count": self.recent_count,
        }

    def on_settings_changed(self, changed_keys):
        if 'root_dir' in changed_keys:
            new_dir = self.setting('root_dir')
            if new_dir and Path(new_dir).is_dir():
                self._apply_root_dir(Path(new_dir).resolve())
        if 'recent_count' in changed_keys:     
            count = self.setting('recent_count', 10)  
            try:
                if count == None:
                    print("[MangaLibrary] recent_count is None")
                    raise ValueError
                self.recent_count = max(1, min(50, int(count)))
            except (ValueError, TypeError):
                pass

    def _apply_root_dir(self, new_dir: Path):
        self.manga_dir = new_dir
        self.cover_dir = self.manga_dir / '.cache' / 'covers'
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.manga_dir / '.cache' / 'manga_state.json'
        self._state = self._load_state()
        self._cache = None
        self._download_state_dir = self._get_download_state_dir()
        self._download_state_file = os.path.join(self._download_state_dir, 'download_state.json')
        self.download_tasks = load_tasks(self._download_state_file, DownloadTask, logger)
        self._download_lock = threading.Lock()
        self._download_state_dir = self._get_download_state_dir()
        self._download_state_file = os.path.join(self._download_state_dir, 'download_state.json')
        self.download_tasks: Dict[str, DownloadTask] = load_tasks(self._download_state_file, DownloadTask, logger)

    # ===== API 注册 =====

    def register_api(self) -> dict:
        return {
            'manga_list': self.list_manga,
            'manga_search': self.search,
            'manga_get_state': self.get_state,
            'manga_toggle_favorite': self.toggle_favorite,
            'manga_update_recent': self.update_recent,
            'manga_get_detail': self.get_detail,
            'manga_get_pages': self.get_pages,
            'download_submit': self.download_submit,
            'download_list': self.download_list,
            'download_pause': self.download_pause,
            'download_resume': self.download_resume,
            'download_retry': self.download_retry,
            'download_delete': self.download_delete,
            'download_start_all': self.download_start_all,
            'download_pause_all': self.download_pause_all,
            'download_clear_completed': self.download_clear_completed,
            'download_get_album_info': self.download_get_album_info,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    # ===== 核心业务（由旧版 MangaModule 迁移） =====

    def _load_state(self) -> Dict:
        defaults = {"favorites": [], "recent": []}
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    favorites = data.get("favorites", [])
                    recent = data.get("recent", [])
                    return {
                        "favorites": favorites if isinstance(favorites, list) else [],
                        "recent": recent if isinstance(recent, list) else [],
                    }
            except Exception:
                pass
        return defaults

    def _save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    def _scan_manga(self):
        if self._cache is None:
            self._cache = scan_manga(self.manga_dir, self._state['favorites'])
        return self._cache


    def list_manga(self) -> List[Dict]:
        self._cache = None
        return self._scan_manga()

    def search(self, keyword: str) -> List[Dict]:
        if not keyword:
            return self.list_manga()
        kw = keyword.lower()
        return [m for m in self.list_manga() if kw in m['title'].lower() or kw in m['author'].lower()]

    def get_state(self) -> Dict:
        recent_ids = [r['id'] for r in self._state['recent']]
        fav_ids = self._state['favorites']
        all_manga = {m['folder_name']: m for m in self.list_manga()}
        return {
            "recent": [all_manga[rid] for rid in recent_ids if rid in all_manga][:self.recent_count],
            "favorites": [all_manga[fid] for fid in fav_ids if fid in all_manga]
        }

    def toggle_favorite(self, folder_name: str) -> bool:
        favs = self._state['favorites']
        if folder_name in favs:
            favs.remove(folder_name)
            is_fav = False
        else:
            favs.append(folder_name)
            is_fav = True
        self._save_state()
        self._cache = None
        return is_fav

    def update_recent(self, folder_name: str, page: int = 0) -> Dict:
        recent = self._state['recent']
        recent = [r for r in recent if r['id'] != folder_name]
        recent.insert(0, {"id": folder_name, "page": page, "time": datetime.now().isoformat()})
        self._state['recent'] = recent[:20]
        self._save_state()
        return {"status": "ok"}

    def get_detail(self, folder_name: str) -> Dict:
        folder_path = resolve_safe_path(self.manga_dir, folder_name)
        if folder_path is None or not folder_path.exists():
            return {}

        info_path = folder_path / 'album_info.json'
        info = {}
        if info_path.exists():
            try:
                with open(info_path, 'r', encoding='utf-8') as f:
                    info = json.load(f)
            except Exception:
                pass

        sub_dirs = [d for d in folder_path.iterdir()
                    if d.is_dir() and not d.name.startswith('.') and d.name != 'ai']
        is_multi_chapter = len(sub_dirs) > 0

        chapters = []
        if is_multi_chapter:
            for d in natural_sorted(sub_dirs):
                chapters.append({
                    "name": d.name,
                    "cover_url": find_cover(d, self.manga_dir),
                    "path": d.name
                })

        return {
            "folder_name": folder_name,
            "title": info.get("title", folder_name),
            "author": info.get("author", "未知"),
            "is_fav": folder_name in self._state['favorites'],
            "is_multi_chapter": is_multi_chapter,
            "info": info,
            "chapters": chapters
        }

    def get_pages(self, folder_name: str, chapter_path: str = "") -> List[str]:
        return list_pages(self.manga_dir, folder_name, chapter_path)


    # ===== 下载中心（与漫画库合并） =====

    def _get_download_state_dir(self) -> str:
        """下载状态存储目录（与旧 download-center 完全一致，任务可无缝继承）。"""
        path = os.path.join(str(self.manga_dir), '.jmcomic_state')
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except (PermissionError, OSError):
            fallback = os.path.join(os.path.expanduser('~'), '.jmcomic_state')
            os.makedirs(fallback, exist_ok=True)
            return fallback

    def download_list(self) -> Dict:
        """下载任务列表摘要。"""
        with self._download_lock:
            tasks = [t.to_api_dict() for t in self.download_tasks.values()]
        priority_order = {'high': 0, 'normal': 1, 'low': 2}
        tasks.sort(key=lambda t: (
            priority_order.get(t.get('priority'), 1),
            t.get('startTime') or ''
        ))
        return {'tasks': tasks}

    def download_submit(self, album_id: str, concurrency: int = 3,
                        priority: str = 'normal', auto_start: bool = True) -> Dict:
        task_id = str(uuid.uuid4())
        task = DownloadTask(
            id=task_id,
            album_id=album_id,
            download_dir=os.path.join(str(self.manga_dir), album_id),
            concurrency=max(1, min(10, int(concurrency))),
            priority=priority if priority in ('high', 'normal', 'low') else 'normal',
            status='queued' if auto_start else 'paused',
            start_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )
        with self._download_lock:
            self.download_tasks[task_id] = task
            save_tasks(self.download_tasks, self._download_state_file, logger)
        if auto_start:
            self._download_start(task_id)
        return {'taskId': task_id, 'success': True}

    def download_pause(self, task_id: str) -> Dict:
        with self._download_lock:
            task = self.download_tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}
            if task.status == 'downloading':
                task._stop_event.set()
                task.status = 'paused'
                save_tasks(self.download_tasks, self._download_state_file, logger)
                return {'success': True}
            return {'success': False, 'error': f'任务状态为 {task.status}，无法暂停'}

    def download_resume(self, task_id: str) -> Dict:
        with self._download_lock:
            task = self.download_tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}
            if task.status == 'paused':
                task._stop_event.clear()
                task.status = 'queued'
                save_tasks(self.download_tasks, self._download_state_file, logger)
                self._download_start(task_id)
                return {'success': True}
            return {'success': False, 'error': f'任务状态为 {task.status}，无法恢复'}

    def download_retry(self, task_id: str) -> Dict:
        with self._download_lock:
            task = self.download_tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}
            if task.status == 'failed':
                task.status = 'queued'
                task.error = ''
                task.completed_images = 0
                task._stop_event.clear()
                save_tasks(self.download_tasks, self._download_state_file, logger)
                self._download_start(task_id)
                return {'success': True}
            return {'success': False, 'error': f'任务状态为 {task.status}，无法重试'}

    def download_delete(self, task_id: str) -> Dict:
        with self._download_lock:
            task = self.download_tasks.get(task_id)
            if not task:
                return {'success': False, 'error': '任务不存在'}
            if task.status == 'downloading':
                task._stop_event.set()
            del self.download_tasks[task_id]
            save_tasks(self.download_tasks, self._download_state_file, logger)
            return {'success': True}

    def download_start_all(self) -> Dict:
        with self._download_lock:
            for task_id, task in self.download_tasks.items():
                if task.status == 'paused':
                    task._stop_event.clear()
                    task.status = 'queued'
                    self._download_start(task_id)
            save_tasks(self.download_tasks, self._download_state_file, logger)
        return {'success': True}

    def download_pause_all(self) -> Dict:
        with self._download_lock:
            for task in self.download_tasks.values():
                if task.status == 'downloading':
                    task._stop_event.set()
                    task.status = 'paused'
            save_tasks(self.download_tasks, self._download_state_file, logger)
        return {'success': True}

    def download_clear_completed(self) -> Dict:
        with self._download_lock:
            to_delete = [tid for tid, t in self.download_tasks.items() if t.status == 'completed']
            for tid in to_delete:
                del self.download_tasks[tid]
            save_tasks(self.download_tasks, self._download_state_file, logger)
        return {'success': True, 'deleted': len(to_delete)}

    def download_get_album_info(self, album_id: str) -> Dict:
        """读取已下载漫画的 album_info.json。"""
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

    def _download_start(self, task_id: str):
        with self._download_lock:
            task = self.download_tasks.get(task_id)
            if not task or (task._thread and task._thread.is_alive()):
                return
            task._thread = threading.Thread(
                target=self._download_worker, args=(task_id,), daemon=True)
            task._thread.start()

    def _download_worker(self, task_id: str):
        with self._download_lock:
            task = self.download_tasks.get(task_id)
            if not task:
                return
            task.status = 'downloading'
            save_tasks(self.download_tasks, self._download_state_file, logger)

        try:
            album_info = execute_download(
                task, str(self.manga_dir), self._download_state_dir,
                self._download_lock, logger)
            with self._download_lock:
                task = self.download_tasks.get(task_id)
                if task and task._stop_event.is_set():
                    return
                if task:
                    task.status = 'completed'
                    task.complete_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    save_tasks(self.download_tasks, self._download_state_file, logger)
                    if album_info:
                        self._save_album_info(task, album_info)
        except Exception as e:
            logger.error(f'下载任务 {task_id} 失败: {e}')
            with self._download_lock:
                task = self.download_tasks.get(task_id)
                if task:
                    task.status = 'failed'
                    task.error = str(e)
                    save_tasks(self.download_tasks, self._download_state_file, logger)

    def _save_album_info(self, task: DownloadTask, album_info: Dict):
        try:
            download_dir = task.download_dir or os.path.join(str(self.manga_dir), task.album_id)
            json_path = os.path.join(download_dir, "album_info.json")
            album_info['download_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            album_info['total_page_count'] = task.completed_images
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(album_info, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.error(f"保存 album_info.json 失败: {e}", exc_info=True)
