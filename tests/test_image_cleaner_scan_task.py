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

    def test_scan_start_is_single_flight(self):
        """并发请求不得各起一个任务：查-建-赋值必须在同一把锁里。

        回归对象：`scan_start` 原先没有锁，Flask 是 threaded=True，两个并发请求
        可以各起一个后台任务，后一个覆盖前一个的 `_scan_task`（前者仍在跑，取消
        就再也够不着它）。
        """
        cleaner = self._cleaner()
        gate = threading.Event()
        task = BackgroundTask(kind='image-cleaner-scan', extra={'mode': 'dupe'})
        task.start(lambda t: gate.wait(10))
        cleaner._scan_task = task
        try:
            second = cleaner.scan_start('dupe')
            self.assertFalse(second['started'], f'已有任务在跑时不该再起一个: {second}')
            self.assertTrue(second['running'])
        finally:
            gate.set()
            task.cancel()

    # ---------- 5. 统一刷新基建 ----------

    def test_freshness_verify_hashes_and_regroups(self):
        """校验一趟 = 维护指纹 + 重新分组写盘（本插件的派生数据全在这一步里）。"""
        from shell.backend.freshness import engine_for

        _make_image(self.root / 'a.jpg', (200, 30, 30))
        _make_image(self.root / 'b.jpg', (200, 30, 30))
        _make_image(self.root / 'c.jpg', (10, 200, 10))
        cleaner = self._cleaner()
        engine = engine_for(cleaner)
        self.assertIsNotNone(engine, 'image-cleaner 必须声明 freshness_spec()')
        engine.spec.min_sync_interval = 0.0

        report = engine.verify()

        self.assertEqual(report['pass_end']['scanned'], 3)
        dupe = cleaner.get_cached_scan('dupe')
        self.assertTrue(dupe['cached'])
        self.assertFalse(dupe['stale'], '校验完的结果不该还是过期的')
        self.assertEqual([sorted(g['files']) for g in dupe['groups']],
                         [['a.jpg', 'b.jpg']])
        # 指纹落盘：旧后台路径从不保存 dhash.json，重启即全量重算
        self.assertTrue(cleaner._dhash_cache_path().exists(), 'dHash 缓存必须落盘')

    def test_deleted_file_invalidates_result_and_hash(self):
        from shell.backend.freshness import engine_for

        _make_image(self.root / 'a.jpg', (200, 30, 30))
        _make_image(self.root / 'b.jpg', (200, 30, 30))
        cleaner = self._cleaner()
        engine = engine_for(cleaner)
        engine.spec.min_sync_interval = 0.0
        engine.verify()
        self.assertEqual(len(cleaner.get_cached_scan('dupe')['groups']), 1)

        (self.root / 'b.jpg').unlink()
        engine.spec.min_sync_interval = 0.0
        engine.sync('', force=True)

        self.assertTrue(cleaner.get_cached_scan('dupe')['stale'],
                        '磁盘变了之后结果缓存必须标记过期')
        after = engine.verify()
        self.assertEqual(after['audited']['valid_entries'], 1)
        self.assertEqual(cleaner.get_cached_scan('dupe')['groups'], [],
                         '重新分组后不该再报已删除文件的重复对')
        dead = cleaner._dhash_cache_key(str(self.root / 'b.jpg'))
        self.assertNotIn(dead, cleaner._dhash_cache, '已删除文件的指纹必须清掉')

    def test_threshold_change_invalidates_similar_result(self):
        from shell.backend.freshness import engine_for

        _make_image(self.root / 'a.jpg', (200, 30, 30))
        cleaner = self._cleaner()
        engine = engine_for(cleaner)
        engine.spec.min_sync_interval = 0.0
        engine.verify()
        self.assertFalse(cleaner.get_cached_scan('similar')['stale'])

        cleaner.on_settings_changed({'threshold'})

        self.assertTrue(cleaner.get_cached_scan('similar')['stale'],
                        '改阈值后相似分组必须重算（旧实现看完还是旧分组）')

    def test_abs_for_rel_handles_extra_root_prefix(self):
        """虚拟路径 → 绝对路径：命名空间前缀先匹配，再回落第一根。"""
        extra = self.tmp / 'extra'
        _make_image(extra / 'a.jpg')
        _make_image(self.root / 'b.jpg')
        viewer = self._viewer()
        viewer.setting = lambda key, default=None: (
            str(extra) if key == 'extra_roots' else
            (str(self.root) if key == 'root_dir' else default))
        viewer._rebuild_paths()
        cleaner = self.cleaner_module.ImageCleanerPlugin(
            {'name': 'image-cleaner'}, {'directories': {'data_root': str(self.root)}})
        cleaner._host = viewer

        roots = cleaner._scan_roots()
        prefix = next(p for _r, p in roots if p)
        self.assertEqual(Path(cleaner._abs_for_rel(f'{prefix}/a.jpg')).resolve(),
                         (extra / 'a.jpg').resolve())
        self.assertEqual(Path(cleaner._abs_for_rel('b.jpg')).resolve(),
                         (self.root / 'b.jpg').resolve())
        self.assertIsNone(cleaner._abs_for_rel(''))


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
