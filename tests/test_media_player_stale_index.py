"""媒体索引的「读回快照」语义与封面版本号。

两件事各一组用例：

1. **读回的索引不是权威结论。** `media_index.json` 是上一次运行写下的观察结果，文件在
   两次运行之间可能被删除或移动。`MediaIndex.load()` 记下 `loaded_at` / `loaded_version`
   并把 `restored` 置真，此时 `get()` 会核对条目路径是否还在 —— 界面按 id 取条目
   （`media_get_item` / `media_get_items` / 收藏与最近播放的投影）不会拿到指向不存在
   文件的条目。同步或校验发布新快照后 `restored` 变假，查询回到纯内存字典查找。
   列表类接口仍直接遍历 `items`，幽灵条目在下一趟遍历把目录对账掉之前会留在列表里。

2. **`cover_mtime` 是封面字节的版本时刻。** 有目录封面图时取该文件自身的 mtime，
   没有时退回条目 mtime（此时封面来自内嵌图或视频抽帧）。替换封面文件后同步会重算它，
   前端的 `/thumbs` URL 版本号因此能前进。
"""

import json
import os
import sys
import tempfile
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

mp = load_sibling(str(PLUGIN_BACKEND / 'main.py'), 'main', 'media_player_stale_index_test')
scanner_module = load_sibling(
    str(PLUGIN_BACKEND / 'scanner.py'), 'scanner', 'media_player_scanner_test')
models_module = load_sibling(
    str(PLUGIN_BACKEND / 'models.py'), 'models', 'media_player_models_test')

MP3_BYTES = b'ID3\x03\x00\x00\x00\x00\x00\x00'


def _track(root: Path, rel: str, mtime: float = 1_700_000_000.0) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MP3_BYTES)
    os.utime(path, (mtime, mtime))
    return path


def _cover(directory: Path, name: str = 'cover.jpg', mtime: float = 1_700_005_000.0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b'\xff\xd8\xff' + b'x' * 16)
    os.utime(path, (mtime, mtime))
    return path


class _PluginFixture(unittest.TestCase):
    """真实插件 + 临时媒体根；每个用例自己决定写哪些文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'media'
        self.root.mkdir(parents=True, exist_ok=True)
        self.plugin = self._make_plugin()
        self.engine = self._engine(self.plugin)

    def _make_plugin(self):
        mp.MediaPlayerPlugin._resolved_config = {'media_roots': str(self.root)}
        self.addCleanup(setattr, mp.MediaPlayerPlugin, '_resolved_config', {})
        plugin = mp.MediaPlayerPlugin(
            {'name': 'media-player'},
            {'directories': {'data_root': str(Path(self._tmp.name) / 'shell-data')}})
        self.addCleanup(plugin.on_unload)
        return plugin

    def _engine(self, plugin):
        engine = engine_for(plugin)
        self.assertIsNotNone(engine, 'media-player 必须声明 freshness_spec()')
        assert engine is not None
        engine.spec.min_sync_interval = 0.0
        return engine

    def _sync(self) -> dict:
        return self.engine.sync('', force=True)

    def _items(self) -> list:
        return list(self.plugin.index.items.values())


class StaleIndexGhostTests(_PluginFixture):
    def test_load_records_where_the_snapshot_came_from(self):
        item = self.plugin.index
        item.save(time.time())
        self.assertTrue(self.plugin.index.cache_file.exists())
        self.assertEqual(item.loaded_at, 0.0, '还没读过盘')
        self.assertFalse(item.restored)

        item.load()

        self.assertGreater(item.loaded_at, 0.0)
        self.assertEqual(item.loaded_version, scanner_module.INDEX_VERSION)
        self.assertFalse(item.restored, '空索引读回后没有可核对的条目')

        _track(self.root, 'd0/a.mp3')
        self._sync()
        self.assertFalse(item.restored, '同步发布的快照是本次遍历的结果')
        self.assertTrue(item.authoritative)

        item.load()
        self.assertTrue(item.restored, '从文件读回的条目需要逐条核对路径')
        self.assertFalse(item.authoritative)

    def test_ghost_entry_is_not_served_by_id_after_restart(self):
        source = _track(self.root, 'd0/a.mp3')
        self._sync()
        [indexed] = self._items()
        item_id = indexed.id
        self.plugin.on_unload()

        source.unlink()
        other = self._make_plugin()
        self.addCleanup(other.on_unload)

        self.assertTrue(other.index.restored, '新实例应从落盘索引读回条目')
        self.assertIn(item_id, other.index.items, '落盘快照本身仍带着这条记录')
        self.assertIsNone(other.index.get(item_id), '文件已不在，按 id 取不到')
        self.assertEqual(other.library.get_item(item_id), {})
        self.assertEqual(other.collections.get_items([item_id]), [])

    def test_verify_drops_ghosts_and_clears_restored(self):
        source = _track(self.root, 'd0/a.mp3')
        self._sync()
        [indexed] = self._items()
        item_id = indexed.id
        self.plugin.on_unload()

        source.unlink()
        other = self._make_plugin()
        self.addCleanup(other.on_unload)
        engine = self._engine(other)
        report = engine.verify()

        self.assertEqual(report.get('removed'), 1, report)
        self.assertNotIn(item_id, other.index.items)
        self.assertFalse(other.index.restored)
        self.assertTrue(other.index.authoritative)
        self.assertIsNone(other.index.get(item_id))

    def test_snapshot_published_in_this_run_is_not_checked_against_disk(self):
        item = models_module.MediaItem(
            id='deadbeefdeadbeef', path=str(self.root / 'gone' / 'x.mp3'),
            kind='audio', title='x', artist='y', album='z', album_key='k')
        self.plugin.index.publish({item.id: item})

        self.assertIs(self.plugin.index.get(item.id), item,
                      '本次运行发布的快照不做存在性核对（否则每条都 stat）')


class CoverMtimeTests(_PluginFixture):
    def test_folder_cover_defines_the_version(self):
        directory = self.root / 'd0'
        _track(self.root, 'd0/a.mp3', mtime=1_700_000_000.0)
        cover = _cover(directory, mtime=1_700_005_000.0)

        self._sync()

        [item] = self._items()
        self.assertTrue(item.has_cover)
        self.assertAlmostEqual(item.cover_mtime, cover.stat().st_mtime, places=3)
        self.assertNotAlmostEqual(item.cover_mtime, item.mtime, places=3)
        self.assertAlmostEqual(item.to_dict()['cover_mtime'], cover.stat().st_mtime, places=3)

    def test_replaced_cover_advances_the_version(self):
        directory = self.root / 'd0'
        _track(self.root, 'd0/a.mp3')
        cover = _cover(directory, mtime=1_700_005_000.0)
        self._sync()
        [first] = self._items()
        before = first.cover_mtime

        cover.write_bytes(b'\xff\xd8\xff' + b'y' * 32)
        os.utime(cover, (1_700_009_000.0, 1_700_009_000.0))
        # 替换同名文件不改目录 mtime；目录指纹变化才会让本趟真的重扫该目录
        os.utime(directory, (1_700_009_100.0, 1_700_009_100.0))
        self._sync()

        [second] = self._items()
        self.assertGreater(second.cover_mtime, before)
        self.assertAlmostEqual(second.cover_mtime, 1_700_009_000.0, places=3)

    def test_entry_without_folder_cover_uses_its_own_mtime(self):
        track = _track(self.root, 'd0/a.mp3', mtime=1_700_000_000.0)

        self._sync()

        [item] = self._items()
        self.assertFalse(item.has_cover)
        self.assertAlmostEqual(item.cover_mtime, track.stat().st_mtime, places=3)
        self.assertAlmostEqual(item.cover_mtime, item.mtime, places=3)

    def test_video_directory_cover_defines_the_version(self):
        video = self.root / 'v0' / 'clip.mp4'
        video.parent.mkdir(parents=True)
        video.write_bytes(b'\x00' * 16)
        os.utime(video, (1_700_001_000.0, 1_700_001_000.0))
        cover = _cover(video.parent, name='poster.jpg', mtime=1_700_006_000.0)

        self._sync()

        [item] = self._items()
        self.assertEqual(item.kind, 'video')
        self.assertAlmostEqual(item.cover_mtime, cover.stat().st_mtime, places=3)

    def test_album_summary_carries_cover_version(self):
        directory = self.root / 'd0'
        _track(self.root, 'd0/a.mp3')
        cover = _cover(directory, mtime=1_700_005_000.0)

        self._sync()

        [album] = self.plugin.library.audio_albums()
        self.assertTrue(album['cover_item_id'])
        self.assertAlmostEqual(album['cover_mtime'], cover.stat().st_mtime, places=3)

    def test_cover_version_survives_index_round_trip(self):
        directory = self.root / 'd0'
        _track(self.root, 'd0/a.mp3')
        cover = _cover(directory, mtime=1_700_005_000.0)
        self._sync()
        [indexed] = self._items()
        expected = indexed.cover_mtime
        self.plugin.on_unload()

        with open(self.plugin.index.cache_file, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        raw = payload['items'][0]
        self.assertIn('cover_mtime', raw, '落盘索引必须带版本时刻')

        other = self._make_plugin()
        self.addCleanup(other.on_unload)
        [reloaded] = list(other.index.items.values())
        self.assertAlmostEqual(reloaded.cover_mtime, expected, places=3)
        self.assertAlmostEqual(reloaded.cover_mtime, cover.stat().st_mtime, places=3)

    def test_old_cache_payload_falls_back_to_entry_mtime(self):
        raw = {
            'id': 'abc', 'path': '/x.mp3', 'kind': 'audio', 'title': 't',
            'artist': 'a', 'album': 'b', 'album_key': 'k', 'mtime': 1234.5,
        }
        item = models_module.MediaItem.from_cache(raw)
        self.assertAlmostEqual(item.cover_mtime, 1234.5, places=3)


class CoverMtimeHelperTests(unittest.TestCase):
    def test_helper_prefers_cover_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            cover = _cover(directory, mtime=1_600_000_000.0)
            self.assertAlmostEqual(
                scanner_module.MediaScanner.cover_mtime(cover, 1_500_000_000.0),
                cover.stat().st_mtime, places=3)

    def test_helper_falls_back_when_cover_is_missing(self):
        self.assertAlmostEqual(
            scanner_module.MediaScanner.cover_mtime(None, 1_500_000_000.0),
            1_500_000_000.0, places=3)


if __name__ == '__main__':      # pragma: no cover
    unittest.main()
