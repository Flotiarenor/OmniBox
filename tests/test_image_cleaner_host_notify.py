"""删除写的是宿主的图片根，转发成功后必须通知宿主失效派生数据（T4）。

为什么需要这条用例：宿主的 `delete_files` 只清列表缓存与缩略图，相册树与目录快照
仍按自己的 TTL 报旧计数 —— 实测「删掉两张图之后 `list_albums` 仍是旧计数，直到宿主
自己的引擎跑完一次同步」。所以清理器必须在删除成功后显式通知一次；这条通知只影响
宿主缓存，**失败不得改写删除结果**（文件已经删掉了）。

用替身宿主记录调用：真的 `ImageViewerPlugin` 会把宿主的落盘缓存也建起来，与这里要
钉的事实（有没有调、调了几次、reason 是什么）无关。

运行：
    venv/Scripts/python.exe -m unittest tests.test_image_cleaner_host_notify -v
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 通知里带的 reason：说明「谁改的根、为什么改」。宿主只记日志，不用它做判断。
DELETE_REASON = 'image-cleaner:delete'


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(PROJECT_ROOT / rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHost:
    """替身宿主：删除行为与真实宿主同形，另记录 `invalidate_derived` 的调用。"""

    def __init__(self, deleted=None, invalidate_error=None, with_method=True,
                 data_root=None):
        self.calls = []
        self.delete_calls = []
        self._deleted = deleted
        self._invalidate_error = invalidate_error
        self._data_root = data_root
        if with_method:
            self.invalidate_derived = self._record_invalidate

    def get_data_root(self):
        """宿主的数据根：引擎按它推导扫描根（旧版宿主没有 `list_roots` 时按单根处理）。"""
        return self._data_root

    def delete_files(self, rel_paths):
        self.delete_calls.append(list(rel_paths))
        deleted = list(rel_paths) if self._deleted is None else list(self._deleted)
        return {'deleted': deleted, 'errors': []}

    def _record_invalidate(self, reason=''):
        self.calls.append(reason)
        if self._invalidate_error is not None:
            raise self._invalidate_error


class ImageCleanerHostNotifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load('plugins/image-cleaner/backend/main.py', 'ic_host_notify_test')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'data'
        self.root.mkdir(parents=True, exist_ok=True)

    def _plugin(self, host):
        plugin = self.module.ImageCleanerPlugin(
            {'name': 'image-cleaner'},
            {'directories': {'data_root': str(self.root)}},
        )
        # 入口把宿主实例缓存在 `_host` 上；这里直接放替身，避免真的解析依赖。
        plugin._host = host
        return plugin

    def test_delete_notifies_host_once(self):
        host = FakeHost(data_root=self.root)
        result = self._plugin(host).delete_files(['album1/a.jpg'])

        self.assertEqual(result, {'deleted': ['album1/a.jpg'], 'errors': []},
                         '通知不得改动删除结果的形状')
        self.assertEqual(host.calls, [DELETE_REASON],
                         '删除成功后应恰好通知宿主一次，且 reason 固定')
        self.assertEqual(host.delete_calls, [['album1/a.jpg']],
                         '仍应把原始 rel_paths 原样转发给宿主')

    def test_no_notification_when_nothing_deleted(self):
        """宿主一个都没删掉（全被拒）时根没有变化，不该让宿主重扫一遍。"""
        host = FakeHost(deleted=[], data_root=self.root)
        result = self._plugin(host).delete_files(['../越界.jpg'])

        self.assertEqual(result['deleted'], [])
        self.assertEqual(host.calls, [], '没删掉任何文件就不该通知')

    def test_notification_failure_does_not_change_result(self):
        """宿主失效实现抛错时，删除结果必须原样返回（文件已经删掉了）。"""
        host = FakeHost(invalidate_error=RuntimeError('宿主缓存坏了'), data_root=self.root)

        result = self._plugin(host).delete_files(['a.jpg'])

        self.assertEqual(result, {'deleted': ['a.jpg'], 'errors': []},
                         '通知失败不得把删除报成失败')
        self.assertEqual(host.calls, [DELETE_REASON],
                         '调用确实发生了，只是异常被吞掉')

    def test_old_host_without_the_method_is_tolerated(self):
        """旧宿主没有 `invalidate_derived`：用 getattr 跳过，删除照常返回。"""
        host = FakeHost(with_method=False, data_root=self.root)

        result = self._plugin(host).delete_files(['a.jpg'])

        self.assertEqual(result, {'deleted': ['a.jpg'], 'errors': []})
        self.assertFalse(hasattr(host, 'invalidate_derived'))

    def test_delete_resets_throttle_so_next_autosync_runs(self):
        """删除成功后下一次 autoSync 必须真跑，不能被 5 秒去抖 skip 掉。

        前端删完立刻发起 `autoSync('')`，而引擎的 `min_sync_interval` 是 5 秒：
        紧跟首屏那次同步时它会返回 `skip:throttled`，表现成"删完了面板上还是旧分组"。
        """
        from shell.backend.freshness import engine_for

        host = FakeHost(data_root=self.root)
        plugin = self._plugin(host)
        engine = engine_for(plugin)
        self.assertIsNotNone(engine, 'image-cleaner 必须声明 freshness_spec()')
        assert engine is not None

        self.assertEqual(engine.sync('', force=True)['action'], 'sync',
                         '首屏同步：写下 5 秒去抖窗口')
        self.assertEqual(engine.sync('').get('reason'), 'throttled',
                         '前提：去抖窗口确实会挡掉紧跟的 autoSync')

        plugin.delete_files(['a.jpg'])

        report = engine.sync('')
        self.assertNotEqual(report.get('reason'), 'throttled',
                            '删除成功后下一次 autoSync 必须真跑（重置去抖）')


if __name__ == '__main__':
    unittest.main()
