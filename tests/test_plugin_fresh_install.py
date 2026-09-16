"""空白实例（新装用户的第一次启动）必须把所有内置插件都加载起来。

这条守卫抓到过一个真实缺陷：image-viewer 在**没有任何设置**时构造抛
`AttributeError: 'ImageViewerPlugin' object has no attribute 'root_dir'`
（`super().get_data_root()` 在以 mixin 为主的 MRO 里命中了 `ThumbMixin.get_data_root`，
而它读的正是那一行要算的 `self.root_dir`），连带声明 `dependencies: ["image-viewer"]`
的 pixiv-sync 一起加载失败。开发机上被 `.config/plugins/image-viewer.json` 里的历史
`root_dir` 掩盖 —— 只有"从零起一个实例"才看得见，而这正是夹具在做的事。

单个实例就够：插件加载与团体无关，不必付两个实例的启动成本。

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
    def test_blank_instance_loads_every_bundled_plugin(self):
        root = Path(tempfile.mkdtemp(prefix='omnibox-fresh-'))
        self.addClassCleanup(cleanup_tree, root)
        with AppInstance('fresh', root) as instance:
            status = instance.system('system_get_plugin_status')
        self.assertEqual(status.get('failures'), [],
                         f"空白实例里有插件加载失败: {status.get('failures')}")
        loaded = status.get('loaded') or []
        self.assertIn('image-viewer', loaded)
        self.assertIn('pixiv-sync', loaded,
                      'pixiv-sync 声明依赖 image-viewer，两者必须一起起来')


if __name__ == '__main__':
    unittest.main()
