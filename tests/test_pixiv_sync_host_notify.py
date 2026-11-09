"""同步任务进入终态后通知宿主失效派生数据（T4）。

为什么需要这条用例：pixiv-sync 把作品图直接写进宿主 image-viewer 的图片根（默认就是
它的数据根），而宿主的相册树与目录快照按自己的 TTL 复用 —— 实测「下载完整个新画师
目录后，宿主的 `list_albums` 完全不含那个目录」。所以下载任务结束时要通知宿主一次，
且必须满足两条：

- **只一次**：收口放在 `SyncRunner._run_sync` 的收尾（`finally`），不是每张作品调一次；
  用户先点取消再由线程收尾的路径也只算一次。
- **只在真的写过盘时发**：未配 token 的失败、清单为空/全部已下的零下载空跑都不通知
  （宿主没变，清一次相册索引只会让它白重扫）；有下载的成功、失败与取消路径都通知。
- **不反向影响任务**：宿主失效实现抛错、宿主没加载、旧宿主没有这个方法，都不能改变
  任务终态，也不能让异常逃出 worker 线程。

用替身宿主记录调用，不构造真实 image-viewer（真实宿主会顺手建落盘缓存，与这里要钉的
事实无关）。"确有下载"由 `download_pending` 的替身把计数写进任务对象来驱动，三条
终态（done / cancelled / 零下载 done）与"未配 token 失败"各走一条最便宜的路径。

运行：
    venv/Scripts/python.exe -m unittest tests.test_pixiv_sync_host_notify -v
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIXIV_BACKEND = PROJECT_ROOT / 'plugins' / 'pixiv-sync' / 'backend'
for _p in (str(PROJECT_ROOT), str(PIXIV_BACKEND), str(PIXIV_BACKEND / 'libs')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pixiv_sync import download as download_mod
from pixiv_sync import scan as scan_mod

#: 通知里带的 reason：说明「谁改的根、为什么改」。宿主只记日志，不用它做判断。
DOWNLOAD_REASON = 'pixiv-sync:download'


def _load_plugin_class():
    main_path = PIXIV_BACKEND / 'main.py'
    spec = importlib.util.spec_from_file_location('pixiv_sync_host_notify_test', str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PixivSyncPlugin


class FakeHost:
    """替身宿主：只记录 `invalidate_derived` 的调用（可按需让它抛错 / 缺方法）。"""

    def __init__(self, invalidate_error=None, with_method=True):
        self.calls = []
        self._invalidate_error = invalidate_error
        if with_method:
            self.invalidate_derived = self._record

    def _record(self, reason=''):
        self.calls.append(reason)
        if self._invalidate_error is not None:
            raise self._invalidate_error


class PixivSyncHostNotifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin_class = _load_plugin_class()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        cache = self.root / '.cache' / 'pixiv-sync'
        cache.mkdir(parents=True)
        (cache / 'downloaded_ids.json').write_text(
            json.dumps({'ids': []}), encoding='utf-8')
        (cache / 'failed_ids.json').write_text(
            json.dumps({'ids': []}), encoding='utf-8')

        original = getattr(self.plugin_class, '_resolved_config', None)
        self.plugin_class._resolved_config = {'download_dir': str(self.root)}
        try:
            self.plugin = self.plugin_class({'name': 'pixiv-sync'}, {'directories': {}})
        finally:
            self.plugin_class._resolved_config = original
        self.runner = self.plugin.runner

    def tearDown(self):
        # 关 SQLite：Windows 下连接没关会让临时目录删不掉（WinError 32）。
        self.plugin.on_unload()
        self._tmp.cleanup()

    # ---------- 确有下载的终态各一次；没写过的不通知 ----------

    def _sync_with_download(self, count: int = 1, cancel_after: bool = False) -> None:
        """跑一趟真的"下载过"的同步：替身把计数写进任务对象。

        `cancel_after` 模拟"下载途中用户点了取消"：同一趟里已写出的文件仍要通知宿主。
        """
        def fake_download(_operand, task, _ids, _failed, _kind):
            task['downloaded'] = int(task.get('downloaded') or 0) + count
            if cancel_after:
                self.runner._cancel_event.set()

        with mock.patch.object(self.plugin.session, 'authenticate', return_value=None), \
                mock.patch.object(scan_mod, 'collect_following_pending',
                                  return_value=([], {'complete': True})), \
                mock.patch.object(download_mod, 'download_pending', side_effect=fake_download):
            self.runner._run_sync('following')

    def test_done_terminal_state_notifies_host_once(self):
        """成功跑完且确有下载：下载写入结束之后通知一次。"""
        host = FakeHost()
        self.plugin._host = host

        self._sync_with_download()

        self.assertEqual(self.runner.task_snapshot()['state'], 'done')
        self.assertEqual(host.calls, [DOWNLOAD_REASON],
                         '收口在任务收尾，不是每张作品一次')

    def test_failed_before_writing_does_not_notify(self):
        """未配 token：登录就失败，一个文件都没写，不该让宿主重扫一遍。"""
        host = FakeHost()
        self.plugin._host = host

        self.runner._run_sync('following')

        self.assertEqual(self.runner.task_snapshot()['state'], 'failed')
        self.assertEqual(host.calls, [])

    def test_zero_download_success_does_not_notify(self):
        """清单为空或全部已下的空跑：终态 done 但没写盘，同样不通知。"""
        host = FakeHost()
        self.plugin._host = host
        with mock.patch.object(self.plugin.session, 'authenticate', return_value=None), \
                mock.patch.object(scan_mod, 'collect_following_pending',
                                  return_value=([], {'complete': True})), \
                mock.patch.object(download_mod, 'download_pending', return_value=None):
            self.runner._run_sync('following')

        self.assertEqual(self.runner.task_snapshot()['state'], 'done')
        self.assertEqual(host.calls, [])

    def test_cancelled_after_partial_download_notifies_once(self):
        """下载途中被取消：已写出的文件同样要通知，且只一次。"""
        host = FakeHost()
        self.plugin._host = host

        self._sync_with_download(count=2, cancel_after=True)

        self.assertEqual(self.runner.task_snapshot()['state'], 'cancelled')
        self.assertEqual(host.calls, [DOWNLOAD_REASON])

    def test_cancel_task_does_not_notify_by_itself(self):
        """`cancel_task` 只定稿状态：此时写盘还没结束，通知留给线程收尾。"""
        host = FakeHost()
        self.plugin._host = host
        self.runner._task = {
            'kind': 'following', 'state': 'running', 'done': 0, 'total': 1,
            'downloaded': 0, 'skipped': 0, 'failed': 0, 'current': '正在下载',
            'started_at': 0.0, 'phase_started_at': 0.0, 'finished_at': None, 'error': '',
        }

        self.assertTrue(self.runner.cancel_task()['ok'])
        self.assertEqual(host.calls, [], '取消动作本身不通知（此时写盘还没结束）')

    # ---------- 不该通知的路径 ----------

    def test_refresh_only_task_does_not_notify(self):
        """只刷新名单的任务不写图片根，不该让宿主重扫一遍。"""
        host = FakeHost()
        self.plugin._host = host

        self.runner._run_refresh('following')

        self.assertEqual(self.runner.task_snapshot()['state'], 'failed')
        self.assertEqual(host.calls, [])

    # ---------- 宿主各种缺能力都不能影响任务 ----------

    def test_invalidate_failure_does_not_change_terminal_state(self):
        host = FakeHost(invalidate_error=RuntimeError('宿主缓存坏了'))
        self.plugin._host = host

        self._sync_with_download()

        self.assertEqual(self.runner.task_snapshot()['state'], 'done',
                         '通知失败不得改写终态')
        self.assertEqual(host.calls, [DOWNLOAD_REASON], '调用确实发生了')

    def test_old_host_without_the_method_is_tolerated(self):
        host = FakeHost(with_method=False)
        self.plugin._host = host

        self._sync_with_download()

        self.assertEqual(self.runner.task_snapshot()['state'], 'done')
        self.assertFalse(hasattr(host, 'invalidate_derived'))

    def test_host_not_loaded_is_tolerated(self):
        """依赖未加载（测试 / 缺 image-viewer）：入口的 `_get_host` 抛错，这里跳过。"""
        self.plugin._host = None            # get_dependency 返回 None → RuntimeError

        self._sync_with_download()

        self.assertEqual(self.runner.task_snapshot()['state'], 'done')

    def test_entry_callback_never_raises(self):
        """回调契约：runner 在任务收尾处直接调用它，所以它必须自己吞掉异常。"""
        self.plugin._host = FakeHost(invalidate_error=RuntimeError('宿主缓存坏了'))
        self.plugin._invalidate_host_derived(DOWNLOAD_REASON)     # 宿主实现抛错

        self.plugin._host = FakeHost(with_method=False)
        self.plugin._invalidate_host_derived(DOWNLOAD_REASON)     # 旧宿主没有该方法

        self.plugin._host = None
        self.plugin._invalidate_host_derived(DOWNLOAD_REASON)     # 宿主未加载

    # ---------- 注入与默认值 ----------

    def test_entry_injects_the_callback(self):
        """入口必须把回调交给 runner；runner 自己不 import 宿主。"""
        self.assertIs(getattr(self.runner._invalidate_host, '__self__', None), self.plugin)
        self.assertEqual(getattr(self.runner._invalidate_host, '__name__', ''),
                         '_invalidate_host_derived')

    def test_runner_default_callback_is_none(self):
        """默认 None：没有注入时同步收尾不调用任何宿主能力。"""
        from pixiv_sync.runner import SyncRunner

        class _Workspace:
            def load_task(self):
                return None

        runner = SyncRunner(None, None, None, _Workspace(), None, None, None, None)
        self.assertIsNone(runner._invalidate_host)


if __name__ == '__main__':
    unittest.main()
