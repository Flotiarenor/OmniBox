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
    # 纵深防御：即使调用方没校验 album_id，也绝不允许下载目录落在漫画根目录之外
    # （os.path.join 遇到绝对路径/`..` 会折叠出根目录，makedirs 就会在外部建目录）。
    root = os.path.realpath(manga_dir)
    target = os.path.realpath(download_dir)
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f'下载目录越界: {download_dir!r} 不在漫画根目录 {manga_dir!r} 内')
    os.makedirs(download_dir, exist_ok=True)

    class ProgressCallback:
        """进度回调：把 jmcomic 的通知写进任务对象（由下面的 ProgressDownloader 调用）。"""

        def __init__(self, task_ref, lock_ref, state_dir, dl_dir):
            self.task = task_ref
            self.lock = lock_ref
            self.state_dir = state_dir
            self.dl_dir = dl_dir
            self.last_update = time.time()
            self.last_count = 0
            self.album_info = None
            self.album_page_count = 0  # 漫画声明的总页数（api 客户端恒为 0）
            self.photo_total = 0       # 各章节 len(photo) 的累加值

        def before_album(self, album):
            with self.lock:
                self.task.title = album.name
                # 总页数不能只信漫画：api 客户端的漫画数据里根本没有这个字段，
                # jmcomic 的 JmApiAdaptTool.post_adapt_album 会把它写死成 '0'，
                # 于是页数永远显示 "?"。真实页数只能按章节累加（见 before_photo）；
                # 这里先归零，续传/重试重跑漫画时不会把上一轮的累加值再算一遍。
                self.album_page_count = int(getattr(album, 'page_count', 0) or 0)
                self.photo_total = 0
                self.task.total_images = self.album_page_count
                if hasattr(album, 'album_id'):
                    self.task.thumb_url = JmcomicText.get_album_cover_url(album.album_id)

                self.album_info = self._build_album_info(album)
                self._save_album_info_local()
                self._save_state_locked()

        def before_photo(self, photo):
            # 章节详情到手时才知道图片数（jmcomic 日志里的"图片数为[N]"就是这个值，
            # 来自 API 的 images 字段）。漫画维度为 0 时以累加值为准；两者都有效时
            # 取大值，多章漫画不会因为两处来源相加而翻倍。
            with self.lock:
                self.photo_total += len(photo)
                self.task.total_images = max(self.album_page_count, self.photo_total)

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
                "type": "curl_cffi",
                "meta_data": {
                    "impersonate": "chrome",
                    "proxies": None
                }
            }
        }
    }

    class ProgressDownloader(jmcomic.JmDownloader):
        """挂钩子用子类，而不是往实例上赋属性。

        JmDownloader 的同名方法除了通知回调，还要维护自己的簿记（download_success_dict
        与 plugin / feature 回调链）；用 `downloader.before_album = ...` 覆盖实例属性会
        把这些静默跳过。子类里 super() 一次，两边都不丢。
        """

        def __init__(self, jm_option, progress_ref):
            super().__init__(jm_option)
            self.progress = progress_ref

        def before_album(self, album):
            super().before_album(album)
            self.progress.before_album(album)

        def before_photo(self, photo):
            super().before_photo(photo)
            self.progress.before_photo(photo)

        def after_image(self, image, img_save_path):
            super().after_image(image, img_save_path)
            self.progress.after_image(image, img_save_path)

    option = jmcomic.JmOption.construct(option_dict)
    progress = ProgressCallback(task, lock, state_dir, download_dir)

    with ProgressDownloader(option, progress) as downloader:
        if task._stop_event.is_set():
            return None

        # 返回值由 progress.before_album 回填到 progress.album_info；
        # download_album 的返回对象本身用不到（以前赋给 album 后从未使用）。
        downloader.download_album(task.album_id)
        return progress.album_info
