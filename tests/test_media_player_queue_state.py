"""媒体播放器播放状态 / 队列持久化与批量收藏的单元测试。

队列部分（media_save_queue / media_get_items）背景：`save_playback` 过去整体替换
`_state['playback']`，任何一次换曲都会把队列字段清掉；恢复播放时前端也只拿到当前条目，
队列退化成单条，表现为重载后「下一首/上一首」不跟列表。这里固定三条行为：队列 id 列表与
播放参数互不覆盖、超出上限按上限截断并置标记、批量取条目按传入顺序返回且忽略已失效 id。

收藏部分（media_add_favorites）背景：网易云「喜欢」导入要一次把几十首写进收藏，逐个
toggle 会写几十次状态文件，而 toggle 是翻转语义 —— 已经喜欢的条目会被再次导入**取消**。
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'media-player' / 'backend'
if str(PLUGIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(PLUGIN_BACKEND))

from shell.backend.plugin_utils import load_sibling

mp = load_sibling(str(PLUGIN_BACKEND / 'main.py'), 'main', 'media_player_queue_test')


def _plugin(item_ids):
    """只装配播放状态相关方法需要的最小状态，避免拉起整个插件。"""
    plugin = object.__new__(mp.MediaPlayerPlugin)
    plugin._items = {}
    for item_id in item_ids:
        plugin._items[item_id] = mp.MediaItem(
            id=item_id,
            path=f'D:/media/{item_id}.mp3',
            kind='audio',
            title=f'title-{item_id}',
            artist='artist',
            album='album',
            album_key='album|artist',
            mtime=1.0,
        )
    plugin._state = {'favorites': [], 'recent': [], 'playlists': [], 'playback': {}}
    plugin.save_count = 0

    def _save_state():
        plugin.save_count += 1

    plugin._save_state = _save_state
    return plugin


class MediaPlaybackQueueStateTests(unittest.TestCase):
    def test_save_playback_keeps_queue_ids(self):
        """换曲只更新播放参数，不得清掉已保存的队列 id 列表与下标。"""
        plugin = _plugin(['a', 'b', 'c'])
        plugin.save_queue(['a', 'b', 'c'])
        plugin.save_playback('b', 'all', True, 0.5, 'video', 1)

        pb = plugin.get_playback()
        self.assertEqual(pb['queue_ids'], ['a', 'b', 'c'])
        self.assertEqual(pb['queue_index'], 1)
        self.assertEqual(pb['item_id'], 'b')
        self.assertTrue(pb['shuffle'])

    def test_save_playback_without_index_keeps_previous_index(self):
        """未传 queue_index（旧调用方）时保留原有下标。"""
        plugin = _plugin(['a', 'b'])
        plugin.save_queue(['a', 'b'])
        plugin.save_playback('a', 'all', False, 1.0, 'video', 1)
        plugin.save_playback('a', 'all', False, 1.0, 'video')

        self.assertEqual(plugin.get_playback()['queue_index'], 1)

    def test_save_queue_truncates_and_flags(self):
        """超过 QUEUE_PERSIST_LIMIT 的队列按上限截断，并置 truncated 标记。"""
        limit = mp.QUEUE_PERSIST_LIMIT
        ids = [f'i{n}' for n in range(limit + 5)]
        plugin = _plugin([])

        result = plugin.save_queue(ids)
        pb = plugin.get_playback()
        self.assertEqual(result['count'], limit)
        self.assertTrue(result['truncated'])
        self.assertTrue(pb['queue_truncated'])
        self.assertEqual(len(pb['queue_ids']), limit)
        self.assertEqual(pb['queue_ids'][0], 'i0')

    def test_save_queue_clamps_index_into_range(self):
        """队列变短后下标不得越界（恢复时按 id 定位，越界下标只作兜底）。"""
        plugin = _plugin([])
        plugin.save_playback('', 'all', False, 1.0, 'video', 9)
        plugin.save_queue(['a', 'b'])

        self.assertEqual(plugin.get_playback()['queue_index'], 1)
        plugin.save_queue([])
        self.assertEqual(plugin.get_playback()['queue_index'], 0)

    def test_get_items_preserves_order_and_skips_missing(self):
        """批量取条目按传入顺序返回，已不存在的 id 直接跳过。"""
        plugin = _plugin(['a', 'b', 'c'])

        items = plugin.get_items(['c', 'missing', 'a'])
        self.assertEqual([i['id'] for i in items], ['c', 'a'])
        self.assertEqual(items[0]['title'], 'title-c')

    def test_get_items_empty_input(self):
        plugin = _plugin(['a'])
        self.assertEqual(plugin.get_items([]), [])
        self.assertEqual(plugin.get_items(None), [])


class MediaFavoritesTests(unittest.TestCase):
    def test_add_favorites_is_idempotent(self):
        """重复导入同一批曲目只写一次盘，且不会把已喜欢的翻成不喜欢。"""
        plugin = _plugin(['a', 'b', 'c', 'd'])

        first = plugin.add_favorites(['a', 'b'])
        second = plugin.add_favorites(['a', 'b', 'c'])

        self.assertEqual(first['added'], 2)
        self.assertEqual(second['added'], 1)
        self.assertEqual(plugin._state['favorites'], ['a', 'b', 'c'])
        self.assertEqual(plugin.save_count, 2, '空批次不应写盘')

    def test_add_favorites_skips_unknown_and_blank_ids(self):
        """索引里不存在的 id 不能进收藏：get_state 解析不出来，等于写进去就删不掉。"""
        plugin = _plugin(['a'])

        result = plugin.add_favorites(['a', 'missing', '', None])

        self.assertEqual(result['added'], 1)
        self.assertEqual(plugin._state['favorites'], ['a'])

    def test_add_favorites_with_nothing_new_does_not_write(self):
        plugin = _plugin(['a'])
        plugin.add_favorites(['a'])
        plugin.save_count = 0

        self.assertEqual(plugin.add_favorites(['a'])['added'], 0)
        self.assertEqual(plugin.save_count, 0)


class MediaExportMissingTests(unittest.TestCase):
    """缺失曲目清单导出（media_export_missing）。

    背景：清单要和音乐放在一起才有用，而「数据根」是多根配置里的第一行 —— 当前部署里
    第一行是视频盘，所以落点按「谁的音频条目多」挑；标题会拼进文件名，必须去掉路径字符，
    否则一个 `..\\` 就能把「导出结果」变成任意路径写文件。
    """

    def _music_plugin(self, tmp):
        plugin = _plugin(['a', 'b'])
        music = tmp / '音乐'
        video = tmp / '视频'
        music.mkdir()
        video.mkdir()
        plugin._scan_roots = [video, music]
        plugin.root_dir = video
        plugin._items['a'].path = str(music / 'a.mp3')
        plugin._items['b'].path = str(music / 'b.mp3')
        plugin._items['v'] = mp.MediaItem(
            id='v', path=str(video / 'v.mp4'), kind='video', title='v',
            artist='', album='', album_key='x|')
        return plugin, music

    def test_report_lands_in_the_music_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, music = self._music_plugin(Path(tmp))

            result = plugin.export_missing(['【歌单】1 首本地缺失', '  甲 - 缺A'], '网易云缺失曲目')

            self.assertTrue(result['success'])
            self.assertEqual(Path(result['path']), music / '网易云缺失曲目.txt')
            self.assertEqual(result['count'], 2)
            text = (music / '网易云缺失曲目.txt').read_text(encoding='utf-8')
            self.assertIn('甲 - 缺A', text)

    def test_title_cannot_escape_the_media_root(self):
        """"标题是文件名的一部分，路径字符必须被剥掉（`../evil` 不能写出根目录）。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, music = self._music_plugin(Path(tmp))

            result = plugin.export_missing(['x'], '../../evil')

            self.assertTrue(result['success'])
            self.assertEqual(Path(result['path']).parent, music)

    def test_no_missing_still_rewrites_the_file(self):
        """没有缺失时也要重写：否则把缺的歌补齐后，旧清单还挂在那里像没同步。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, music = self._music_plugin(Path(tmp))
            target = music / '网易云缺失曲目.txt'
            target.write_text('上一次的旧清单\n', encoding='utf-8')

            result = plugin.export_missing([], '网易云缺失曲目')

            self.assertTrue(result['success'])
            self.assertEqual(result['count'], 0)
            text = target.read_text(encoding='utf-8')
            self.assertNotIn('上一次的旧清单', text)
            self.assertIn('没有缺失曲目', text)

    def test_repeat_call_overwrites_instead_of_appending(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, music = self._music_plugin(Path(tmp))

            plugin.export_missing(['缺A'], '网易云缺失曲目')
            plugin.export_missing(['缺B'], '网易云缺失曲目')

            text = (music / '网易云缺失曲目.txt').read_text(encoding='utf-8')
            self.assertIn('缺B', text)
            self.assertNotIn('缺A', text)

    def test_title_gets_its_own_file(self):
        """单个歌单的清单写自己的文件，不覆盖全量清单。"""
        with tempfile.TemporaryDirectory() as tmp:
            plugin, music = self._music_plugin(Path(tmp))

            plugin.export_missing(['全量缺失'], '网易云缺失曲目')
            plugin.export_missing(['单歌单缺失'], '网易云缺失曲目 · 某个歌单')

            self.assertIn('全量缺失', (music / '网易云缺失曲目.txt').read_text(encoding='utf-8'))
            self.assertIn('单歌单缺失', (music / '网易云缺失曲目 · 某个歌单.txt').read_text(encoding='utf-8'))


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
