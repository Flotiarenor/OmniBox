"""manga-library 后端组件化之后的**门面契约**回归。

为什么需要：组件化把每个 API 键的实现挪进了组件，用例也随之改成直接调组件 ——
于是「`register_api` 里键名与绑定对象」这一段就没有任何用例覆盖了。前端与壳按名字
取成员（`manga_*` / `download_*` 经 `Bridge.call` 映射，钩子由壳直接取），改名或
漏绑都不会报错，只会静默 404 / 静默退化成默认实现。这里把四件事钉住：

1. `register_api` 的键与**顺序**与迁移前逐字一致，且每个键绑到预期组件；
2. 壳按名调用的钩子仍在入口类上；
3. 设置项 schema 与迁移前逐字一致（设置弹窗与 `save_settings` 的白名单都读它）；
4. `get_settings` 仍返回**生效值**（根目录可能来自默认数据根，不等于存储里的原值）。

入口覆写 `get_settings` 在这里是有意的、迁移前就有：它对外报的是"当前生效的根"，
而 `PluginBase.get_settings` 报存储原值 —— 换掉会让设置弹窗在未配置根时显示空白。

运行：
    python -m unittest tests.test_manga_library_api_table -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'manga-library'
PLUGIN_BACKEND = PLUGIN_DIR / 'backend'

from shell.backend.freshness import engine_for
from shell.backend.plugin_base import PluginBase
from shell.backend.settings_store import SettingsStore
from tools.check_plugins import _load_backend_class

#: 迁移前的键与顺序（前端按名字调用，改动即静默 404）。
EXPECTED_KEYS = [
    'manga_list',
    'manga_search',
    'manga_get_state',
    'manga_toggle_favorite',
    'manga_update_recent',
    'manga_get_detail',
    'manga_get_pages',
    'download_submit',
    'download_list',
    'download_pause',
    'download_resume',
    'download_retry',
    'download_delete',
    'download_start_all',
    'download_pause_all',
    'download_clear_completed',
    'download_get_album_info',
    'get_settings',
    'save_settings',
]

#: 键 → 绑定的组件属性名（绑错组件时"能调用"但行为不对，只有这里能发现）。
EXPECTED_COMPONENTS = {
    'manga_list': 'library',
    'manga_search': 'library',
    'manga_get_state': 'library',
    'manga_toggle_favorite': 'library',
    'manga_update_recent': 'library',
    'manga_get_detail': 'library',
    'manga_get_pages': 'library',
    'download_submit': 'downloads',
    'download_list': 'downloads',
    'download_pause': 'downloads',
    'download_resume': 'downloads',
    'download_retry': 'downloads',
    'download_delete': 'downloads',
    'download_start_all': 'downloads',
    'download_pause_all': 'downloads',
    'download_clear_completed': 'downloads',
    'download_get_album_info': 'downloads',
}

#: 壳按名字从插件实例上取的钩子（不经 `register_api`）。
SHELL_HOOKS = [
    'settings_schema',
    'freshness_spec',
    'get_data_root',
    'get_settings',
    'on_settings_changed',
    'register_api',
]

#: 设置项字段的完整形状：设置弹窗按它渲染，`save_settings` 按它校验白名单。
EXPECTED_SCHEMA_KEYS = ['root_dir', 'recent_count']


class MangaLibraryApiTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'manga'
        self.root.mkdir(parents=True, exist_ok=True)
        cls = _load_backend_class(PLUGIN_DIR, 'backend/main.py', 'MangaLibraryPlugin')
        cls._resolved_config = {'root_dir': str(self.root)}
        self.addCleanup(setattr, cls, '_resolved_config', {})
        self.plugin = cls({'name': 'manga-library'},
                          {'directories': {'data_root': str(self.root)}})
        self.plugin._settings_store = SettingsStore(
            str(Path(self._tmp.name) / 'settings'))
        self.addCleanup(self.plugin.on_unload)

    def test_api_keys_and_order_are_unchanged(self):
        self.assertEqual(list(self.plugin.register_api()), EXPECTED_KEYS)

    def test_every_api_key_binds_expected_component(self):
        api = self.plugin.register_api()
        for key, attribute in EXPECTED_COMPONENTS.items():
            with self.subTest(key=key):
                self.assertIs(getattr(api[key], '__self__', None),
                              getattr(self.plugin, attribute),
                              f'{key} 应绑定到组件的 {attribute}')

    def test_settings_bindings(self):
        api = self.plugin.register_api()
        # save_settings 仍走基类：校验、脱敏与 on_settings_changed 的通知都在那里。
        self.assertEqual(api['save_settings'].__func__, PluginBase.save_settings)
        self.assertEqual(api['get_settings'].__func__,
                         type(self.plugin).get_settings,
                         'get_settings 是入口自己的实现，用来报"当前生效的根"')
        self.assertIs(api['get_settings'].__self__, self.plugin)

    def test_shell_hooks_live_on_the_entry_class(self):
        for name in SHELL_HOOKS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(type(self.plugin), name),
                                f'壳按名调用的 {name} 必须能从插件实例上取到')

    def test_entry_has_no_mixin_and_is_not_a_mixin(self):
        bases = [base.__name__ for base in type(self.plugin).__mro__[1:]]
        self.assertEqual([name for name in bases if name.endswith('Mixin')], [],
                         f'入口不得继承任何 Mixin: {bases}')

    def test_settings_schema_is_unchanged(self):
        schema = self.plugin.settings_schema
        self.assertEqual([item['key'] for item in schema], EXPECTED_SCHEMA_KEYS)
        root_field = schema[0]
        self.assertEqual(root_field['type'], 'directory')
        self.assertTrue(root_field.get('admin_only'),
                        'root_dir 决定 /file 的允许根，改它必须限管理员')
        recent_field = schema[1]
        self.assertEqual((recent_field['min'], recent_field['max']), (1, 50))
        self.assertEqual(recent_field['default'], 10)

    def test_get_settings_reports_effective_values(self):
        settings = self.plugin.get_settings()
        self.assertEqual(settings['root_dir'], str(self.root.resolve()))
        self.assertEqual(settings['recent_count'], 10)

    def test_recent_count_is_clamped_on_settings_change(self):
        library = self.plugin.library
        saved = self.plugin.save_settings({'recent_count': 7})
        self.assertTrue(saved.get('success'), saved)
        self.assertEqual(library.recent_count, 7)
        # 直接改存储里成越界值：on_settings_changed 仍把它夹回范围内
        self.plugin.update_setting('recent_count', 500)
        self.plugin.on_settings_changed({'recent_count'})
        self.assertEqual(library.recent_count, 50)

    def test_freshness_spec_fields_are_unchanged(self):
        spec = self.plugin.freshness_spec()
        self.assertEqual(
            sorted(spec),
            sorted(['roots', 'include', 'derive', 'prune', 'on_verified', 'rebuild',
                    'content_version', 'unit', 'min_sync_interval', 'stat_entries']))
        self.assertEqual(spec['unit'], '部')
        self.assertEqual(spec['min_sync_interval'], 5.0)
        self.assertIs(spec['stat_entries'], False)
        self.assertEqual(spec['content_version'], self.plugin.SHELF_VERSION)
        self.assertEqual(list(spec['roots']()), [self.root.resolve()])
        self.assertIn('.jpg', spec['include'])

    def test_root_change_rebinds_components(self):
        """换根必须同时搬书架状态与下载任务表，并清掉刷新引擎的去抖。"""
        new_root = Path(self._tmp.name) / 'new-manga'
        new_root.mkdir()
        engine = engine_for(self.plugin)
        engine.spec.min_sync_interval = 0.0
        self.plugin.update_setting('root_dir', str(new_root))

        self.plugin.on_settings_changed({'root_dir'})

        self.assertEqual(self.plugin.get_data_root(), new_root.resolve())
        self.assertEqual(self.plugin.paths.state_file,
                         new_root.resolve() / '.cache' / 'manga_state.json')
        self.assertEqual(self.plugin.get_settings()['root_dir'],
                         str(new_root.resolve()))
        # 下载任务表跟着根走：旧根里的任务不该出现在新根上
        self.assertEqual(self.plugin.downloads.list_tasks()['tasks'], [])
        self.assertTrue(self.plugin.downloads._state_file.startswith(
            str(new_root.resolve())))


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
