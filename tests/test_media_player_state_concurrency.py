"""状态文件并发写的回归：一次换曲会连发多条桥调用，各跑一条线程。

背景：`media_save_playback` 与 `media_update_recent` 在同一个 tick 里发出，由
pywebview 各起一条线程执行（`webview.util.js_bridge_call` 注明回调在独立线程）。
`_save_state` 过去没有串行化、临时文件名固定为 `media_state.tmp`：一个写者还在
写临时文件、另一个已经 `os.replace`，Windows 上就报 WinError 32（"另一个程序
正在使用此文件"），这一次写入被吞掉。日志里的表现是每次换曲偶发一条
"保存状态失败: [WinError 32] ... 'media_state.tmp' -> 'media_state.json'"。

这里钉住两条不变量：
1. 同一时刻只有一个写者在写（把 `fsync` 放慢放大窗口后仍必须是 1）；
2. 改名失败时不残留临时文件，已有状态文件不被破坏。
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.settings_store import SettingsStore

_PLUGIN = PROJECT_ROOT / 'plugins' / 'media-player' / 'backend' / 'main.py'


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location('media_player_concurrency_test', str(_PLUGIN))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StateWriteConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.media = self.tmp / 'media'
        self.media.mkdir(parents=True, exist_ok=True)
        self.module = _load_plugin_module()
        self.cls = self.module.MediaPlayerPlugin
        self.cls._resolved_config = {'media_roots': str(self.media)}
        self.addCleanup(setattr, self.cls, '_resolved_config', {})

    def _plugin(self):
        plugin = self.cls({'name': 'media-player'},
                          {'directories': {'data_root': str(self.tmp / 'shell-data')}})
        plugin._settings_store = SettingsStore(str(self.tmp / 'settings'))
        self.addCleanup(plugin.on_unload)
        return plugin

    def test_concurrent_saves_are_serialized(self):
        plugin = self._plugin()
        # 预热：把 playback 的键建齐，竞赛期间只剩"替换值"，避免 json.dumps 撞上
        # 字典增键（那是另一个问题，不该让这条用例变成偶发失败）
        plugin.save_playback('warmup', 'none', False, 1.0, 'video', 0)
        plugin.update_recent('warmup')

        inside = {'value': 0, 'peak': 0}
        guard = threading.Lock()
        real_fsync = os.fsync

        def slow_fsync(fd):
            # 放大"临时文件被打开"的窗口：真实磁盘上这段只有几毫秒，撞车因此偶发
            with guard:
                inside['value'] += 1
                inside['peak'] = max(inside['peak'], inside['value'])
            try:
                time.sleep(0.02)
                return real_fsync(fd)
            finally:
                with guard:
                    inside['value'] -= 1

        log = plugin._save_state.__func__.__globals__['log']
        threads_n, per_thread = 4, 3
        barrier = threading.Barrier(threads_n)

        def save_playback_worker():
            barrier.wait()
            for _ in range(per_thread):
                plugin.save_playback('item', 'all', False, 1.0, 'video', 0)

        def recent_worker():
            barrier.wait()
            for _ in range(per_thread):
                plugin.update_recent('item')

        with mock.patch('os.fsync', slow_fsync), \
                mock.patch.object(log, 'error') as error_log:
            threads = [threading.Thread(target=save_playback_worker),
                       threading.Thread(target=recent_worker)]
            threads += [threading.Thread(target=save_playback_worker)
                        for _ in range(threads_n - 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(inside['peak'], 1, '同一时刻只允许一个写者进入写盘')
        self.assertEqual(error_log.call_args_list, [], '并发写不应产生失败日志')
        leftovers = sorted(p.name for p in plugin._state_file.parent.iterdir()
                           if p.name.endswith('.tmp'))
        self.assertEqual(leftovers, [], '临时文件要已被替换掉')
        data = json.loads(plugin._state_file.read_text(encoding='utf-8'))
        self.assertEqual(data['playback']['item_id'], 'item')
        self.assertEqual(data['recent'][0]['id'], 'item')

    def test_failed_replace_leaves_no_temp_file(self):
        plugin = self._plugin()
        plugin._state['favorites'] = ['a']
        plugin._save_state()
        before = plugin._state_file.read_text(encoding='utf-8')

        original_attempts = self.module._REPLACE_ATTEMPTS
        self.module._REPLACE_ATTEMPTS = 0
        self.addCleanup(setattr, self.module, '_REPLACE_ATTEMPTS', original_attempts)
        with mock.patch('os.replace',
                        side_effect=PermissionError(13, '另一个程序正在使用此文件')):
            plugin._save_state()

        self.assertEqual(plugin._state_file.read_text(encoding='utf-8'), before,
                         '改名失败时已有状态文件必须原样保留')
        leftovers = sorted(p.name for p in plugin._state_file.parent.iterdir()
                           if p.name.endswith('.tmp'))
        self.assertEqual(leftovers, [], '失败路径不能把临时文件留在目录里')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
