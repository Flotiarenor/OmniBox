"""jmcomic 下载执行器。

从 Plugin 主类中拆出，避免 main.py 同时承担任务管理、状态持久化和下载细节。
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, Optional


def execute_download(task, manga_dir: str, state_dir: str, lock, logger=None) -> Optional[Dict]:
    """执行实际下载，返回 album_info；被停止时返回 None。"""
    import jmcomic
    from jmcomic import JmcomicText

    download_dir = task.download_dir or os.path.join(manga_dir, task.album_id)
    os.makedirs(download_dir, exist_ok=True)

    class ProgressCallback(jmcomic.DownloadCallback):
        def __init__(self, task_ref, lock_ref, state_dir, dl_dir):
            self.task = task_ref
            self.lock = lock_ref
            self.state_dir = state_dir
            self.dl_dir = dl_dir
            self.last_update = time.time()
            self.last_count = 0
            self.album_info = None

        def before_album(self, album):
            with self.lock:
                self.task.title = album.name
                self.task.total_images = album.page_count
                if hasattr(album, 'album_id'):
                    self.task.thumb_url = JmcomicText.get_album_cover_url(album.album_id)

                self.album_info = self._build_album_info(album)
                self._save_album_info_local()
                self._save_state_locked()

        def after_image(self, image, img_save_path):
            with self.lock:
                self.task.completed_images += 1

                now = time.time()
                if self.task.completed_images - self.last_count >= 5:
                    elapsed = now - self.last_update
                    if elapsed > 0:
                        bytes_downloaded = (self.task.completed_images - self.last_count) * 500 * 1024
                        self.task.speed = bytes_downloaded / elapsed

                        remaining = self.task.total_images - self.task.completed_images
                        if self.task.speed > 0:
                            self.task.eta = int((remaining * 500 * 1024) / self.task.speed)

                        self.last_update = now
                        self.last_count = self.task.completed_images

                    self._save_state_locked()

        def _build_album_info(self, album) -> Dict:
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
            if not self.album_info:
                return
            try:
                json_path = os.path.join(self.dl_dir, "album_info.json")
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(self.album_info, f, ensure_ascii=False, indent=2, default=str)
            except Exception as exc:
                if logger:
                    logger.error(f'保存 album_info.json 失败: {exc}')

        def _save_state_locked(self):
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
                with open(progress_file, 'w', encoding='utf-8') as f:
                    json.dump(state, f, ensure_ascii=False)
            except Exception as exc:
                if logger:
                    logger.error(f'保存进度状态失败: {exc}')

    option_dict = {
        "dir_rule": {
            "base_dir": manga_dir,
            "rule": "Bd / Aid"
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
                "type": "requests",
                "meta_data": {
                    "headers": None,
                    "proxies": None
                }
            }
        }
    }

    option = jmcomic.JmOption.construct(option_dict)
    progress = ProgressCallback(task, lock, state_dir, download_dir)

    with jmcomic.new_downloader(option) as downloader:
        downloader.before_album = progress.before_album
        downloader.after_image = progress.after_image

        if task._stop_event.is_set():
            return None

        album = downloader.download_album(task.album_id)
        return progress.album_info
