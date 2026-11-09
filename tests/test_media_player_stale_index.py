"""媒体索引的「读回快照」语义、落盘原子性与封面版本号。

三件事各一组用例：

1. **读回的索引不是权威结论。** `media_index.json` 是上一次运行写下的观察结果，文件在
   两次运行之间可能被删除或移动。`MediaIndex.load()` 记下 `loaded_at` / `loaded_version`
   并把 `restored` 置真，此时 `get()` 会核对条目路径是否还在 —— 界面按 id 取条目
   （`media_get_item` / `media_get_items` / 收藏与最近播放的投影）不会拿到指向不存在
   文件的条目。`publish()` **不**清 `restored`（发布只说明"这份条目来自某次遍历"）；
   只有插件的 `on_pass_end` 在整趟非 partial、无读取错误时调 `mark_authoritative()`
   才清。预算用尽的部分同步因此仍逐条核对路径。
   列表类接口仍直接遍历 `items`，幽灵条目在下一趟遍历把目录对账掉之前会留在列表里。

2. **`cover_mtime` 是封面字节的版本时刻。** 有目录封面图时取该文件自身的 mtime，
   没有时退回条目 mtime（此时封面来自内嵌图或视频抽帧）。替换封面文件后同步会重算它，
   前端的 `/thumbs` URL 版本号因此能前进。

3. **索引落盘是原子写。** `save()` 走同目录临时文件 + `os.replace` 并串行化在实例锁上：
   `on_pass_end`（每趟结束）与 `on_unload`（退出）来自不同线程，直接覆写会留下截断的
   JSON，下次启动解析失败退化成空索引、整库重建。
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

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
metadata_module = load_sibling(
    str(PLUGIN_BACKEND / 'metadata.py'), 'metadata', 'media_player_metadata_test')

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

    def _make_plugin(self, roots=None):
        configured = roots if roots is not None else [self.root]
        mp.MediaPlayerPlugin._resolved_config = {
            'media_roots': '\n'.join(str(root) for root in configured)}
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
        self.assertFalse(item.restored, '整趟同步走完后由 on_pass_end 宣布索引权威')
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

    def test_partial_sync_does_not_make_the_snapshot_authoritative(self):
        """预算用尽的部分遍历只覆盖一部分目录：落盘快照仍要逐条核对路径。"""
        source = _track(self.root, 'd0/a.mp3')
        self._sync()
        [indexed] = self._items()
        item_id = indexed.id
        self.plugin.on_unload()

        source.unlink()
        other = self._make_plugin()
        self.addCleanup(other.on_unload)
        engine = self._engine(other)
        self.assertTrue(other.index.restored, '新实例先从落盘索引读回条目')

        # 预算 0：这一趟一个目录都不处理，属于 partial（"还有活没干"）
        engine.spec.max_dirs_per_sync = 0
        report = engine.sync('', force=True)

        self.assertTrue(report.get('partial'), report)
        self.assertTrue(other.index.restored,
                        '部分遍历没有走完，不能宣布索引权威')
        self.assertIsNone(other.index.get(item_id), '幽灵条目不能被部分遍历顺手变成权威')

    def test_scoped_sync_does_not_make_the_snapshot_authoritative(self):
        """带子目录作用域的同步只覆盖一棵子树：索引仍要逐条核对路径，直到全根整趟。"""
        source = _track(self.root, 'd0/a.mp3')
        self._sync()
        [indexed] = self._items()
        item_id = indexed.id
        self.plugin.on_unload()

        source.unlink()
        other = self._make_plugin()
        self.addCleanup(other.on_unload)
        engine = self._engine(other)
        self.assertTrue(other.index.restored, '新实例先从落盘索引读回条目')

        # 同根的另一个目录有新增内容：这趟作用域同步真的干了活，但没走全根
        _track(self.root, 'd1/b.mp3')
        report = engine.sync('d1', force=True)

        self.assertEqual(report.get('action'), 'sync', report)
        self.assertEqual(report.get('scope'), 'd1', report)
        self.assertTrue(report.get('scoped'), '子目录作用域必须在报告里标出来')
        self.assertFalse(report.get('partial'), report)
        self.assertEqual(report.get('errors'), [], report)
        self.assertTrue(other.index.restored,
                        '子目录作用域的同步没有覆盖全部根，不能宣布索引权威')
        self.assertIsNone(other.index.get(item_id), '幽灵条目仍要逐条核对路径')

        # 同一进程里再走一趟全根：整趟走完才宣布权威
        full = engine.sync('', force=True)
        self.assertFalse(full.get('scoped'), full)
        self.assertFalse(other.index.restored)
        self.assertTrue(other.index.authoritative)

    def test_unreachable_root_keeps_the_snapshot_unauthoritative(self):
        """不可达的根里的条目不是本次观察的结果：全根同步也不能宣布索引权威。"""
        extra = Path(self._tmp.name) / 'media2'
        _track(self.root, 'd0/a.mp3')
        _track(extra, 'd0/b.mp3')

        first = self._make_plugin(roots=[self.root, extra])
        first_engine = engine_for(first)
        assert first_engine is not None
        first_engine.spec.min_sync_interval = 0.0
        first_engine.sync('', force=True)
        ghost = next(item for item in first.index.items.values() if 'media2' in item.path)
        first.on_unload()

        # 外接盘拔掉：第二个根整体不可达（引擎不遍历、也不清理它的目录行）
        shutil.rmtree(extra)
        other = self._make_plugin(roots=[self.root, extra])
        other_engine = engine_for(other)
        assert other_engine is not None
        other_engine.spec.min_sync_interval = 0.0
        self.assertTrue(other.index.restored, '新实例先从落盘索引读回条目')
        self.assertIsNone(other.index.get(ghost.id), '不可达根里的条目还没核对过路径')

        report = other_engine.sync('', force=True)

        unreachable = report.get('unreachable_roots') or []
        self.assertEqual([Path(root).name for root in unreachable], ['media2'], report)
        self.assertFalse(report.get('partial'), report)
        self.assertEqual(report.get('errors'), [], report)
        self.assertTrue(other.index.restored,
                        '有不可达的根时这一趟没有覆盖全部条目，不能宣布索引权威')
        self.assertFalse(other.index.authoritative)
        self.assertIsNone(other.index.get(ghost.id),
                          '不可达根里的幽灵条目仍要逐条核对路径')

    def test_publish_alone_keeps_restored_until_mark_authoritative(self):
        """`publish` 只换快照，不宣布"整趟走完"；权威由 `mark_authoritative` 单独宣布。"""
        _track(self.root, 'd0/a.mp3')
        self._sync()
        [indexed] = self._items()
        self.plugin.on_unload()

        # 直接往落盘索引里塞一条"文件已不在"的幽灵条目（上次运行后被删）
        with open(self.plugin.index.cache_file, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        ghost = dict(payload['items'][0])
        ghost['id'] = 'deadbeefdeadbeef'
        ghost['path'] = str(self.root / 'gone' / 'x.mp3')
        payload['items'].append(ghost)
        with open(self.plugin.index.cache_file, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False)

        other = self._make_plugin()
        self.addCleanup(other.on_unload)
        self.assertTrue(other.index.restored)
        self.assertIsNone(other.index.get(ghost['id']))

        # 局部遍历拼出来的新快照里仍然带着上次运行的条目：发布不能清 restored
        other.index.publish(dict(other.index.items))
        self.assertTrue(other.index.restored, 'publish 不宣布"整趟走完"')
        self.assertIsNone(other.index.get(ghost['id']))

        other.index.mark_authoritative()
        self.assertFalse(other.index.restored)
        self.assertTrue(other.index.authoritative)
        self.assertIsNotNone(other.index.get(ghost['id']),
                             '整趟之后由对账清条目，get 不再逐条核对')

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

    def test_replaced_folder_cover_returns_new_thumb_bytes(self):
        """替换 folder.jpg 后 `/thumbs` 必须重新生成：版本号前进不等于字节更新。

        `ThumbCache` 的失效判据是媒体文件的 mtime/size，同名替换封面不改媒体文件，
        旧缓存行会被判成有效并原样返回 —— 所以 `_refresh_covers` 要一起删缓存行。
        """
        directory = self.root / 'd0'
        _track(self.root, 'd0/a.mp3')
        cover = _cover(directory, mtime=1_700_005_000.0)
        self._sync()
        [item] = self._items()
        before_version = item.cover_mtime
        first = self.plugin.covers.get_thumb_data(item.id)
        self.assertIsNotNone(first, '前置条件：封面应能生成')
        assert first is not None

        cover.write_bytes(b'\xff\xd8\xff' + b'B' * 96)
        os.utime(cover, (1_700_009_000.0, 1_700_009_000.0))
        # 替换同名文件不改目录 mtime；目录指纹变化才会让本趟真的重扫该目录
        os.utime(directory, (1_700_009_100.0, 1_700_009_100.0))
        report = self._sync()

        self.assertGreater(report.get('changed', 0), 0, report)
        [refreshed] = self._items()
        self.assertGreater(refreshed.cover_mtime, before_version, '版本号要前进')
        second = self.plugin.covers.get_thumb_data(refreshed.id)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertNotEqual(second[0], first[0],
                            '替换目录封面后缩略图缓存必须失效并重新生成')

    def test_removed_folder_cover_drops_the_cached_thumb(self):
        """删掉 folder.jpg 后缓存行也要失效（否则留着旧图，has_cover 变假也对不上）。"""
        directory = self.root / 'd0'
        _track(self.root, 'd0/a.mp3')
        cover = _cover(directory, mtime=1_700_005_000.0)
        self._sync()
        [item] = self._items()
        self.assertIsNotNone(self.plugin.covers.get_thumb_data(item.id))

        cover.unlink()
        os.utime(directory, (1_700_009_100.0, 1_700_009_100.0))
        self._sync()

        [refreshed] = self._items()
        self.assertFalse(refreshed.has_cover, '没有内嵌封面且目录封面已删，应判定无封面')
        self.assertIsNone(self.plugin.covers.get_thumb_data(refreshed.id),
                          '封面已删除时不能继续返回旧缓存')

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


class CoverSourceConsistencyTests(unittest.TestCase):
    """同一目录多个候选封面时，`has_cover`/`cover_mtime` 与实际字节必须取同一个文件。"""

    def test_scanner_and_metadata_pick_the_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'folder.jpg').write_bytes(b'folder-cover')
            (directory / 'cover.jpg').write_bytes(b'cover-cover')
            scanner = scanner_module.MediaScanner(models_module, metadata_module, None, None)

            picked = scanner.find_cover(directory)
            data = metadata_module.MetadataReader._find_folder_cover(
                directory, scanner.cover_names)

            self.assertIsNotNone(picked)
            assert picked is not None
            self.assertEqual(picked.name, 'cover.jpg', '优先序必须固定，不能按无序集合迭代')
            self.assertEqual(data, picked.read_bytes(),
                             '版本号的来源与实际字节必须是同一个封面文件')


class StaleTempCleanupTests(_PluginFixture):
    """硬杀留下的 `.<文件名>.<随机>.tmp` 在加载时清掉（异常路径自己会删，硬杀不会）。"""

    def test_index_load_removes_crashed_temp_files(self):
        index = self.plugin.index
        index.save(time.time())
        stale = index.cache_file.with_name(f'.{index.cache_file.name}.crashed.tmp')
        stale.write_text('{"version": 5, ', encoding='utf-8')

        index.load()

        self.assertFalse(stale.exists())
        self.assertTrue(index.cache_file.exists())

    def test_state_load_removes_crashed_temp_files(self):
        self.plugin.state.save()
        state_file = self.plugin.paths.state_file
        stale = state_file.with_name(f'.{state_file.name}.crashed.tmp')
        stale.write_text('{"favorites": [', encoding='utf-8')

        self.plugin.state.load()

        self.assertFalse(stale.exists())
        self.assertTrue(state_file.exists())


class ClearFileLockTests(_PluginFixture):
    """`clear_file` 与 `save` 共用实例锁：删除必须排在在途写之后。"""

    def test_clear_file_waits_for_inflight_save(self):
        index = self.plugin.index
        index.save(time.time())
        started = threading.Event()
        release = threading.Event()
        real_fsync = os.fsync

        def slow_fsync(fd):
            started.set()
            release.wait(5)
            return real_fsync(fd)

        def saver() -> None:
            index.save(time.time())

        def clearer() -> None:
            index.clear_file()

        with mock.patch('os.fsync', slow_fsync):
            writer = threading.Thread(target=saver)
            writer.start()
            self.assertTrue(started.wait(5), '保存没有进入写盘阶段')
            remover = threading.Thread(target=clearer)
            remover.start()
            time.sleep(0.1)     # 让 clear_file 真的试着进锁
            release.set()
            writer.join(10)
            remover.join(10)

        self.assertFalse(writer.is_alive() or remover.is_alive(), '线程没有结束')
        self.assertFalse(index.cache_file.exists(),
                         'clear_file 必须排在在途写之后：否则写完又把文件建回来了')


class CollectionsWriteSerializationTests(_PluginFixture):
    """一次换曲会从不同桥接线程连发 update_recent：读-改-写必须串行，否则丢条目。"""

    def test_concurrent_update_recent_keeps_both_entries(self):
        collections = self.plugin.collections
        module = sys.modules[type(collections).__module__]
        real_strftime = module.time.strftime
        barrier = threading.Barrier(2, timeout=0.5)
        errors: list = []

        def slow_strftime(*args, **kwargs):
            # 卡在"读旧值之后、赋值之前"的窗口里：另一个线程此时也会读到旧值
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return real_strftime(*args, **kwargs)

        def worker(item_id: str) -> None:
            try:
                collections.update_recent(item_id)
            except Exception as error:      # pragma: no cover - 失败时给断言留证据
                errors.append(error)

        fake_time = types.SimpleNamespace(strftime=slow_strftime)
        with mock.patch.object(module, 'time', fake_time):
            threads = [threading.Thread(target=worker, args=(item_id,))
                       for item_id in ('a', 'b')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

        self.assertEqual(errors, [])
        ids = [entry.get('id') for entry in self.plugin.state.data.get('recent', [])]
        self.assertEqual(sorted(ids), ['a', 'b'], f'并发 update_recent 丢条目：{ids}')


class IndexAtomicWriteTests(_PluginFixture):
    """`MediaIndex.save` 的原子性与并发写：索引文件任何时候都必须是完整 JSON。"""

    def _temp_leftovers(self) -> list:
        cache_file = self.plugin.index.cache_file
        prefix = f'.{cache_file.name}.'
        return [path.name for path in cache_file.parent.iterdir()
                if path.name.startswith(prefix)]

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        _track(self.root, 'd0/a.mp3')
        self._sync()
        index = self.plugin.index
        index.touch()

        index.save(time.time())

        with open(index.cache_file, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertEqual(payload['version'], scanner_module.INDEX_VERSION)
        self.assertEqual(len(payload['items']), 1)
        self.assertFalse(index.dirty, '落盘成功后脏标记应清掉')
        self.assertEqual(self._temp_leftovers(), [], '原子写不留下临时文件')

    def test_concurrent_save_keeps_the_file_parseable(self):
        """`on_pass_end`（校验线程）与 `on_unload`（退出线程）会并发走到同一次写。"""
        _track(self.root, 'd0/a.mp3')
        self._sync()
        index = self.plugin.index
        index.touch()
        errors: list = []

        def writer() -> None:
            try:
                index.save(time.time())
            except Exception as error:      # pragma: no cover - 失败时给断言留证据
                errors.append(error)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        self.assertEqual(errors, [])
        with open(index.cache_file, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertEqual(payload['version'], scanner_module.INDEX_VERSION)
        self.assertEqual(len(payload['items']), 1)
        self.assertEqual(self._temp_leftovers(), [], '并发写不留下临时文件')


if __name__ == '__main__':      # pragma: no cover
    unittest.main()
