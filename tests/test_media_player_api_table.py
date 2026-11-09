"""media-player 后端组件化之后的**门面契约**回归。

为什么需要：组件化把每个 API 键的实现挪进了组件，用例也随之改成直接调组件 ——
于是"`register_api` 里键名与绑定对象"这一段就没有任何用例覆盖了。前端与壳按名字取
成员（`media_*` 经 `Bridge.call` 映射、九个钩子由壳直接取），改名或漏绑都不会报错，
只会静默 404 / 静默退化成默认实现。这里把三件事钉住：

1. `register_api` 的键与**顺序**与迁移前逐字一致，且每个键绑到预期组件；
2. 壳按名调用的钩子仍在入口类上（且是入口自己的实现，不是组件上的同名方法）；
3. 入口没有覆写 `PluginBase.get_settings` —— 覆写会改掉基类的脱敏与默认值语义。

运行：
    python -m unittest tests.test_media_player_api_table -v
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

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

mp = load_sibling(str(PLUGIN_BACKEND / 'main.py'), 'main', 'media_player_api_table_test')

#: 迁移前的键与顺序（前端按名字调用，改动即静默 404）。
EXPECTED_KEYS = [
    'media_scan',
    'media_scan_status',
    'media_scan_cancel',
    'media_search',
    'media_recent',
    'media_stats',
    'media_audio_albums',
    'media_all_audio',
    'media_video_albums',
    'media_all_video',
    'media_album_items',
    'media_get_item',
    'media_get_items',
    'media_playlist_list',
    'media_playlist_get',
    'media_playlist_save',
    'media_playlist_delete',
    'media_toggle_favorite',
    'media_add_favorites',
    'media_update_recent',
    'media_get_state',
    'media_save_playback',
    'media_save_queue',
    'media_get_playback',
    'media_get_lyrics',
    'media_export_missing',
    'media_put_thumb',
    'media_thumb_missing',
    'media_debug_meta',
    'media_ffmpeg_status',
    'media_list_eq_presets',
    'media_save_eq_preset',
    'media_get_config',
    'media_set_config',
    'get_settings',
    'save_settings',
]

#: 键 → 绑定的组件属性名（绑定错组件时"能调用"但行为不对，只有这里能发现）。
EXPECTED_COMPONENTS = {
    'media_scan': 'freshness',
    'media_scan_status': 'freshness',
    'media_scan_cancel': 'freshness',
    'media_search': 'library',
    'media_recent': 'library',
    'media_stats': 'library',
    'media_audio_albums': 'library',
    'media_all_audio': 'library',
    'media_video_albums': 'library',
    'media_all_video': 'library',
    'media_album_items': 'library',
    'media_get_item': 'library',
    'media_get_items': 'collections',
    'media_playlist_list': 'collections',
    'media_playlist_get': 'collections',
    'media_playlist_save': 'collections',
    'media_playlist_delete': 'collections',
    'media_toggle_favorite': 'collections',
    'media_add_favorites': 'collections',
    'media_update_recent': 'collections',
    'media_get_state': 'collections',
    'media_save_playback': 'collections',
    'media_save_queue': 'collections',
    'media_get_playback': 'collections',
    'media_get_lyrics': 'lyrics',
    'media_export_missing': 'report',
    'media_put_thumb': 'covers',
    'media_thumb_missing': 'covers',
    'media_debug_meta': 'diagnostics',
    'media_ffmpeg_status': 'diagnostics',
    'media_list_eq_presets': 'presets',
    'media_save_eq_preset': 'presets',
}

#: 壳按名字从插件实例上取的钩子（不经 `register_api`）。
SHELL_HOOKS = [
    'settings_schema',
    'freshness_spec',
    'get_data_root',
    'get_file_roots',
    'get_thumb_data',
    'get_cache_dir',
    'on_settings_changed',
    'on_unload',
]

#: 其中必须由入口类自己声明（一行委派）的那些。`get_cache_dir` 不在列：media-player
#: 沿用基类派生（`数据根/.cache/<插件名>`），刻意不覆写。
ENTRY_DECLARED_HOOKS = [
    'settings_schema',
    'freshness_spec',
    'get_data_root',
    'get_file_roots',
    'get_thumb_data',
    'on_settings_changed',
    'on_unload',
]


class MediaPlayerApiTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name) / 'media'
        mp.MediaPlayerPlugin._resolved_config = {'media_roots': str(root)}
        self.addCleanup(setattr, mp.MediaPlayerPlugin, '_resolved_config', {})
        self.plugin = mp.MediaPlayerPlugin(
            {'name': 'media-player'},
            {'directories': {'data_root': str(Path(self._tmp.name) / 'shell-data')}})
        self.addCleanup(self.plugin.on_unload)

    def test_api_keys_and_order_are_unchanged(self):
        self.assertEqual(list(self.plugin.register_api()), EXPECTED_KEYS)

    def test_every_api_key_binds_expected_component(self):
        api = self.plugin.register_api()
        for key, attribute in EXPECTED_COMPONENTS.items():
            with self.subTest(key=key):
                bound = getattr(api[key], '__self__', None)
                self.assertIs(bound, getattr(self.plugin, attribute),
                              f'{key} 应绑定到组件的 {attribute}')

    def test_get_and_save_settings_bind_to_base_members(self):
        api = self.plugin.register_api()
        self.assertEqual(api['get_settings'].__func__, PluginBase.get_settings)
        self.assertEqual(api['save_settings'].__func__, PluginBase.save_settings)
        self.assertIs(api['get_settings'].__self__, self.plugin)

    def test_shell_hooks_live_on_the_entry_class(self):
        for name in SHELL_HOOKS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(type(self.plugin), name),
                                f'壳按名调用的 {name} 必须能从插件实例上取到')
        for name in ENTRY_DECLARED_HOOKS:
            with self.subTest(name=name):
                self.assertIn(name, type(self.plugin).__dict__,
                              f'{name} 必须由入口类自己声明（一行委派）')

    def test_entry_does_not_override_get_settings(self):
        """覆写基类同名方法会改掉它的脱敏与默认值语义。"""
        self.assertIs(type(self.plugin).get_settings, PluginBase.get_settings)

    def test_freshness_spec_fields_are_unchanged(self):
        """壳的 freshness 契约字段与语义不变（渲染、同步、校验都读它）。"""
        spec = self.plugin.freshness_spec()
        self.assertEqual(
            sorted(spec),
            sorted(['roots', 'include', 'include_names', 'key_of', 'derive', 'prune',
                    'on_verified', 'on_pass_end', 'rebuild', 'content_version',
                    'unit', 'min_sync_interval', 'max_dirs_per_sync']))
        self.assertEqual(spec['unit'], '首')
        self.assertEqual(spec['min_sync_interval'], 5.0)
        self.assertEqual(spec['max_dirs_per_sync'], 400)
        self.assertEqual(list(spec['roots']()), [self.plugin.paths.root_dir])


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
