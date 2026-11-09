"""下载完成后书架缓存必须立刻失效。

回归对象：下载中心与书架共用同一个漫画根，但 `DownloadCenter` 写完 `album_info.json`
与整本图片后只改任务状态；书架缓存（`Library._shelf`）没有 TTL，只在
`derive` / `prune` / `on_verified` / `rebuild` / `toggle_favorite` / `rebind` 时作废。
于是「刚下完的本子在『全部漫画』里看不到，必须手动点校验」成为稳定复现的行为。

修法：`DownloadCenter` 在任务进入 `completed` 时调一次入口注入的回调，入口把它
编排成 `library.reset()` + `engine.reset_throttle()`（只置脏 + 清去抖，不做全树重扫）。

这里钉住四件事：
  * 完成回调被调用一次（不是每章 / 每图一次）；
  * 书架缓存确实作废（`_shelf is None`、`_shelf_dirty` 为真）；
  * 暂停中的任务（`_stop_event` 已置）不触发回调；
  * 回调抛异常不会把已完成的任务改成 `failed`。

下载执行器用返回空 album_info 的替身：本用例测的是完成信号的接线，不是下载本身
（真实下载循环的取消点由 `test_manga_library_download_cancel.py` 覆盖）。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'manga-library'

from shell.backend.freshness import engine_for
from shell.backend.settings_store import SettingsStore
from tools.check_plugins import _load_backend_class


class DownloadCompletionInvalidatesShelfTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'manga'
        self.root.mkdir(parents=True, exist_ok=True)

        cls: Any = _load_backend_class(PLUGIN_DIR, 'backend/main.py', 'MangaLibraryPlugin')
        cls._resolved_config = {'root_dir': str(self.root)}
        self.addCleanup(setattr, cls, '_resolved_config', {})
        #: `_load_backend_class` 的返回类型是 `PluginBase`；装配出来的组件属性是 Any
        self.plugin: Any = cls({'name': 'manga-library'},
                               {'directories': {'data_root': str(self.root)}})
        self.plugin._settings_store = SettingsStore(
            str(Path(self._tmp.name) / 'settings'))
        self.addCleanup(self.plugin.on_unload)

        engine = engine_for(self.plugin)
        self.assertIsNotNone(engine, 'manga-library 必须声明 freshness_spec()')
        assert engine is not None
        engine.spec.min_sync_interval = 0.0
        #: 引擎在装配里是 Optional；用例内断言过非 None，这里收成 Any 便于直接调用
        self.engine: Any = engine

        # 计数：回调的两种效果各记一处（书架的 reset 与引擎的去抖）
        self.resets: list = []
        self.throttles: list = []
        library_reset = self.plugin.library.reset

        def counting_reset() -> None:
            self.resets.append(time.time())
            library_reset()

        self.plugin.library.reset = counting_reset
        engine_throttle = engine.reset_throttle

        def counting_throttle() -> None:
            self.throttles.append(time.time())
            engine_throttle()

        engine.reset_throttle = counting_throttle

    # ===== 工具 =====

    def _install_stub_download(self, on_run=None):
        """把下载执行器换成"立刻返回"的替身；`on_run(task)` 可以在返回前改任务状态。"""
        def run(task, manga_dir, state_dir, lock, logger):
            if on_run is not None:
                on_run(task)
            return {}

        self.plugin.downloads._run_download = run

    def _submit(self, auto_start: bool = True) -> str:
        result = self.plugin.register_api()['download_submit'](
            '123456', concurrency=2, auto_start=auto_start)
        self.assertTrue(result.get('success'), result)
        return str(result['taskId'])

    def _task(self, task_id: str):
        return self.plugin.downloads.tasks[task_id]

    def _wait_status(self, task_id: str, expected: str, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.plugin.downloads.tasks.get(task_id)
            if task is not None and task.status == expected:
                return True
            time.sleep(0.01)
        return False

    def _join(self, task_id: str) -> None:
        task = self._task(task_id)
        if task._thread is not None:
            task._thread.join(10)

    # ===== 用例 =====

    def test_completed_download_invalidates_shelf_once(self):
        self._install_stub_download()
        # 书架先建出缓存：否则"作废"无法与"本来就没有"区分
        (self.root / 'A').mkdir()
        (self.root / 'A' / '001.jpg').write_bytes(b'\x89PNG\r\n\x1a\n')
        self.plugin.library.list_manga()
        self.assertIsNotNone(self.plugin.library._shelf, '前置条件：书架缓存已建立')
        self.assertEqual(self.resets, [])

        task_id = self._submit()
        self.assertTrue(self._wait_status(task_id, 'completed'),
                        f'任务没有完成：{self._task(task_id).status}')
        self._join(task_id)

        self.assertEqual(len(self.resets), 1, '完成回调必须让书架缓存作废一次')
        self.assertEqual(len(self.throttles), 1, '完成回调必须清一次刷新引擎的去抖')
        self.assertIsNone(self.plugin.library._shelf, '书架缓存应被清空')
        self.assertTrue(self.plugin.library._shelf_dirty, '书架缓存应被标脏')

    def test_new_download_appears_in_shelf_without_manual_verify(self):
        """用户视角：不点校验、不重启，下一次读书架就能看到下载结果。"""
        self._install_stub_download()
        self.plugin.library.list_manga()

        task_id = self._submit()
        self.assertTrue(self._wait_status(task_id, 'completed'))
        self._join(task_id)
        # 替身没有真的写图片；这里补上"下载已落盘"的等价物
        (self.root / '123456').mkdir()
        (self.root / '123456' / '001.jpg').write_bytes(b'\x89PNG\r\n\x1a\n')

        names = [item['folder_name'] for item in self.plugin.library.list_manga()]

        self.assertIn('123456', names, '下载完成的目录必须出现在书架上')

    def test_paused_task_does_not_fire_completion(self):
        def pause_midway(task) -> None:
            # 与 `DownloadCenter.pause` 的效果一致：置停止信号 + 改状态
            task._stop_event.set()
            task.status = 'paused'

        self._install_stub_download(on_run=pause_midway)
        task_id = self._submit()
        self.assertTrue(self._wait_status(task_id, 'paused'))
        self._join(task_id)

        self.assertEqual(self.resets, [], '暂停的任务不能触发完成回调')
        self.assertEqual(self.throttles, [])

    def test_not_completed_when_auto_start_is_off(self):
        self._install_stub_download()
        task_id = self._submit(auto_start=False)
        time.sleep(0.05)

        self.assertEqual(self._task(task_id).status, 'paused')
        self.assertEqual(self.resets, [], '未开始的任务不该触发回调')

    def test_callback_failure_does_not_flip_task_to_failed(self):
        def broken(_task_id: str) -> None:
            raise RuntimeError('失效入口炸了')

        self.plugin.downloads._on_completed = broken
        self._install_stub_download()
        task_id = self._submit()
        self.assertTrue(self._wait_status(task_id, 'completed'))
        self._join(task_id)

        self.assertEqual(self._task(task_id).status, 'completed',
                         '回调失败不能把已经下完的任务变成 failed（文件已在盘上）')


if __name__ == '__main__':      # pragma: no cover
    unittest.main()
