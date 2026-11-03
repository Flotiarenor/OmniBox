"""`register_api` 的表与壳钩子在组件化之后必须逐字不变，并且真的绑到对应组件上。

为什么需要这条用例：拆分把接口实现从入口类上的方法移到了组件，`register_api` 从
"绑定自己"变成"绑定组件"。键名或绑定对象写错时，前端经 `Bridge.call` 打到的是 404 或
错误的方法，而**没有任何静态检查能发现**（表是运行时字典，键是字符串）。迁移前这条路是
隐式覆盖的：用例直接调 `plugin.duplicate_scan()` 之类，等于顺带验证了绑定；迁移后用例如
改调组件方法，绑定本身就必须单独钉住。键的**顺序**也一并锁定 —— 它决定插件加载后注册的
HTTP 端点顺序，与迁移前必须一致。

同时钉住两件最容易静默退化的事：壳按名调用的钩子仍在入口类上；`get_settings` 仍是入口的
覆写（它要补只读信息行 `root_dir`，且必须调用 `super().get_settings()`）。

运行：
    venv/Scripts/python.exe -m unittest tests.test_image_cleaner_api_table -v
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
    'duplicate_scan', 'similar_scan', 'scan_start', 'scan_status', 'scan_cancel',
    'get_cached_scan', 'delete_files', 'get_status', 'get_settings', 'save_settings',
]

#: 绑定到组件的键 → (组件属性名, 组件方法名)。其余键留在入口类上。
EXPECTED_OWNER = {
    'duplicate_scan': ('pipeline', 'duplicate_scan'),
    'similar_scan': ('pipeline', 'similar_scan'),
    'scan_start': ('scans', 'start'),
    'scan_status': ('scans', 'status'),
    'scan_cancel': ('scans', 'cancel'),
    'get_cached_scan': ('results', 'get_cached_scan'),
}

#: 壳按名调用（不经 register_api）的钩子，必须仍在入口类上。
SHELL_HOOKS = [
    'get_data_root', 'get_file_roots', 'ensure_thumb', 'get_thumb_data',
    'get_extensions', 'freshness_spec', 'on_settings_changed', 'on_unload',
]


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(PROJECT_ROOT / rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ImageCleanerApiTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load('plugins/image-cleaner/backend/main.py', 'ic_main_api_table_test')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'data'
        self.root.mkdir(parents=True, exist_ok=True)

    def _plugin(self):
        return self.module.ImageCleanerPlugin(
            {'name': 'image-cleaner'},
            {'directories': {'data_root': str(self.root)}},
        )

    def test_keys_and_order_are_unchanged(self):
        api = self._plugin().register_api()
        self.assertEqual(list(api), EXPECTED_KEYS,
                         '键名或顺序变了，前端与壳按名字调用会静默 404')

    def test_keys_are_bound_to_the_expected_component(self):
        plugin = self._plugin()
        api = plugin.register_api()
        for key, (owner, method) in EXPECTED_OWNER.items():
            bound = api[key]
            self.assertIs(getattr(bound, '__self__', None), getattr(plugin, owner),
                          f'{key} 应绑定到 {owner}.{method}')
            self.assertEqual(getattr(bound, '__name__', ''), method,
                             f'{key} 绑定的方法名不对')

    def test_entry_owned_keys_stay_on_the_plugin_instance(self):
        plugin = self._plugin()
        api = plugin.register_api()
        for key in ('delete_files', 'get_status', 'get_settings', 'save_settings'):
            bound = api[key]
            self.assertIs(getattr(bound, '__self__', None), plugin,
                          f'{key} 必须留在入口类上')
            self.assertEqual(getattr(bound, '__func__', None), getattr(plugin, key).__func__,
                             f'{key} 绑定的实现应就是入口类上的方法')

    def test_shell_hooks_stay_on_the_entry_class(self):
        for name in SHELL_HOOKS:
            self.assertTrue(callable(getattr(self.module.ImageCleanerPlugin, name, None)),
                            f'壳按名调用 {name}，它必须仍在入口类上')

    def test_get_settings_override_is_kept(self):
        """只读信息行 `root_dir` 由入口的覆写补上；覆写必须留下（它调用 super()）。"""
        base = self.module.PluginBase
        self.assertIn('get_settings', self.module.ImageCleanerPlugin.__dict__)
        self.assertIsNot(self.module.ImageCleanerPlugin.get_settings, base.get_settings)


if __name__ == '__main__':
    unittest.main()
