"""媒体播放器播放状态与队列持久化的单元测试（media_save_queue / media_get_items）。

背景：`save_playback` 过去整体替换 `_state['playback']`，任何一次换曲都会把队列字段清掉；
恢复播放时前端也只拿到当前条目，队列退化成单条，表现为重载后「下一首/上一首」不跟列表。
这里固定三条行为：队列 id 列表与播放参数互不覆盖、超出上限按上限截断并置标记、
批量取条目按传入顺序返回且忽略已失效 id。
"""

import sys
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


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
