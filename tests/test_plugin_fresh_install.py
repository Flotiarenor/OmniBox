"""空白实例（新装用户的第一次启动）必须把所有内置插件都加载起来。

这条守卫抓到过一个真实缺陷：image-viewer 在**没有任何设置**时构造抛
`AttributeError: 'ImageViewerPlugin' object has no attribute 'root_dir'`
（`super().get_data_root()` 在以 mixin 为主的 MRO 里命中了 `ThumbMixin.get_data_root`，
而它读的正是那一行要算的 `self.root_dir`），连带声明 `dependencies: ["image-viewer"]`
的 pixiv-sync 一起加载失败。开发机上被 `.config/plugins/image-viewer.json` 里的历史
`root_dir` 掩盖 —— 只有"从零起一个实例"才看得见，而这正是夹具在做的事。

单个实例就够：插件加载与团体无关，不必付两个实例的启动成本。

同时守住第二条（同一个实例，不多付一次启动）：**声明了可写设置项的插件，设置 API 必须
真的可达**。`get_settings` / `save_settings` 需要各自登记进 `register_api()`（PluginManager
只自动补 `get_settings_schema`），漏登记时设置弹窗打得开、字段全是默认值、保存必然失败，
全程无报错 —— image-cleaner 与 group-mesh 都这样漏过。静态的同一条门禁见
`tools/check_plugins.py`。

运行：
    venv/Scripts/python -m unittest tests.test_plugin_fresh_install -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.harness.app_instance import AppInstance, boot_prerequisites
from tests.harness.support import cleanup_tree

_BOOT_OK, _BOOT_REASON = boot_prerequisites()


@unittest.skipUnless(_BOOT_OK, f'本机无法启动应用实例（{_BOOT_REASON}）')
class FreshInstallPluginLoadTest(unittest.TestCase):
    """一次启动，两条守卫：插件都加载起来 + 设置链路真的可达。"""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix='omnibox-fresh-'))
        cls.instance = AppInstance('fresh', cls.root).start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.instance.stop()
        finally:
            cleanup_tree(cls.root)

    def test_blank_instance_loads_every_bundled_plugin(self):
        status = self.instance.system('system_get_plugin_status')
        self.assertEqual(status.get('failures'), [],
                         f"空白实例里有插件加载失败: {status.get('failures')}")
        loaded = status.get('loaded') or []
        self.assertIn('image-viewer', loaded)
        self.assertIn('pixiv-sync', loaded,
                      'pixiv-sync 声明依赖 image-viewer，两者必须一起起来')

    def test_every_plugin_with_settings_exposes_settings_api(self):
        """声明了可写设置项的插件，`get_settings` / `save_settings` 必须真的可达。

        背景：壳的设置弹窗（插件自己的 `openSettingsModal`、内嵌页的
        `HostChannel.requestSettings`）走 `Bridge.call('get_settings')` 与
        `Bridge.call('save_settings')`，即 `POST /api/<插件>__<方法>`；而
        `PluginManager` 只会额外登记 `<插件>__get_settings_schema`
        （plugin_manager.py:566-572）。漏登记的表现完全是静默的：弹窗打得开、每个字段
        显示 schema 默认值、点保存必然"保存失败" —— image-cleaner 与 group-mesh 都这样
        漏过（实测这两个端点 404）。静态的同一条门禁见 tools/check_plugins.py。
        """
        status = self.instance.system('system_get_plugin_status')
        checked = []
        for plugin in sorted(status.get('loaded') or []):
            schema = self.instance.call(plugin, 'get_settings_schema') or []
            writable = [item for item in schema
                        if isinstance(item, dict) and item.get('key')
                        and item.get('type') != 'info']
            if not writable:
                continue                      # 没有可写设置项，不会弹设置窗
            values = self.instance.call(plugin, 'get_settings')
            self.assertIsInstance(values, dict, f'{plugin} 的 get_settings 必须返回字典')
            saved = self.instance.call(plugin, 'save_settings', values)
            self.assertNotEqual(saved.get('success'), False,
                                f'{plugin} 保存自身当前设置失败: {saved}')
            checked.append(plugin)
        self.assertIn('image-cleaner', checked,
                      'image-cleaner 声明了 threshold，必须被这条守卫覆盖')


if __name__ == '__main__':
    unittest.main()
