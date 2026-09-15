"""缩略图全量重建（BackgroundTask）（ImageViewerPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 ImageViewerPlugin.__init__ 持有 ——
分片只把方法挂到同一个类上，因此方法与调用点都没有变。
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List

from shell.backend.plugin_utils import load_sibling
from shell.backend.tasks import BackgroundTask

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'image_viewer')
ALLOWED_EXTENSIONS = _common.ALLOWED_EXTENSIONS

class RebuildMixin:
    """缩略图全量重建（BackgroundTask）。"""

    def _collect_all_images(self, rel_path: str = '') -> List[str]:
        """收集整个相册树（或指定子文件夹）下所有需要生成缩略图的图片虚拟路径。

        空路径 = 全部根目录；命名空间节点 = 只收集该额外根目录。
        """
        rel_path = (rel_path or '').strip().strip('/')
        pairs = []
        if not rel_path:
            pairs = [(root, '') for root in self._roots()]
        else:
            root, inner = self._split_virtual(rel_path)
            if root is not None:
                pairs = [(root, inner)]
        images = []
        for root, inner in pairs:
            base_dir = root / inner if inner else root
            prefix = self._virtual_path(root, inner)
            try:
                for current, dir_names, filenames in os.walk(base_dir):
                    dir_names[:] = [d for d in dir_names
                                    if not d.startswith('.') and d != '.cache']
                    current_path = Path(current)
                    rel_dir = ('' if current_path == base_dir
                               else current_path.relative_to(base_dir).as_posix())
                    for name in filenames:
                        if name.startswith('.'):
                            continue
                        if Path(name).suffix.lower() in ALLOWED_EXTENSIONS:
                            parts = [p for p in (prefix, rel_dir, name) if p]
                            images.append('/'.join(parts))
            except OSError:
                pass
        return images

    def rebuild_all(self, rel_path: str = '', force: bool = True) -> Dict:
        """全量/指定文件夹重建。

        - rel_path 非空时：只重建该文件夹，且不会清空已有缩略图（增量补齐）。
        - rel_path 为空且 force=True 时：清空旧缓存后全量重新生成。
        - rel_path 为空且 force=False 时：全库增量补齐，跳过已有有效缩略图。
        """
        rel_path = (rel_path or '').strip().strip('/')
        if rel_path and not self._is_safe(rel_path):
            return {'started': False, 'success': False, 'error': '非法路径'}
        if self._rebuild and self._rebuild.state == 'running':
            return {'started': False, 'running': True, **self.rebuild_status()}

        self._list_cache.clear()
        if not rel_path and force:
            self._invalidate_albums_cache()
            self._meta_cache = {}
            try:
                if self.meta_file.exists():
                    self.meta_file.unlink()
            except OSError:
                pass
            self.thumb_cache.clear()
            # 旧版散文件缩略图目录已不再使用，全量重建时一并清理。
            try:
                if self.thumb_dir.exists():
                    shutil.rmtree(self.thumb_dir)
            except OSError:
                pass
            self.thumb_dir.mkdir(parents=True, exist_ok=True)
            self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}
            try:
                if self.album_cache_file.exists():
                    self.album_cache_file.unlink()
            except OSError:
                pass

        images = self._collect_all_images(rel_path)
        task = BackgroundTask(kind='rebuild', extra={'rebuild_path': rel_path})
        task.update(total=len(images))
        task.start(self._rebuild_worker, args=(images,))
        self._rebuild = task
        return {'started': True, 'running': True, 'total': len(images)}

    def rebuild_folder(self, rel_path: str) -> Dict:
        """只重建指定文件夹下的缩略图（增量补齐，不清空已有缓存）。"""
        return self.rebuild_all(rel_path=rel_path, force=False)

    def _rebuild_worker(self, task: BackgroundTask, images: List[str]):
        """全量重建 worker：批量并行生成缩略图到 ThumbCache。"""
        def progress(processed, total, current, errors):
            task.update(processed=processed, total=total, current=current, errors=errors)

        try:
            items = [(rel, self.root_dir / rel) for rel in images]
            result = self.thumb_cache.generate_bulk(
                items,
                progress_cb=progress,
                stop_event=task.stop_event,
            )
            task.update(processed=result['processed'], errors=result['errors'])
        except Exception as e:
            task.add_error(f'重建异常: {e}')
        finally:
            task.update(current='')

    def rebuild_status(self) -> Dict:
        """返回全量重建后台任务的进度。"""
        task = self._rebuild
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

    def rebuild_cancel(self) -> Dict:
        """请求取消当前全量重建任务；已生成的缩略图会保留。"""
        if self._rebuild and self._rebuild.state == 'running':
            self._rebuild.cancel()
            return {'success': True}
        return {'success': False, 'error': '没有正在运行的重建任务'}

    def on_unload(self) -> None:
        """进程退出收尾：取消正在跑的缩略图重建任务（已生成的缩略图保留）。"""
        if self._rebuild and self._rebuild.state == 'running':
            self._rebuild.cancel()
