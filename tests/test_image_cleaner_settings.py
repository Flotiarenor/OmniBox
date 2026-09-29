"""image-cleaner 的设置 API 契约：登记、只读信息行、可写键。

背景（真实缺陷，三个提交都没发现的静默失效）：`settings_schema` 声明了 `root_dir`
（`type: "info"`，值由 `get_settings()` 现取）与 `threshold`，前端既请宿主渲染设置弹窗
（`HostChannel.requestSettings` → `Bridge.call('get_settings')`），保存也走
`Bridge.call('save_settings')` —— 而 `register_api()` 里两个都没登记。
`PluginManager` 只会为每个插件额外登记 `<插件>__get_settings_schema`
（`plugin_manager.py:566-572`），桌面模式的 `js_api` 用的是同一张表（`main.py:231-243`）。
实测 `POST /api/image-cleaner__get_settings` 与 `__save_settings` 都是 HTTP 404，后果是：

  * 设置弹窗里的 `root_dir` 只读信息行**永远是空的**（取不到 values → schema 又没有
    `value`/`default`），而同一批改动把内嵌态那条唯一显示根目录的工具栏也删了
    —— 根目录在界面上彻底看不到；
  * `threshold` 永远等于 schema 默认值 8（`similar_scan` 走 `self.setting('threshold', 8)`），
    点保存必然"保存失败"。

本用例用真实插件实例锁住三件事：API 已登记；信息行现取宿主根目录（多根逐行）；
`save_settings()` 收下 threshold、拒收 info 键（`PluginBase.save_settings` 的
declared/allowed 判据）。全插件的同一契约另有 `tools/check_plugins.py` 的静态门禁与
`tests/test_plugin_fresh_install.py` 的真机检查。

运行：
    python -m unittest tests.test_image_cleaner_settings -v
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.settings_store import SettingsStore


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(PROJECT_ROOT / rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_image(path: Path) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (16, 16), (10, 120, 200)).save(path)


class ImageCleanerSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer_module = _load('plugins/image-viewer/backend/main.py', 'iv_main_settings_test')
        cls.cleaner_module = _load('plugins/image-cleaner/backend/main.py', 'ic_main_settings_test')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 先规范化一次：信息行的值来自宿主 resolve() 过的根目录，而 Windows runner 的
        # `%TEMP%` 是 8.3 短名（`C:\Users\RUNNER~1\…`）。不规范化会拿短名比长名。
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / 'data'
        self.extra = self.tmp / '外部' / '额外图库'
        self._settings_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._settings_tmp.cleanup)
        self.settings_dir = Path(self._settings_tmp.name)

    def _viewer(self, extra_roots: str = ''):
        module = self.viewer_module
        original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
        module.ImageViewerPlugin._resolved_config = {
            'root_dir': str(self.root), 'extra_roots': extra_roots}
        try:
            plugin = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = original
        plugin._settings_store = SettingsStore(str(self.settings_dir))
        return plugin

    def _cleaner(self, host):
        plugin = self.cleaner_module.ImageCleanerPlugin(
            {'name': 'image-cleaner'},
            {'directories': {'data_root': str(self.root)}},
        )
        plugin._host = host
        plugin._settings_store = SettingsStore(str(self.settings_dir))
        return plugin

    # ---------- 1. API 登记（前端两条 Bridge.call 的前提）----------

    def test_settings_api_is_registered(self):
        """`get_settings` / `save_settings` 必须挂在 register_api() 上。

        少了它们，`POST /api/image-cleaner__get_settings` 是 404，而前端的
        `requestSettings()` 与 `Bridge.call('save_settings', …)` 都按方法名直呼。
        `get_settings_schema` 由 PluginManager 自动登记，不在这里断言。
        """
        api = set(self._cleaner(self._viewer()).register_api())
        self.assertIn('get_settings', api,
                      '设置弹窗要靠它取当前值（漏登记时信息行空白、字段全是默认值）')
        self.assertIn('save_settings', api,
                      '设置弹窗保存走 Bridge.call(\'save_settings\')，漏登记必然保存失败')

    # ---------- 2. 只读信息行现取宿主根目录 ----------

    def test_info_row_shows_host_roots(self):
        _make_image(self.root / 'a.jpg')
        _make_image(self.extra / 'a.jpg')
        cleaner = self._cleaner(self._viewer(str(self.extra)))

        settings = cleaner.get_settings()
        self.assertEqual(settings['root_dir'].splitlines(), [str(self.root), str(self.extra)],
                         '信息行取的是宿主当前的根目录，多根时逐行')
        self.assertEqual(settings['threshold'], 8, '可写字段照常带 schema 默认值')

    def test_info_row_is_empty_when_host_is_unavailable(self):
        """宿主不可用时信息行留空，但不能因此让整个设置弹窗打不开。"""
        cleaner = self.cleaner_module.ImageCleanerPlugin(
            {'name': 'image-cleaner', 'dependencies': ['image-viewer']},
            {'directories': {'data_root': str(self.root)}},
        )
        # 声明了依赖但没有 PluginManager（等价于宿主没加载）：_get_host() 抛 RuntimeError
        self.assertIsNone(cleaner._plugin_manager)
        self.assertEqual(cleaner.get_settings()['root_dir'], '')

    # ---------- 3. 保存：收可写键、拒 info 键 ----------

    def test_save_keeps_info_key_out_and_stores_threshold(self):
        _make_image(self.root / 'a.jpg')
        cleaner = self._cleaner(self._viewer())

        result = cleaner.save_settings({'root_dir': 'D:\\别的目录', 'threshold': 4})
        self.assertTrue(result.get('success'), result)

        stored = cleaner._settings_store.get('image-cleaner') or {}
        self.assertEqual(stored.get('threshold'), 4, '可写字段必须落盘')
        self.assertNotIn('root_dir', stored,
                         'info 是展示行，不能因为一次手写 API 调用就写进设置文件')
        self.assertEqual(cleaner.get_settings()['root_dir'], str(self.root),
                         '信息行的值始终来自宿主，不受提交内容影响')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
