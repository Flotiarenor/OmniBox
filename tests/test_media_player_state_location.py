"""播放记录（歌单/喜欢/进度）换了个落点：不再随媒体文件夹搬家。

背景：`media_state.json` 原先落在「媒体文件夹」第一行的 `.cache` 下，而那一行同时
是数据根 —— 用户在设置里换文件夹（或调整顺序）就等于把整个状态文件换到别处，插件
在新位置写一份空状态、旧的那份留在原地再没人读（用户视角："我的歌单被删了"）。

这里守三件事：
1. 落点在壳数据根下，换媒体文件夹不影响它；
2. 新落点没有状态文件时，从各媒体根的旧位置收养**最完整**的一份；
3. 读取失败不再静默退默认值（先备份），写入是原子的（读取方拿不到半份 JSON）。
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.settings_store import SettingsStore

_PLUGIN = PROJECT_ROOT / 'plugins' / 'media-player' / 'backend' / 'main.py'


def _load_plugin_class():
    spec = importlib.util.spec_from_file_location('media_player_state_test', str(_PLUGIN))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MediaPlayerPlugin


class StateLocationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.shell_data = self.tmp / 'shell-data'
        self.media = self.tmp / 'media'
        self.video = self.tmp / 'video'
        for d in (self.shell_data, self.media, self.video):
            d.mkdir(parents=True, exist_ok=True)
        self.cls = _load_plugin_class()

    def _plugin(self, roots):
        self.cls._resolved_config = {'media_roots': '\n'.join(str(r) for r in roots)}
        plugin = self.cls({'name': 'media-player'},
                          {'directories': {'data_root': str(self.shell_data)}})
        plugin._settings_store = SettingsStore(str(self.tmp / 'settings'))
        self.addCleanup(plugin.on_unload)
        return plugin

    def _write_state(self, cache_root: Path, playlists, favorites):
        cache = cache_root / '.cache'
        cache.mkdir(parents=True, exist_ok=True)
        (cache / 'media_state.json').write_text(json.dumps({
            'playlists': playlists, 'favorites': favorites, 'recent': [], 'playback': {},
        }, ensure_ascii=False), encoding='utf-8')

    def test_state_lives_under_the_shell_data_root(self):
        plugin = self._plugin([self.media, self.video])

        # 两侧都 resolve：`PluginBase.get_data_root()` 会解析路径，Windows 上
        # 临时目录的 8.3 短名（ADMINI~1）与长名解析结果不同
        self.assertEqual(plugin._state_file.resolve(),
                         (self.shell_data / '.cache' / 'media-player'
                          / 'media_state.json').resolve())
        self.assertNotIn(str(self.media), str(plugin._state_file),
                         '用户数据不能落在媒体文件夹里')

    def test_changing_media_root_keeps_the_state_file(self):
        plugin = self._plugin([self.media, self.video])
        plugin.playlist_save('我的歌单', '', [])
        before = plugin._state_file

        plugin.on_settings_changed({'media_roots'})       # 把另一个媒体根挪到第一行

        self.assertEqual(plugin._state_file, before, '换媒体文件夹不该换用户数据的落点')
        self.assertEqual([p['name'] for p in plugin.playlist_list()], ['我的歌单'])

    def test_orphan_state_is_adopted_on_first_run(self):
        """新落点没有状态文件时，收养各媒体根里最完整的那份（歌单 + 喜欢）。"""
        self._write_state(self.video, [{'id': 'a', 'name': '网易云 · 甲'}], [1, 2, 3])
        self._write_state(self.media, [{'id': 'b', 'name': '手工歌单'}], [9])

        plugin = self._plugin([self.media, self.video])

        self.assertEqual([p['name'] for p in plugin.playlist_list()], ['网易云 · 甲'],
                         '要收养更完整的那份')
        self.assertEqual(plugin._state['favorites'], [1, 2, 3])
        self.assertTrue((self.video / '.cache' / 'media_state.json').exists(),
                        '收养是复制，旧文件要留在原地可回溯')

    def test_existing_state_wins_over_orphans(self):
        self._write_state(self.video, [{'id': 'a', 'name': '旧的'}], [1, 2, 3])
        state_dir = self.shell_data / '.cache' / 'media-player'
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / 'media_state.json').write_text(json.dumps({
            'playlists': [], 'favorites': [], 'recent': [], 'playback': {},
        }), encoding='utf-8')

        plugin = self._plugin([self.media, self.video])

        self.assertEqual(plugin.playlist_list(), [], '已有状态文件就不再收养')

    def test_broken_state_is_backed_up_not_overwritten_silently(self):
        state_dir = self.shell_data / '.cache' / 'media-player'
        state_dir.mkdir(parents=True, exist_ok=True)
        broken = state_dir / 'media_state.json'
        broken.write_text('{"playlists": [', encoding='utf-8')

        plugin = self._plugin([self.media])

        self.assertEqual(plugin.playlist_list(), [])
        backups = list(state_dir.glob('media_state.corrupt-*.json'))
        self.assertEqual(len(backups), 1, '损坏的状态文件必须先备份再退默认值')
        self.assertEqual(backups[0].read_text(encoding='utf-8'), '{"playlists": [')

    def test_state_write_is_atomic(self):
        plugin = self._plugin([self.media])
        plugin.playlist_save('甲', '', [])

        plugin._save_state()

        self.assertFalse((plugin._state_file.parent / 'media_state.tmp').exists(),
                         '临时文件要已被替换掉')
        data = json.loads(plugin._state_file.read_text(encoding='utf-8'))
        self.assertEqual([p['name'] for p in data['playlists']], ['甲'])


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
