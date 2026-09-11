"""媒体库索引的并发可见性测试（docs/code-review.md §4.1-1）。

历史缺陷：扫描 worker 用 `merged = dict(self._items)` 做工作副本，却在每个
检查点把 `self._items` **重新绑定到 worker 仍在写的那个对象**；桥接/HTTP
线程同期遍历 `self._items.values()`（search / recent / all_audio…），≥2 个根
目录就可能撞 "dictionary changed size during iteration"，前端在扫描期间每
500ms 轮询会把窗口放大。

这里用确定性的方式守它：检查点当时发布的那个字典对象，之后内容不得再变化。
"""

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'media-player' / 'backend'
if str(PLUGIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(PLUGIN_BACKEND))

from shell.backend.tasks import BackgroundTask  # noqa: E402
from shell.backend.plugin_utils import load_sibling  # noqa: E402

mp = load_sibling(str(PLUGIN_BACKEND / 'main.py'), 'main', 'media_player_test')
MediaItem = mp.MediaItem


def _item(item_id: str, kind: str = 'audio', mtime: float = 1.0) -> dict:
    return MediaItem(
        id=item_id,
        path=f'C:/media/{item_id}.mp3',
        kind=kind,
        title=f'title-{item_id}',
        artist='artist',
        album='album',
        album_key='album|artist',
        mtime=mtime,
    ).to_dict()


class _ScanWorkerFixture:
    """只装配 _scan_worker 需要的最小状态，避免拉起整个插件。"""

    def __init__(self, dirs):
        plugin = object.__new__(mp.MediaPlayerPlugin)
        plugin._items = {}
        plugin._media_dirs = lambda: [str(d) for d in dirs]
        plugin.published = []          # (发布的字典对象, 发布时的键集合)
        plugin.saved_indexes = []
        plugin._migrate_legacy_state = lambda: None

        def _save_index(result):
            plugin.saved_indexes.append(result)

        plugin._save_index = _save_index
        plugin._thumb_cache = None
        # stats()/get_state() 需要 _state（真实构造时由 _load_state() 填充）
        plugin._state = {"favorites": [], "recent": [], "playlists": [], "playback": {}}
        self.plugin = plugin

        # 每次 _save_index 之前，_scan_worker 刚发布过一份快照：记下它
        original = plugin._save_index

        def spy(result):
            plugin.published.append((plugin._items, set(plugin._items)))
            return original(result)

        plugin._save_index = spy


class MediaIndexConcurrencyTests(unittest.TestCase):
    def _run_scan(self, fixture, items_by_dir, force=False):
        """用假 scan_media 驱动 _scan_worker（每个根目录给不同条目）。"""
        def fake_scan(root, cache=None, namespace=''):
            return {'items': [_item(f'{Path(root).name}-{n}') for n in items_by_dir[Path(root).name]]}

        task = BackgroundTask(kind='scan')
        with mock.patch.object(mp, 'scan_media', side_effect=fake_scan):
            fixture.plugin._scan_worker(task, force=force)
        return task

    def test_published_checkpoint_is_never_mutated_afterwards(self):
        """检查点发布的索引快照，在后续根目录继续扫描时不得再被改动。"""
        with tempfile.TemporaryDirectory() as td:
            dirs = [Path(td) / f'd{i}' for i in range(3)]
            for d in dirs:
                d.mkdir()
            fixture = _ScanWorkerFixture(dirs)
            self._run_scan(fixture, {f'd{i}': [1, 2] for i in range(3)})

            self.assertTrue(fixture.plugin.published, '至少应发布一次索引')
            for published, keys_at_publish in fixture.plugin.published:
                self.assertEqual(
                    set(published), keys_at_publish,
                    '检查点发布的快照之后又被改动了 —— 读取方会撞上 '
                    'dictionary changed size during iteration',
                )
            # 全部分目录的条目最终都要在索引里
            self.assertEqual(len(fixture.plugin._items), 6)

    def test_published_snapshots_are_distinct_objects(self):
        """每个检查点发布的必须是新字典，而不是同一个被反复修改的对象。"""
        with tempfile.TemporaryDirectory() as td:
            dirs = [Path(td) / f'd{i}' for i in range(3)]
            for d in dirs:
                d.mkdir()
            fixture = _ScanWorkerFixture(dirs)
            self._run_scan(fixture, {f'd{i}': [1, 2] for i in range(3)})
            objects = [id(published) for published, _ in fixture.plugin.published]
            self.assertEqual(len(objects), len(set(objects)), '不同检查点发布了同一个字典对象')

    def test_reader_thread_can_iterate_during_scan(self):
        """扫描进行中持续读取索引不得抛异常（放大的真实场景）。"""
        with tempfile.TemporaryDirectory() as td:
            dirs = [Path(td) / f'd{i}' for i in range(4)]
            for d in dirs:
                d.mkdir()
            fixture = _ScanWorkerFixture(dirs)
            plugin = fixture.plugin
            errors = []
            stop = threading.Event()

            def reader():
                while not stop.is_set():
                    try:
                        plugin.search('title')
                        plugin.recent(10)
                        plugin.all_audio()
                        plugin.stats()
                    except Exception as e:  # pragma: no cover - 出错即失败
                        errors.append(e)
                        return

            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            try:
                self._run_scan(fixture, {f'd{i}': list(range(40)) for i in range(4)})
            finally:
                stop.set()
                thread.join(timeout=5)

            self.assertEqual(errors, [], f'读取线程在扫描期间出错: {errors[:1]}')


if __name__ == '__main__':
    unittest.main()
