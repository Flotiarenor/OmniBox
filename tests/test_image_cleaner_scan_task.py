"""image-cleaner 的后台扫描任务契约（共享基建 `shell.backend.tasks.BackgroundTask`）。

背景：整库扫描要为每张图算指纹（相似模式还要两两比较），大图库上是分钟级的；放在同步
API（`duplicate_scan` / `similar_scan`）里既没有进度也不能取消。现在扫描跑在后台任务里
（`scan_start` / `scan_status` / `scan_cancel`），与 image-viewer 的缩略图重建
（`rebuild_all` / `rebuild_status` / `rebuild_cancel`）用的是同一套骨架。

本用例用真实插件实例（不经过 Shell）锁住：
  1. `scan_start` 真的把任务跑起来并把结果写进扫描缓存（与同步入口同一份分组逻辑）；
  2. 已有任务在跑时再 `scan_start` 不会叠第二个任务；
  3. 取消的任务**不写**结果缓存（没有"半份结果"被当成完整结果渲染）；
  4. 空转时的 `scan_status` / `scan_cancel` 形状稳定。

运行：
    python -m unittest tests.test_image_cleaner_scan_task -v
"""

import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.settings_store import SettingsStore
from shell.backend.tasks import BackgroundTask


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(PROJECT_ROOT / rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_image(path: Path, color=(10, 120, 200)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (24, 24), color).save(path)


class ScanTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer_module = _load('plugins/image-viewer/backend/main.py', 'iv_main_scan_task_test')
        cls.cleaner_module = _load('plugins/image-cleaner/backend/main.py', 'ic_main_scan_task_test')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / 'data'
        self._settings_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._settings_tmp.cleanup)
        self.settings_dir = Path(self._settings_tmp.name)

    def _viewer(self):
        module = self.viewer_module
        original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
        module.ImageViewerPlugin._resolved_config = {'root_dir': str(self.root), 'extra_roots': ''}
        try:
            plugin = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = original
        plugin._settings_store = SettingsStore(str(self.settings_dir))
        return plugin

    def _cleaner(self):
        plugin = self.cleaner_module.ImageCleanerPlugin(
            {'name': 'image-cleaner'},
            {'directories': {'data_root': str(self.root)}},
        )
        plugin._host = self._viewer()
        plugin._settings_store = SettingsStore(str(self.settings_dir))
        return plugin

    def _wait(self, plugin, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = plugin.scan_status()
            if not status['running']:
                return status
            time.sleep(0.02)
        self.fail(f'扫描任务未在 {timeout:.0f}s 内收尾')
        raise AssertionError('unreachable')   # self.fail 之后仍要让类型检查看得懂

    # ---------- 1. 起任务 → 写缓存 ----------

    def test_scan_start_runs_task_and_writes_cache(self):
        _make_image(self.root / 'a.jpg', (7, 7, 7))
        _make_image(self.root / '副本' / 'a.jpg', (7, 7, 7))   # 与 a.jpg 逐字节相同
        _make_image(self.root / 'b.jpg', (9, 9, 9))
        cleaner = self._cleaner()

        started = cleaner.scan_start('dupe')
        self.assertTrue(started.get('started'), started)
        status = self._wait(cleaner)
        self.assertTrue(status['success'], status)
        self.assertEqual(status['mode'], 'dupe')
        self.assertEqual(status['total'], 3, '进度分母是本次扫描的文件数')
        self.assertEqual(status['processed'], 3, '全部文件都定论后进度走满')
        self.assertEqual(status['groups'], 1)

        cached = cleaner.get_cached_scan('dupe')
        self.assertTrue(cached['cached'], '任务结束必须把结果写进扫描缓存')
        self.assertEqual(cached['scanned'], 3)
        self.assertEqual([sorted(g['files']) for g in cached['groups']],
                         [['a.jpg', '副本/a.jpg']],
                         '后台任务与同步入口必须用同一份分组逻辑')
        self.assertEqual([sorted(g['files']) for g in cleaner.duplicate_scan()['groups']],
                         [sorted(g['files']) for g in cached['groups']])

    def test_similar_scan_task_writes_cache_with_phase_progress(self):
        _make_image(self.root / 'a.jpg', (7, 7, 7))
        _make_image(self.root / 'b.jpg', (200, 30, 30))
        cleaner = self._cleaner()

        cleaner.scan_start('similar')
        status = self._wait(cleaner)
        self.assertTrue(status['success'], status)
        self.assertEqual(status['mode'], 'similar')
        self.assertEqual(status['total'], 2, '第二阶段的分母是有效图片数')
        self.assertEqual(status['processed'], 2)
        self.assertTrue(cleaner.get_cached_scan('similar')['cached'])

    def test_unknown_mode_falls_back_to_dupe(self):
        _make_image(self.root / 'a.jpg')
        cleaner = self._cleaner()
        self.assertEqual(cleaner.scan_start('nonsense').get('mode'), 'dupe')
        self._wait(cleaner)

    # ---------- 2. 不叠第二个任务 ----------

    def test_scan_start_while_running_does_not_stack_tasks(self):
        cleaner = self._cleaner()
        gate = threading.Event()
        task = BackgroundTask(kind='image-cleaner-scan', extra={'mode': 'dupe'})
        task.start(lambda t: gate.wait(10))
        cleaner._scan_task = task
        try:
            again = cleaner.scan_start('dupe')
            self.assertFalse(again.get('started'), '已有任务在跑时不能再起一个')
            self.assertTrue(again.get('running'))
            self.assertTrue(cleaner.scan_cancel()['success'])
        finally:
            gate.set()
            task.cancel()

    # ---------- 3. 取消不写结果 ----------

    def test_cancelled_worker_does_not_write_cache(self):
        """取消点是任务级的：worker 直接返回，结果缓存里不能出现"半份结果"。"""
        _make_image(self.root / 'a.jpg', (7, 7, 7))
        _make_image(self.root / '副本' / 'a.jpg', (7, 7, 7))
        cleaner = self._cleaner()
        task = BackgroundTask(kind='image-cleaner-scan', extra={'mode': 'dupe'})
        task.cancel()                                    # 未 start 就取消 → cancelled 为真

        cleaner._scan_worker(task, 'dupe', None)

        self.assertFalse(cleaner._load_scan_cache().get('dupe'),
                         '被取消的扫描不能留下任何分组结果')
        self.assertEqual(task.status()['processed'], 0)

    # ---------- 4. 空转时的形状 ----------

    def test_idle_status_and_cancel_shapes(self):
        cleaner = self._cleaner()
        status = cleaner.scan_status()
        self.assertFalse(status['running'])
        self.assertFalse(status['done'])
        self.assertEqual(status['total'], 0)
        self.assertEqual(status['mode'], '')
        cancelled = cleaner.scan_cancel()
        self.assertFalse(cancelled['success'])
        self.assertIn('没有正在运行', cancelled['error'])

    def test_on_unload_cancels_running_task(self):
        cleaner = self._cleaner()
        gate = threading.Event()
        task = BackgroundTask(kind='image-cleaner-scan', extra={'mode': 'dupe'})
        task.start(lambda t: gate.wait(10))
        cleaner._scan_task = task
        try:
            cleaner.on_unload()
            self.assertTrue(task.cancelled, '进程退出要取消正在跑的扫描（结果缓存保留）')
        finally:
            gate.set()
            task.cancel()


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
