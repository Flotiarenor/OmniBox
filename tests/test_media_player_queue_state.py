"""媒体播放器播放状态 / 队列持久化与批量收藏的单元测试。

队列部分（media_save_queue / media_get_items）背景：`save_playback` 过去整体替换
`_state['playback']`，任何一次换曲都会把队列字段清掉；恢复播放时前端也只拿到当前条目，
队列退化成单条，表现为重载后「下一首/上一首」不跟列表。这里固定三条行为：队列 id 列表与
播放参数互不覆盖、超出上限按上限截断并置标记、批量取条目按传入顺序返回且忽略已失效 id。

收藏部分（media_add_favorites）背景：网易云「喜欢」导入要一次把几十首写进收藏，逐个
toggle 会写几十次状态文件，而 toggle 是翻转语义 —— 已经喜欢的条目会被再次导入**取消**。

装配方式：只拉起被测组件（`UserStateStore` / `MediaCollections` / `MissingReport`）与
两个替身（路径、索引），不构造整个插件 —— 后端组件化之后"不启动插件就能测一个能力"
正是目标，这比 `object.__new__(MediaPlayerPlugin)` 更接近真实装配路径。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'media-player' / 'backend'
if str(PLUGIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(PLUGIN_BACKEND))

from shell.backend.plugin_utils import load_sibling

_models = load_sibling(str(PLUGIN_BACKEND / 'models.py'), 'models', 'mp_queue_models')
_scanner = load_sibling(str(PLUGIN_BACKEND / 'scanner.py'), 'scanner', 'mp_queue_scanner')
_const = load_sibling(str(PLUGIN_BACKEND / 'const.py'), 'const', 'mp_queue_const')
_state = load_sibling(str(PLUGIN_BACKEND / 'state.py'), 'state', 'mp_queue_state')
_collections = load_sibling(str(PLUGIN_BACKEND / 'collections.py'), 'collections',
                            'mp_queue_collections')
_report = load_sibling(str(PLUGIN_BACKEND / 'report.py'), 'report', 'mp_queue_report')


class _FakePaths:
    """路径替身：只提供组件实际用到的几个属性。"""

    def __init__(self, base: Path) -> None:
        self.state_file = base / 'media_state.json'
        self.cache_dir = base / '.cache'
        self.root_dir = base
        self.scan_roots: List[Path] = []


class _FakeIndex:
    """索引替身：`items` 与 `get` 就是各组件的全部契约。"""

    def __init__(self, items: Dict[str, object]) -> None:
        self.items = items

    def get(self, item_id: str):
        return self.items.get(item_id)


class _CountingStore(_state.UserStateStore):
    """记下真实写盘次数（空批次不应写盘）。"""

    def __init__(self, paths, index) -> None:
        super().__init__(paths, index, _scanner.item_id)
        self.save_count = 0

    def save(self) -> None:
        self.save_count += 1


def _app(tmp: Path, item_ids: Sequence[str]) -> SimpleNamespace:
    items = {}
    for item_id in item_ids:
        items[item_id] = _models.MediaItem(
            id=item_id,
            path=f'D:/media/{item_id}.mp3',
            kind='audio',
            title=f'title-{item_id}',
            artist='artist',
            album='album',
            album_key='album|artist',
            mtime=1.0,
        )
    index = _FakeIndex(items)
    paths = _FakePaths(tmp)
    store = _CountingStore(paths, index)
    store.data = {'favorites': [], 'recent': [], 'playlists': [], 'playback': {}}
    return SimpleNamespace(
        index=index,
        paths=paths,
        state=store,
        collections=_collections.MediaCollections(store, index, _const.QUEUE_PERSIST_LIMIT),
        report=_report.MissingReport(paths, index),
    )


class MediaPlaybackQueueStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _app(self, item_ids: Sequence[str]) -> SimpleNamespace:
        return _app(self.tmp, item_ids)

    def test_save_playback_keeps_queue_ids(self):
        """换曲只更新播放参数，不得清掉已保存的队列 id 列表与下标。"""
        app = self._app(['a', 'b', 'c'])
        app.collections.save_queue(['a', 'b', 'c'])
        app.collections.save_playback('b', 'all', True, 0.5, 'video', 1)

        pb = app.collections.get_playback()
        self.assertEqual(pb['queue_ids'], ['a', 'b', 'c'])
        self.assertEqual(pb['queue_index'], 1)
        self.assertEqual(pb['item_id'], 'b')
        self.assertTrue(pb['shuffle'])

    def test_save_playback_without_index_keeps_previous_index(self):
        """未传 queue_index（旧调用方）时保留原有下标。"""
        app = self._app(['a', 'b'])
        app.collections.save_queue(['a', 'b'])
        app.collections.save_playback('a', 'all', False, 1.0, 'video', 1)
        app.collections.save_playback('a', 'all', False, 1.0, 'video')

        self.assertEqual(app.collections.get_playback()['queue_index'], 1)

    def test_save_queue_truncates_and_flags(self):
        """超过 QUEUE_PERSIST_LIMIT 的队列按上限截断，并置 truncated 标记。"""
        limit = _const.QUEUE_PERSIST_LIMIT
        ids = [f'i{n}' for n in range(limit + 5)]
        app = self._app([])

        result = app.collections.save_queue(ids)
        pb = app.collections.get_playback()
        self.assertEqual(result['count'], limit)
        self.assertTrue(result['truncated'])
        self.assertTrue(pb['queue_truncated'])
        self.assertEqual(len(pb['queue_ids']), limit)
        self.assertEqual(pb['queue_ids'][0], 'i0')

    def test_save_queue_clamps_index_into_range(self):
        """队列变短后下标不得越界（恢复时按 id 定位，越界下标只作兜底）。"""
        app = self._app([])
        app.collections.save_playback('', 'all', False, 1.0, 'video', 9)
        app.collections.save_queue(['a', 'b'])

        self.assertEqual(app.collections.get_playback()['queue_index'], 1)
        app.collections.save_queue([])
        self.assertEqual(app.collections.get_playback()['queue_index'], 0)

    def test_get_items_preserves_order_and_skips_missing(self):
        """批量取条目按传入顺序返回，已不存在的 id 直接跳过。"""
        app = self._app(['a', 'b', 'c'])

        items = app.collections.get_items(['c', 'missing', 'a'])
        self.assertEqual([item['id'] for item in items], ['c', 'a'])
        self.assertEqual(items[0]['title'], 'title-c')

    def test_get_items_empty_input(self):
        app = self._app(['a'])
        self.assertEqual(app.collections.get_items([]), [])
        self.assertEqual(app.collections.get_items(None), [])


class MediaFavoritesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = _app(Path(self._tmp.name), ['a', 'b', 'c', 'd'])

    def test_add_favorites_is_idempotent(self):
        """重复导入同一批曲目只写一次盘，且不会把已喜欢的翻成不喜欢。"""
        first = self.app.collections.add_favorites(['a', 'b'])
        second = self.app.collections.add_favorites(['a', 'b', 'c'])

        self.assertEqual(first['added'], 2)
        self.assertEqual(second['added'], 1)
        self.assertEqual(self.app.state.data['favorites'], ['a', 'b', 'c'])
        self.assertEqual(self.app.state.save_count, 2, '空批次不应写盘')

    def test_add_favorites_skips_unknown_and_blank_ids(self):
        """索引里不存在的 id 不能进收藏：get_state 解析不出来，等于写进去就删不掉。"""
        result = self.app.collections.add_favorites(['a', 'missing', '', None])

        self.assertEqual(result['added'], 1)
        self.assertEqual(self.app.state.data['favorites'], ['a'])

    def test_add_favorites_with_nothing_new_does_not_write(self):
        self.app.collections.add_favorites(['a'])
        self.app.state.save_count = 0

        self.assertEqual(self.app.collections.add_favorites(['a'])['added'], 0)
        self.assertEqual(self.app.state.save_count, 0)


class MediaExportMissingTests(unittest.TestCase):
    """缺失曲目清单导出（media_export_missing）。

    背景：清单要和音乐放在一起才有用，而「数据根」是多根配置里的第一行 —— 当前部署里
    第一行是视频盘，所以落点按「谁的音频条目多」挑；标题会拼进文件名，必须去掉路径字符，
    否则一个 `..\\` 就能把「导出结果」变成任意路径写文件。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _music_app(self) -> SimpleNamespace:
        app = _app(self.tmp, ['a', 'b'])
        music = self.tmp / '音乐'
        video = self.tmp / '视频'
        music.mkdir()
        video.mkdir()
        app.paths.scan_roots = [video, music]
        app.paths.root_dir = video
        app.index.items['a'].path = str(music / 'a.mp3')
        app.index.items['b'].path = str(music / 'b.mp3')
        app.index.items['v'] = _models.MediaItem(
            id='v', path=str(video / 'v.mp4'), kind='video', title='v',
            artist='', album='', album_key='x|')
        return app

    def test_report_lands_in_the_music_root(self):
        app = self._music_app()

        result = app.report.export(['【歌单】1 首本地缺失', '  甲 - 缺A'], '网易云缺失曲目')

        self.assertTrue(result['success'])
        self.assertEqual(Path(result['path']), self.tmp / '音乐' / '网易云缺失曲目.txt')
        self.assertEqual(result['count'], 2)
        text = (self.tmp / '音乐' / '网易云缺失曲目.txt').read_text(encoding='utf-8')
        self.assertIn('甲 - 缺A', text)

    def test_title_cannot_escape_the_media_root(self):
        """标题是文件名的一部分，路径字符必须被剥掉（`../evil` 不能写出根目录）。"""
        app = self._music_app()

        result = app.report.export(['x'], '../../evil')

        self.assertTrue(result['success'])
        self.assertEqual(Path(result['path']).parent, self.tmp / '音乐')

    def test_no_missing_still_rewrites_the_file(self):
        """没有缺失时也要重写：否则把缺的歌补齐后，旧清单还挂在那里像没同步。"""
        app = self._music_app()
        target = self.tmp / '音乐' / '网易云缺失曲目.txt'
        target.write_text('上一次的旧清单\n', encoding='utf-8')

        result = app.report.export([], '网易云缺失曲目')

        self.assertTrue(result['success'])
        self.assertEqual(result['count'], 0)
        text = target.read_text(encoding='utf-8')
        self.assertNotIn('上一次的旧清单', text)
        self.assertIn('没有缺失曲目', text)

    def test_repeat_call_overwrites_instead_of_appending(self):
        app = self._music_app()

        app.report.export(['缺A'], '网易云缺失曲目')
        app.report.export(['缺B'], '网易云缺失曲目')

        text = (self.tmp / '音乐' / '网易云缺失曲目.txt').read_text(encoding='utf-8')
        self.assertIn('缺B', text)
        self.assertNotIn('缺A', text)

    def test_title_gets_its_own_file(self):
        """单个歌单的清单写自己的文件，不覆盖全量清单。"""
        app = self._music_app()

        app.report.export(['全量缺失'], '网易云缺失曲目')
        app.report.export(['单歌单缺失'], '网易云缺失曲目 · 某个歌单')

        music = self.tmp / '音乐'
        self.assertIn('全量缺失', (music / '网易云缺失曲目.txt').read_text(encoding='utf-8'))
        self.assertIn('单歌单缺失',
                      (music / '网易云缺失曲目 · 某个歌单.txt').read_text(encoding='utf-8'))


class StateDefaultsIsolationTests(unittest.TestCase):
    """回归：状态文件的默认容器不得跨实例共享。

    背景：`UserStateStore` 的默认形状里 `favorites` / `recent` / `playlists` / `playback`
    都是可变容器，而歌单写入是**就地** append。如果默认形状来自模块级常量的一份浅拷贝，
    同一个进程里第二个实例就会带上第一个实例的歌单与收藏（先开一个实例、再开另一个
    必现）。`_defaults()` 每次返回全新容器，两条入口都要守住：构造时与 `load()` 时。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _store(self, name: str):
        return _state.UserStateStore(_FakePaths(self.tmp / name), _FakeIndex({}),
                                     _scanner.item_id)

    def test_two_stores_do_not_share_default_containers(self):
        first = self._store('a')
        second = self._store('b')

        first.data['playlists'].append({'id': 'p1', 'name': '第一个实例的歌单', 'item_ids': []})
        first.data['favorites'].append('item-1')
        first.data['playback']['item_id'] = 'item-1'

        self.assertEqual(second.data['playlists'], [], '第二个实例不应看到第一个实例的歌单')
        self.assertEqual(second.data['favorites'], [])
        self.assertEqual(second.data['playback'], {})

    def test_load_without_state_file_starts_from_fresh_containers(self):
        first = self._store('a')
        first.load()
        first.data['playlists'].append({'id': 'p1', 'name': '第一个实例的歌单', 'item_ids': []})

        second = self._store('b')
        second.load()

        self.assertEqual(second.data['playlists'], [], 'load() 也不能复用其它实例的容器')

    def test_load_of_partial_state_file_starts_from_fresh_containers(self):
        # 两份都只有 `{}`：缺 playlists 键时由默认值补齐，这条合并路径也不得共享容器
        for name in ('a', 'b'):
            path = self.tmp / name / 'media_state.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}', encoding='utf-8')
        first = self._store('a')
        first.load()
        first.data['playlists'].append({'id': 'p1', 'name': '第一个实例的歌单', 'item_ids': []})

        second = self._store('b')
        second.load()

        self.assertEqual(second.data['playlists'], [])


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
