"""下载执行器：把接口客户端下到的图片按章节落盘，并维护进度与取消语义。

**取消语义**：任务的 `_stop_event` 只在块级检查点上读取 —— 章节级（进入下一章之前）
与图片级（每一张图落盘之前）。检查点必须放在真正写盘的那个函数里：只在调度处置
信号、循环不查，结果是界面显示"已暂停"而线程继续写盘。

**依赖注入**：接口客户端与图片解码都由入口（`main.py`）注入。本文件不 import 任何
兄弟模块 —— 插件后端由 importlib 按入口文件加载，后端目录不在 `sys.path` 上，
组件之间互相 import 会在运行期 ModuleNotFoundError（分层门禁 R3）。

**为什么有 `save_image` 接缝**：图片落盘要做乱序重排（`jm_mini.save_image`），而重排与
接口客户端是两件事。入口把两者分别注入，用例因此可以在不打网络的前提下验证
"暂停后不再写盘"这条行为。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: 估算速度时按每张图 500 KB 计：接口不返回图片大小，只能取一个经验值。
BYTES_PER_IMAGE = 500 * 1024
#: 每写这么多张图落一次进度快照（每次落盘都要序列化整条任务，不必每张都写）。
STATE_SAVE_EVERY = 5


def _sanitize(name: str) -> str:
    """把标题变成 Windows 合法目录名：禁字符换 `_`，去掉结尾的点。"""
    cleaned = ''.join('_' if char in '\\/:*?"<>|\n\t\r' else char for char in name)
    return cleaned.rstrip('.')


def chapter_dir(base_dir: str, album: Any, chapter: Any) -> str:
    """章节落盘目录：单章本子直接放漫画目录，多章按「第 N 話 标题」分子目录。

    `base_dir` 就是漫画目录（`<漫画根>/<漫画 id>`，见 `downloads.py` 的任务构造）；
    多章必须再分子目录：站点给的图片名是 `00001.webp` 这类纯序号，所有章节塞进同一
    目录会互相覆盖。目录措辞与既有漫画库保持一致，否则已下载的书会被当成新书。
    """
    if len(album) <= 1:
        return base_dir
    return os.path.join(base_dir, _sanitize(chapter.indextitle))


def execute_download(task, manga_dir: str, state_dir: str, lock,
                     logger=None, client: Any = None,
                     save_image: Optional[Callable[..., None]] = None) -> Optional[Dict]:
    """执行实际下载，返回 album_info；被停止（含下载中途暂停）时返回 None。"""
    if client is None or save_image is None:
        raise RuntimeError(
            '下载依赖未注入：接口客户端与图片解码由入口 main.py 装配时传入 '
            '（execute_download 的 client / save_image 参数）')

    download_dir = task.download_dir or os.path.join(manga_dir, task.album_id)
    # 纵深防御：即使调用方没校验 album_id，也绝不允许下载目录落在漫画根目录之外
    # （os.path.join 遇到绝对路径/`..` 会折叠出根目录，makedirs 就会在外部建目录）。
    root = os.path.realpath(manga_dir)
    target = os.path.realpath(download_dir)
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f'下载目录越界: {download_dir!r} 不在漫画根目录 {manga_dir!r} 内')
    os.makedirs(download_dir, exist_ok=True)

    progress = ProgressCallback(task, lock, state_dir, download_dir, client, logger,
                               save_image)
    album = client.album(task.album_id)
    progress.before_album(album)

    for ref in album.chapters:
        if task._stop_event.is_set():
            return None
        chapter = client.chapter(ref.photo_id)
        progress.before_photo(chapter)
        chapter_path = chapter_dir(download_dir, album, chapter)
        os.makedirs(chapter_path, exist_ok=True)
        _download_chapter(task, client, chapter, chapter_path, progress, logger)

    # 全部章节下完才知道真实页数（接口的漫画维度没有这个字段），补写一次 album_info
    progress.finish()
    if task._stop_event.is_set():
        return None
    return progress.album_info


def _download_chapter(task, client: Any, chapter: Any, chapter_path: str,
                      progress: 'ProgressCallback', logger=None) -> None:
    """并发下载一章的图片；单张失败不影响其余，整章全失败才让任务失败。"""
    if not chapter.images:
        return
    failures: List[str] = []

    def download_one(name: str) -> None:
        if task._stop_event.is_set():
            return
        save_path = Path(chapter_path) / f'{Path(name).stem}.jpg'
        try:
            raw = client.image_bytes(chapter.photo_id, name)
            if task._stop_event.is_set():      # 传输期间被暂停：不落盘
                return
            progress.save_image(raw, save_path, chapter, name)
        except Exception as error:             # 单张失败只记日志，最后统一判定
            failures.append(f'{name}: {type(error).__name__}: {error}')
            if logger:
                logger.warning(f'[{task.album_id}] 图片 {name} 下载失败: {error}')
            return
        progress.after_image(name, str(save_path))

    workers = max(1, min(int(getattr(task, 'concurrency', 1) or 1), len(chapter.images)))
    if workers == 1:
        for name in chapter.images:
            download_one(name)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(download_one, chapter.images))

    if failures and len(failures) >= len(chapter.images):
        raise RuntimeError(f'章节 {chapter.photo_id} 全部图片下载失败：{failures[0]}')
    if failures and logger:
        logger.warning(f'[{task.album_id}] 章节 {chapter.photo_id} '
                       f'有 {len(failures)}/{len(chapter.images)} 张失败')


class ProgressCallback:
    """进度回调：把下载进度写进任务对象、进度快照与 `album_info.json`。"""

    def __init__(self, task, lock, state_dir: str, download_dir: str,
                 client: Any, logger=None,
                 save_image: Optional[Callable[..., None]] = None) -> None:
        self.task = task
        self.lock = lock
        self.state_dir = state_dir
        self.download_dir = download_dir
        self.client = client
        self.logger = logger
        self._save_fn = save_image
        self.last_update = time.time()
        self.last_count = 0
        #: 漫画声明的总页数（接口不返回，按章节累加）
        self.album_page_count = 0
        #: 各章节 len(chapter) 的累加值
        self.photo_total = 0
        self.chapters: List[Dict] = []
        self.album_info: Optional[Dict] = None

    # ------------------------------------------------------------------ 钩子

    def before_album(self, album) -> None:
        with self.lock:
            self.task.title = album.name
            self.album_page_count = 0
            self.photo_total = 0
            self.task.total_images = 0
            self.task.thumb_url = self.client.cover_url(album.album_id)
            self.chapters = [
                {'chapter_id': ref.photo_id, 'title': ref.name, 'page_count': 0}
                for ref in album.chapters
            ]
            self.album_info = {
                'oname': getattr(album, 'name', 'unknown'),
                'album_id': str(album.album_id),
                'actors': list(getattr(album, 'actors', []) or []),
                'title': getattr(album, 'name', '未知主标题'),
                'author': getattr(album, 'author', '未知作者'),
                'tags': list(getattr(album, 'tags', []) or []),
                'chapter_count': len(album),
                'total_page_count': 0,
                'chapters': self.chapters,
                'download_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            self._save_album_info()
            self._save_state()

    def before_photo(self, chapter) -> None:
        # 章节详情到手时才知道图片数（接口的 images 字段）。漫画维度没有总页数，
        # 因此总页数按章节累加；两者都有效时取大值，多章不会因为重复累加而翻倍。
        with self.lock:
            self.photo_total += len(chapter)
            self.task.total_images = max(self.album_page_count, self.photo_total)
            for entry in self.chapters:
                if entry['chapter_id'] == chapter.photo_id:
                    entry['title'] = chapter.name
                    entry['page_count'] = len(chapter)
                    break
            if self.album_info is not None:
                self.album_info['total_page_count'] = self.photo_total

    def save_image(self, raw: bytes, save_path: Path, chapter, name: str) -> None:
        """落盘一张图（乱序重排由注入的 save_image 完成）。"""
        if self._save_fn is None:
            raise RuntimeError('图片落盘函数未注入')
        self._save_fn(raw, save_path, scramble_id=chapter.scramble_id,
                      photo_id=chapter.photo_id, source_name=name)

    def after_image(self, name: str, save_path: str) -> None:
        with self.lock:
            self.task.completed_images += 1
            now = time.time()
            delta = self.task.completed_images - self.last_count
            if delta >= STATE_SAVE_EVERY:
                elapsed = now - self.last_update
                if elapsed > 0:
                    self.task.speed = delta * BYTES_PER_IMAGE / elapsed
                    remaining = max(0, self.task.total_images - self.task.completed_images)
                    if self.task.speed > 0:
                        self.task.eta = int(remaining * BYTES_PER_IMAGE / self.task.speed)
                    self.last_update = now
                    self.last_count = self.task.completed_images
                self._save_state()

    def finish(self) -> None:
        with self.lock:
            if self.album_info is not None:
                self.album_info['total_page_count'] = self.photo_total
            self._save_album_info()
            self._save_state()

    # ------------------------------------------------------------------ 落盘

    def attach_save(self, injected: Callable[..., None]) -> None:
        self._save_fn = injected

    def _save_album_info(self) -> None:
        if not self.album_info:
            return
        try:
            path = os.path.join(self.download_dir, 'album_info.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(self.album_info, handle, ensure_ascii=False, indent=2, default=str)
        except Exception as error:
            if self.logger:
                self.logger.error(f'保存 album_info.json 失败: {error}')

    def _save_state(self) -> None:
        try:
            path = os.path.join(self.state_dir, f'progress_{self.task.id}.json')
            state = {
                'task_id': self.task.id,
                'album_id': self.task.album_id,
                'completed_images': self.task.completed_images,
                'total_images': self.task.total_images,
                'speed': self.task.speed,
                'eta': self.task.eta,
                'status': self.task.status,
            }
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(state, handle, ensure_ascii=False)
        except Exception as error:
            if self.logger:
                self.logger.error(f'保存进度状态失败: {error}')
