"""媒体库索引的并发可见性测试（统一刷新基建版）。

历史缺陷：扫描 worker 用一份工作副本累积条目，却在检查点把 `self._items`
**重新绑定到还在写的那个字典**；桥接/HTTP 线程同期遍历 `self._items.values()`
（search / recent / all_audio …），≥2 个根目录就可能撞
"dictionary changed size during iteration"。

接入统一刷新基建后，"写索引"的位置从扫描 worker 挪到了三个钩子
（`_freshness_derive` / `_freshness_prune` / `_freshness_audit`），不变量不变：
**每次发布必须是新字典，发布之后内容不得再变化**。这里用确定性的方式守它：
检查点当时发布的那个字典对象，之后内容不得再变化；并让读取线程在整趟同步/校验
期间持续遍历索引。

运行：
    python -m unittest tests.test_media_player_index -v
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'media-player' / 'backend'
if str(PLUGIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(PLUGIN_BACKEND))

from shell.backend.freshness import engine_for
from shell.backend.plugin_utils import load_sibling

mp = load_sibling(str(PLUGIN_BACKEND / 'main.py'), 'main', 'media_player_index_test')


def _write_tracks(root: Path, count: int, prefix: str = 't') -> None:
    """造 count 个最小 mp3 文件（内容不重要：读标签失败会被容错成默认值）。"""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / f'{prefix}{i:03d}.mp3').write_bytes(b'ID3\x03\x00\x00\x00\x00\x00\x00')


class MediaIndexPublishTests(unittest.TestCase):
    """索引发布的不变量（统一刷新基建的三个钩子都算"发布点"）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'media'
        self.config = {'directories': {'data_root': str(self.root)}}
        mp.MediaPlayerPlugin._resolved_config = {'media_roots': str(self.root)}
        self.addCleanup(setattr, mp.MediaPlayerPlugin, '_resolved_config', {})
        self.plugin = mp.MediaPlayerPlugin({'name': 'media-player'}, self.config)
        self.addCleanup(self.plugin.on_unload)
        self.engine = engine_for(self.plugin)
        self.assertIsNotNone(self.engine, 'media-player 必须声明 freshness_spec()')

        self.published = []
        original = self.plugin._publish_items

        def spy(items):
            self.published.append((items, set(items)))
            return original(items)

        self.plugin._publish_items = spy

    def _run_pass(self, verify: bool = False):
        self.engine.spec.min_sync_interval = 0.0
        return self.engine.verify() if verify else self.engine.sync('', force=True)

    # ----- 契约 -----

    def test_published_snapshot_is_never_mutated_afterwards(self):
        _write_tracks(self.root / 'd0', 3)
        _write_tracks(self.root / 'd1', 3)

        self._run_pass()

        self.assertTrue(self.published, '至少应发布一次索引')
        for published, keys_at_publish in self.published:
            self.assertEqual(
                set(published), keys_at_publish,
                '发布的快照之后又被改动了 —— 读取方会撞上 '
                'dictionary changed size during iteration',
            )

    def test_published_snapshots_are_distinct_objects(self):
        _write_tracks(self.root / 'd0', 3)
        _write_tracks(self.root / 'd1', 3)

        self._run_pass()
        (self.root / 'd1' / 't002.mp3').unlink()
        self._run_pass(verify=True)

        objects = [id(published) for published, _ in self.published]
        self.assertGreater(len(objects), 1)
        self.assertEqual(len(objects), len(set(objects)), '不同发布点复用了同一个字典对象')

    def test_reader_thread_can_iterate_during_pass(self):
        for i in range(4):
            _write_tracks(self.root / f'd{i}', 25)
        errors = []
        stop = threading.Event()

        def reader():
            # 1ms 一次：真实调用方是前端 500ms 轮询；无 sleep 的紧循环会长时间占着
            # GIL，把被测线程饿到几十秒（实测 160 文件从 0.3s 变 64s），
            # 那是测试写法问题，不是被测代码的问题。
            while not stop.is_set():
                try:
                    self.plugin.search('t0')
                    self.plugin.recent(10)
                    self.plugin.all_audio()
                    self.plugin.stats()
                except Exception as e:  # pragma: no cover - 出错即失败
                    errors.append(e)
                    return
                time.sleep(0.001)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            self._run_pass(verify=True)
        finally:
            stop.set()
            thread.join(timeout=5)

        self.assertEqual(errors, [], f'读取线程在刷新期间出错: {errors[:1]}')

    def test_deleted_media_leaves_the_index_after_verify(self):
        """回归：旧增量扫描只增不减，删除的媒体永久留在索引里。"""
        _write_tracks(self.root / 'd0', 3)
        self._run_pass()
        self.assertEqual(len(self.plugin._items), 3)

        (self.root / 'd0' / 't001.mp3').unlink()
        report = self._run_pass(verify=True)

        self.assertEqual(report['removed'], 1)
        self.assertEqual(sorted(Path(i.path).name for i in self.plugin._items.values()),
                         ['t000.mp3', 't002.mp3'])

    def test_later_added_folder_cover_refreshes_has_cover(self):
        """回归：后补一张 folder.jpg 只改目录内容，不改任何媒体文件指纹。"""
        _write_tracks(self.root / 'd0', 2)
        self._run_pass()
        self.assertFalse(any(i.has_cover for i in self.plugin._items.values()))

        (self.root / 'd0' / 'folder.jpg').write_bytes(b'\xff\xd8\xff' + b'x' * 16)
        self._run_pass()

        self.assertTrue(all(i.has_cover for i in self.plugin._items.values()))

    def test_index_file_is_written_once_per_pass(self):
        _write_tracks(self.root / 'd0', 2)
        _write_tracks(self.root / 'd1', 2)
        _write_tracks(self.root / 'd2', 2)

        self._run_pass()

        self.assertTrue(self.plugin._cache_file.exists(), '整趟结束必须落盘索引')
        self.assertFalse(self.plugin._index_dirty, '落盘后不应仍标记为脏')


if __name__ == '__main__':
    unittest.main()
