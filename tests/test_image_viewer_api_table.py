"""`register_api` 的表必须在迁移后逐字不变，并且真的绑到对应组件上。

为什么需要这条用例：迁移把接口实现从"入口类上的方法"移到了组件，`register_api` 从
"绑定自己"变成"绑定组件"。键名或绑定对象写错时，前端经 `Bridge.call` 打到的是 404 或
错误的方法，而**没有任何静态检查能发现**（表是运行时字典，键是字符串）。

迁移前这条路是隐式覆盖的：用例直接调 `plugin.list_albums()` 之类，等于顺带验证了绑定；
迁移后用例改为调组件方法，绑定本身就必须单独钉住。键的**顺序**也一并锁定 —— 它决定
插件加载后注册的 HTTP 端点顺序，与迁移前必须一致。

运行：
    venv/Scripts/python.exe -m unittest tests.test_image_viewer_api_table -v
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 迁移前的键与顺序（逐字固定）：前端与壳按名字调用，改名即静默 404。
EXPECTED_KEYS = [
    'list_images', 'list_folder_items', 'load_album_tiles', 'list_album_images',
    'list_dir', 'list_albums', 'create_folder', 'get_album_config', 'set_album_config',
    'get_image_info', 'delete_files', 'move_files', 'regenerate_thumbs', 'refresh',
    'rebuild_all', 'rebuild_folder', 'rebuild_status', 'rebuild_cancel',
    'get_settings', 'save_settings', 'get_root_dir', 'diagnose',
    'clear_folder_settings', 'list_roots', 'browse_dir', 'delete_folder',
    'list_recent_images',
]

#: 需要绑定到组件的键 → 组件属性名。其余键留在入口类上（诊断、兼容刷新、根目录视图）。
EXPECTED_OWNER = {
    'list_images': 'listing',
    'list_folder_items': 'listing',
    'list_dir': 'listing',
    'load_album_tiles': 'tiles',
    'list_album_images': 'tiles',
    'list_albums': 'albums',
    'create_folder': 'file_ops',
    'delete_folder': 'file_ops',
    'delete_files': 'file_ops',
    'move_files': 'file_ops',
    'regenerate_thumbs': 'file_ops',
    'get_image_info': 'thumbs',
    'get_album_config': 'album_config',
    'set_album_config': 'album_config',
    'get_settings': 'settings',
    'save_settings': 'settings',
    'clear_folder_settings': 'settings',
    'get_root_dir': 'paths',
    'list_roots': 'paths',
    'browse_dir': 'paths',
    'refresh': 'freshness',
    'rebuild_all': 'rebuild',
    'rebuild_folder': 'rebuild',
    'rebuild_status': 'rebuild',
    'rebuild_cancel': 'rebuild',
    'list_recent_images': 'recent',
}


def _load_plugin(tmp: Path):
    main_path = PROJECT_ROOT / 'plugins' / 'image-viewer' / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('image_viewer_api_table_test',
                                                  str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
    module.ImageViewerPlugin._resolved_config = {'root_dir': str(tmp)}
    try:
        return module.ImageViewerPlugin(
            {'name': 'image-viewer'},
            {'directories': {'data_root': str(tmp)}},
        )
    finally:
        module.ImageViewerPlugin._resolved_config = original


class ApiTableTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plugin = _load_plugin(Path(self._tmp.name))
        self.api = self.plugin.register_api()

    def test_keys_and_order_are_unchanged(self):
        self.assertEqual(list(self.api), EXPECTED_KEYS,
                         'register_api 的键或顺序变了：前端与壳按名字调用，改名即 404')

    def test_every_entry_is_callable(self):
        for key, value in self.api.items():
            self.assertTrue(callable(value), f'{key} 绑定的对象不可调用: {value!r}')

    def test_entries_are_bound_to_their_component(self):
        for key, component in EXPECTED_OWNER.items():
            owner = self.api[key].__self__
            self.assertIs(owner, getattr(self.plugin, component),
                          f'{key} 应绑定到 {component}，实际是 {owner!r}')

    def test_shell_hooks_stay_on_the_entry_class(self):
        """壳按名调用的钩子必须留在入口类上（不经 register_api，也不能只剩组件方法）。"""
        for name in ('get_file_roots', 'resolve_file_path', 'get_data_root',
                     'get_thumb_data', 'ensure_thumb', 'get_cache_dir',
                     'freshness_spec', 'on_settings_changed', 'on_unload'):
            self.assertTrue(callable(getattr(self.plugin, name, None)),
                            f'入口类缺少壳按名调用的成员 {name}')

    def test_companion_contract_members_are_on_the_instance(self):
        """附属插件（image-cleaner）直接读宿主实例：这两个成员必须按实例可调。

        `list_roots()` 供它拿额外根前缀，`delete_files()` 供它复用宿主的删除与缓存清理。
        它们同时在 `register_api` 里绑到组件，两条路径指向同一份实现。
        """
        for name in ('list_roots', 'delete_files'):
            self.assertTrue(callable(getattr(self.plugin, name, None)),
                            f'附属插件按实例调用 {name}()，入口类没提供')
        roots = self.plugin.list_roots()
        self.assertEqual(roots[0]['path'], str(self.plugin.paths.root))

    def test_entry_does_not_override_base_get_settings(self):
        """按目录读设置是 `settings.for_path`，不能覆盖基类的无参 `get_settings()`。"""
        from shell.backend.plugin_base import PluginBase
        self.assertIs(type(self.plugin).get_settings, PluginBase.get_settings,
                      '入口覆盖了 PluginBase.get_settings()：基类的脱敏与默认值语义会失效')
        self.assertEqual(self.plugin.get_settings().get('sort_by'), 'mtime')


if __name__ == '__main__':
    unittest.main()
