import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from shell.backend.freshness import engine_for
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_scanner = load_sibling(__file__, 'scanner', 'manga_library')
find_cover = _scanner.find_cover
list_pages = _scanner.list_pages
natural_sorted = _scanner.natural_sorted
resolve_safe_path = _scanner.resolve_safe_path
scan_manga = _scanner.scan_manga
visible_subdirs = _scanner.visible_subdirs

_dmodels = load_sibling(__file__, 'download_models', 'manga_library_download')
_dstate = load_sibling(__file__, 'download_state', 'manga_library_download')
_downloader = load_sibling(__file__, 'downloader', 'manga_library_download')

DownloadTask = _dmodels.DownloadTask
load_tasks = _dstate.load_tasks
save_tasks = _dstate.save_tasks
execute_download = _downloader.execute_download

logger = logging.getLogger(__name__)

class MangaLibraryPlugin(PluginBase):
    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        {"key": "root_dir", "label": "漫画根目录", "type": "directory",
         "admin_only": True,
         "placeholder": "输入目录绝对路径，如 D:\\漫画",
         "emptyText": "未添加任何目录，将使用默认数据目录（./data）",
         "help": "存放漫画文件夹的根目录；第一行即生效根目录，保存后生效。"
                 "改动需管理员（它决定 /file 的允许根）"},
        {"key": "recent_count", "label": "最近阅读显示数量", "type": "number",
         "default": 10, "min": 1, "max": 50, "help": "首页「最近阅读」展示的漫画数量"},
    ]

    # 书架派生数据的规则版本：收录哪些顶层目录、封面怎么挑变了就 +1，
    # 由统一刷新基建整体作废（不再依赖"记得手动清缓存"）
    SHELF_VERSION: ClassVar[int] = 1

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir') or str(super().get_data_root())
        self.recent_count = int(self.setting('recent_count', 10))
        self.manga_dir = Path(root).resolve()
        self.state_file = self.manga_dir / '.cache' / 'manga_state.json'
        # 书架列表缓存：由统一刷新基建驱动失效（见 freshness 一节）。
        # 旧实现叫 `_cache`，但唯一调用点在它之前刚把它置 None —— 死缓存，
        # 每次 `manga_list` 都全量重扫一遍。
        self._shelf: Optional[List[Dict]] = None
        self._shelf_dirty = True
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
                if count is None:
                    log.info("[MangaLibrary] recent_count is None")
                    raise ValueError
                self.recent_count = max(1, min(50, int(count)))
            except (ValueError, TypeError):
                pass

    def _apply_root_dir(self, new_dir: Path):
        self.manga_dir = new_dir
        self.state_file = self.manga_dir / '.cache' / 'manga_state.json'
        self._state = self._load_state()
        self._reset_shelf()
        self._download_state_dir = self._get_download_state_dir()
        self._download_state_file = os.path.join(self._download_state_dir, 'download_state.json')
        self.download_tasks = load_tasks(self._download_state_file, DownloadTask, logger)
        self._download_lock = threading.Lock()
        engine = engine_for(self)
        if engine is not None:
            # 换根后清掉去抖，让下一次进书架的被动同步立刻按新根重扫
            engine.reset_throttle()

    def _reset_shelf(self):
        """书架缓存作废（磁盘变化、收藏变更、换根时调用）。"""
        self._shelf = None
        self._shelf_dirty = True

    # ===== 统一刷新基建（同步 / 校验）=====
    #
    # 书架卡片 = 顶层漫画目录的派生数据（含页数、封面、"里面有图片吗"的两次判断）。
    # 旧实现没有可用缓存，`manga_get_state` + `manga_list` 各扫一遍全树，切一次视图
    # 就是两遍。接入基建后：目录级短路判定"没变"时连 derive 都不会被调用，
    # 列表只在磁盘真变时重建。

    def freshness_spec(self) -> Dict[str, Any]:
        return {
            'roots': lambda: [self.manga_dir],
            'include': tuple(sorted(_scanner.IMAGE_EXTS)),
            'derive': self._freshness_derive,
            'prune': self._freshness_prune,
            'on_verified': self._freshness_audit,
            'rebuild': self._freshness_rebuild,
            # 扫描规则（收录哪些目录、封面怎么挑）变更时改它：整体作废书架缓存
            'content_version': self.SHELF_VERSION,
            'unit': '部',
            'min_sync_interval': 5.0,
            # 顶层目录数是几十到几百：逐条 stat 换"加了一页立刻反映"很划算
            'stat_entries': True,
        }

    def _freshness_derive(self, items) -> Dict[str, Any]:
        """条目新增/指纹变化：作废受影响漫画的书架卡片。"""
        folders = {str(it.get('key') or '').split('/', 1)[0] for it in items}
        folders.discard('')
        if not folders:
            return {'count': 0}
        self._reset_shelf()
        return {'count': len(items), 'folders': len(folders)}

    def _freshness_prune(self, keys) -> int:
        """条目消失：整本漫画的图片都没了（或删了几页）→ 书架重建。"""
        if not keys:
            return 0
        self._reset_shelf()
        return len(keys)

    def _freshness_audit(self, keys) -> Dict[str, Any]:
        """全量校验后的对账：书架只保留磁盘上还有图片的漫画。"""
        valid_folders = {str(k).split('/', 1)[0] for k in keys}
        shelf = self._scan_manga()
        stale = [m['folder_name'] for m in shelf if m['folder_name'] not in valid_folders]
        if stale:
            log.info(f'[MangaLibrary] 校验清理已消失的漫画 {len(stale)} 部')
            self._shelf = [m for m in shelf if m['folder_name'] in valid_folders]
            self._shelf_dirty = False
        return {'dropped_books': len(stale), 'valid_folders': len(valid_folders)}

    def _freshness_rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃书架缓存并重建（不动收藏与最近阅读）。"""
        self._reset_shelf()
        self._scan_manga()
        return {'kind': kind, 'books': len(self._shelf or [])}

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

    def _scan_manga(self) -> List[Dict]:
        """书架列表（真正的缓存：只在"磁盘确实变了"时重建）。

        失效由统一刷新基建驱动 —— 目录级短路判定"没变"时连 `derive` 都不会被调用，
        所以切视图、切分栏、`manga_get_state` + `manga_list` 各调一次，全都只读内存。
        """
        if self._shelf is None or self._shelf_dirty:
            self._shelf = scan_manga(self.manga_dir, self._state['favorites'])
            self._shelf_dirty = False
        return self._shelf

    def list_manga(self) -> List[Dict]:
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
        """切换收藏。

        `folder_name` 会被写进 `manga_state.json`，因此必须是**根内的单个目录名**：
        它不参与路径拼接（危害限于脏数据），但放进来 `../` 或绝对路径只会让
        "最近阅读/收藏"里出现永远匹配不到书架的条目。统一在入口挡掉。
        """
        if not self._is_valid_folder_name(folder_name):
            return False
        favs = self._state['favorites']
        if folder_name in favs:
            favs.remove(folder_name)
            is_fav = False
        else:
            favs.append(folder_name)
            is_fav = True
        self._save_state()
        # 书架卡片带 is_fav：收藏变了必须重建列表（缓存由基建之外的因素失效）
        self._reset_shelf()
        return is_fav

    def _is_valid_folder_name(self, folder_name: str) -> bool:
        """合法的漫画目录名：非空、不含路径分隔符、不是 `.` / `..`。"""
        name = str(folder_name or '').strip()
        if not name or name in ('.', '..'):
            return False
        return not any(sep in name for sep in ('/', '\\'))

    def update_recent(self, folder_name: str, page: int = 0) -> Dict:
        if not self._is_valid_folder_name(folder_name):
            return {"status": "error", "error": "非法目录名"}
        try:
            page = max(0, int(page))
        except (TypeError, ValueError):
            page = 0
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

        sub_dirs = visible_subdirs(folder_path)
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
        # album_id 直接用 os.path.join 拼下载目录，必须是纯数字专辑号：
        # 传 "../../../Users/Public/x" 会让 os.path.join 折叠越界，
        # 随后 os.makedirs(exist_ok=True) 就会在漫画根目录之外建目录并写入图片。
        album_id = str(album_id).strip()
        if not album_id.isdigit():
            return {'success': False, 'error': f'非法的专辑号: {album_id!r}（必须是纯数字）'}

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
            # 早先漫画的 page_count 恒为 0（api 客户端），只能拿"已完成数"充总数；
            # 现在 downloader 按章节累加出了真值，优先写它，避免续传时把总数写小。
            album_info['total_page_count'] = task.total_images or task.completed_images
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(album_info, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.error(f"保存 album_info.json 失败: {e}", exc_info=True)
