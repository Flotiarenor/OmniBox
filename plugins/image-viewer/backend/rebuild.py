"""缩略图全量 / 定向重建的后台任务。

状态自持：`BackgroundTask` 句柄在这里。它是**可选能力**：`/thumbs` 本来就按需生成，
重建只是给"一次生成整库"留的入口（不再挂在工具栏上），因此取消与退出收尾都由本组件
负责，入口只把 `on_unload` 委派进来。

`force=True` 的全量重建会清空全部派生缓存（缩略图库、尺寸元数据、相册索引），所以
破坏面比其余方法大 —— 清在这里、由入口编排，不让其它组件各自记得。
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from shell.backend.tasks import BackgroundTask

log = logging.getLogger(__name__)


class RebuildTask:
    """后台全量 / 增量重建缩略图。"""

    def __init__(self, paths: Any, thumbs: Any, albums: Any, listing: Any,
                 extensions: Any) -> None:
        self._paths = paths
        self._thumbs = thumbs
        self._albums = albums
        self._listing = listing
        self._extensions = extensions
        self._task: Optional[BackgroundTask] = None

    # ===== 收集 =====

    def collect_all_images(self, rel_path: str = '') -> List[str]:
        """收集整个相册树（或指定子文件夹）下所有需要生成缩略图的图片虚拟路径。

        空路径 = 全部根目录；命名空间节点 = 只收集该额外根目录。
        """
        rel_path = (rel_path or '').strip().strip('/')
        pairs = []
        if not rel_path:
            pairs = [(root, '') for root in self._paths.roots()]
        else:
            root, inner = self._paths.split_virtual(rel_path)
            if root is not None:
                pairs = [(root, inner)]
        images: List[str] = []
        for root, inner in pairs:
            base_dir = root / inner if inner else root
            prefix = self._paths.virtual_path(root, inner)
            try:
                for current, dir_names, filenames in os.walk(base_dir):
                    dir_names[:] = [name for name in dir_names
                                    if not name.startswith('.') and name != '.cache']
                    current_path = Path(current)
                    rel_dir = ('' if current_path == base_dir
                               else current_path.relative_to(base_dir).as_posix())
                    for name in filenames:
                        if name.startswith('.'):
                            continue
                        if Path(name).suffix.lower() in self._extensions:
                            parts = [part for part in (prefix, rel_dir, name) if part]
                            images.append('/'.join(parts))
            except OSError:
                pass
        return images

    # ===== 任务 =====

    def rebuild_all(self, rel_path: str = '', force: bool = True) -> Dict:
        """全量/指定文件夹重建。

        - rel_path 非空时：只重建该文件夹，且不会清空已有缩略图（增量补齐）。
        - rel_path 为空且 force=True 时：清空旧缓存后全量重新生成。
        - rel_path 为空且 force=False 时：全库增量补齐，跳过已有有效缩略图。
        """
        rel_path = (rel_path or '').strip().strip('/')
        if rel_path and not self._paths.is_safe(rel_path):
            return {'started': False, 'success': False, 'error': '非法路径'}
        if self._task and self._task.state == 'running':
            return {'started': False, 'running': True, **self.status()}

        self._listing.clear_cache()
        if not rel_path and force:
            self._albums.invalidate()
            self._thumbs.clear_meta()
            self._thumbs.cache.clear()
            # 旧版散文件缩略图目录已不再使用，全量重建时一并清理。
            try:
                if self._paths.thumb_dir.exists():
                    shutil.rmtree(self._paths.thumb_dir)
            except OSError:
                pass
            self._paths.thumb_dir.mkdir(parents=True, exist_ok=True)
            self._albums.reset_index()
            try:
                if self._paths.album_cache_file.exists():
                    self._paths.album_cache_file.unlink()
            except OSError:
                pass

        images = self.collect_all_images(rel_path)
        task = BackgroundTask(kind='rebuild', extra={'rebuild_path': rel_path})
        task.update(total=len(images))
        task.start(self._worker, args=(images,))
        self._task = task
        return {'started': True, 'running': True, 'total': len(images)}

    def rebuild_folder(self, rel_path: str) -> Dict:
        """只重建指定文件夹下的缩略图（增量补齐，不清空已有缓存）。"""
        return self.rebuild_all(rel_path=rel_path, force=False)

    def _worker(self, task: BackgroundTask, images: List[str]) -> None:
        """重建 worker：批量并行生成缩略图到缩略图库。"""
        def progress(processed, total, current, errors):
            task.update(processed=processed, total=total, current=current, errors=errors)

        try:
            items = [(rel, self._paths.root / rel) for rel in images]
            result = self._thumbs.cache.generate_bulk(
                items,
                progress_cb=progress,
                stop_event=task.stop_event,
            )
            task.update(processed=result['processed'], errors=result['errors'])
        except Exception as exc:
            task.add_error(f'重建异常: {exc}')
        finally:
            task.update(current='')

    def status(self) -> Dict:
        """返回全量重建后台任务的进度。"""
        task = self._task
        if not task:
            return {
                'running': False,
                'done': False,
                'success': False,
                'cancelled': False,
                'rebuild_path': '',
                'total': 0,
                'processed': 0,
                'current': '',
                'error_count': 0,
                'errors': [],
            }
        status = task.status()
        return {
            'running': status['running'],
            'done': status['done'],
            'success': status['success'],
            'cancelled': status['cancelled'],
            'rebuild_path': status['extra'].get('rebuild_path', ''),
            'total': status['total'],
            'processed': status['processed'],
            'current': status['current'],
            'error_count': status['error_count'],
            'errors': status['errors'],
        }

    def cancel(self) -> Dict:
        """请求取消当前全量重建任务；已生成的缩略图会保留。"""
        if self._task and self._task.state == 'running':
            self._task.cancel()
            return {'success': True}
        return {'success': False, 'error': '没有正在运行的重建任务'}

    def shutdown(self) -> None:
        """进程退出收尾：取消正在跑的重建任务（已生成的缩略图保留）。"""
        if self._task and self._task.state == 'running':
            self._task.cancel()
