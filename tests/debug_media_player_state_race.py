"""手工复现 media-player 状态文件的并发写（日志里的 "保存状态失败: [WinError 32]"）。

用真实的 `MediaPlayerPlugin` 和真实的状态文件体量（几十 KB），多线程同时调
`_save_state()`，观察是否出现 `WinError 32` / `Errno 13`。修复前的实测：
2 线程 × 200 次写盘里 140 次 WinError 32 + 66 次 Errno 13；修复后为 0。

为什么不是 unittest：这条只用来现场观察时序（放大窗口、看失败率），断言由
`tests/test_media_player_state_concurrency.py` 负责。

用法：python tests/debug_media_player_state_race.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.settings_store import SettingsStore

PLUGIN_PATH = PROJECT_ROOT / 'plugins' / 'media-player' / 'backend' / 'main.py'


def load_module():
    spec = importlib.util.spec_from_file_location('media_player_state_race', str(PLUGIN_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_plugin(module, tmp):
    media = tmp / 'media'
    media.mkdir()
    module.MediaPlayerPlugin._resolved_config = {'media_roots': str(media)}
    plugin = module.MediaPlayerPlugin(
        {'name': 'media-player'},
        {'directories': {'data_root': str(tmp / 'shell-data')}})
    plugin._settings_store = SettingsStore(str(tmp / 'settings'))
    plugin._state['playlists'] = [
        {'id': f'p{i}', 'name': f'歌单 {i}', 'item_ids': [f'x{j}' for j in range(30)],
         'created_at': '2026-01-01 00:00:00', 'updated_at': '2026-01-01 00:00:00'}
        for i in range(24)]
    plugin._state['favorites'] = [f'f{i}' for i in range(150)]
    plugin._state['recent'] = [{'id': f'r{i}', 'played_at': '2026-01-01 00:00:00'}
                               for i in range(50)]
    plugin._save_state()
    return plugin


def race(plugin, threads_n, per_thread, fsync_delay=0.0):
    """threads_n 条线程尽量同时开始，各写 per_thread 次，返回 (错误列表, 耗时)。"""
    log = plugin._save_state.__func__.__globals__['log']
    errors = []
    original_error = log.error
    log.error = lambda msg, *args, **kwargs: errors.append(str(msg))

    real_fsync = os.fsync
    if fsync_delay:
        def slow_fsync(fd):
            time.sleep(fsync_delay)   # 放大"临时文件被打开"的窗口，等价于慢盘
            return real_fsync(fd)
        os.fsync = slow_fsync

    barrier = threading.Barrier(threads_n)

    def worker():
        barrier.wait()
        for _ in range(per_thread):
            plugin._save_state()

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    if fsync_delay:
        os.fsync = real_fsync
    log.error = original_error
    return errors, elapsed


def main():
    module = load_module()
    with tempfile.TemporaryDirectory() as tmpdir:
        plugin = make_plugin(module, Path(tmpdir))
        print(f'状态文件 {plugin._state_file.name}: {plugin._state_file.stat().st_size} B\n')

        cases = [
            ('2 线程 × 200 次（原速）', 2, 200, 0.0),
            ('2 线程 × 200 次（fsync +5ms）', 2, 200, 0.005),
            ('4 线程 × 200 次（fsync +5ms）', 4, 200, 0.005),
        ]
        for label, threads_n, per_thread, delay in cases:
            errors, elapsed = race(plugin, threads_n, per_thread, delay)
            win32 = [e for e in errors if 'WinError 32' in e]
            denied = [e for e in errors if 'Errno 13' in e]
            total = threads_n * per_thread
            print(f'{label}: {total} 次写盘 / {elapsed:.2f}s')
            print(f'  WinError 32: {len(win32)}   Errno 13: {len(denied)}   其它: {len(errors) - len(win32) - len(denied)}')
            if errors:
                print(f'  样例: {errors[0]}')
            try:
                json.loads(plugin._state_file.read_text(encoding='utf-8'))
                print('  落盘结果: 可解析')
            except Exception as exc:
                print(f'  落盘结果: 无法解析（{exc}）')
            leftovers = sorted(p.name for p in plugin._state_file.parent.iterdir()
                               if p.name.endswith('.tmp'))
            print(f'  残留临时文件: {leftovers}\n')
        plugin.on_unload()


if __name__ == '__main__':
    main()
