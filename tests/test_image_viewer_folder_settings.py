"""image-viewer 文件夹级设置的 number/range 契约。

背景：`save_folder_settings`（对外注册为 `save_settings`）在 `rel_path` 非空时
直接把 payload 写进 `folders[rel_path]` 并返回 `success:true`，绕过了
`PluginBase.save_settings` 的 min/max 校验。于是越界/空的 `row_height`/`per_page`/
`lightbox_cache` 会原样落盘，前端 `_saveStep` 看不到失败（P2-10），用户以为保存成功。

本文件锁住：文件夹级保存复用同一套数字校验 —— 任一字段非法则整次不落盘并返回
字段级 `errors`；合法数字字符串归一成数字后落盘；全局保存路径不受影响。
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

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'image-viewer'


def _load_plugin_class():
    path = PLUGIN_DIR / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('image_viewer_folder_settings_test', str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ImageViewerPlugin


class FolderSettingsValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin_cls = _load_plugin_class()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / '图库'
        cls.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self._settings_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._settings_tmp.cleanup)
        self.settings_dir = Path(self._settings_tmp.name)

    def _plugin(self):
        cls = self.plugin_cls
        original = getattr(cls, '_resolved_config', None)
        cls._resolved_config = {'root_dir': str(self.root)}
        try:
            plugin = cls(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            cls._resolved_config = original
        plugin._settings_store = SettingsStore(str(self.settings_dir))
        return plugin

    def _folders(self, plugin):
        return plugin._settings_store.get('image-viewer').get('folders', {})

    # ---- 红：非法的文件夹级数字必须拒绝且不落盘 ----

    def test_invalid_numbers_are_rejected_and_not_persisted(self):
        plugin = self._plugin()
        result = plugin.save_folder_settings('作者A/作品1', {
            'row_height': 9999,      # max 400
            'per_page': 5,           # min 10
            'lightbox_cache': -1,    # min 0
        })
        self.assertFalse(result.get('success'), result)
        errors = result.get('errors') or {}
        self.assertEqual(set(errors), {'row_height', 'per_page', 'lightbox_cache'}, errors)
        self.assertTrue(all(errors.values()), errors)
        self.assertNotIn('作者A/作品1', self._folders(plugin), '非法保存不得写盘')

    def test_valid_numbers_are_normalized_before_persisting(self):
        plugin = self._plugin()
        result = plugin.save_folder_settings('作者A/作品2', {
            'row_height': '250',
            'per_page': '20',
            'sort_by': 'name',
        })
        self.assertTrue(result.get('success'), result)
        stored = self._folders(plugin)['作者A/作品2']
        self.assertEqual(stored['row_height'], 250)
        self.assertIsInstance(stored['row_height'], int)
        self.assertEqual(stored['per_page'], 20)
        self.assertIsInstance(stored['per_page'], int)
        self.assertEqual(stored['sort_by'], 'name')
        self.assertEqual(plugin.get_settings('作者A/作品2')['row_height'], 250)

    def test_invalid_save_keeps_previous_folder_settings(self):
        plugin = self._plugin()
        self.assertTrue(plugin.save_folder_settings('作者A/作品3', {
            'row_height': 300, 'sort_by': 'name',
        }).get('success'))
        result = plugin.save_folder_settings('作者A/作品3', {
            'row_height': 9999, 'sort_by': 'time_name',
        })
        self.assertFalse(result.get('success'), result)
        stored = self._folders(plugin)['作者A/作品3']
        self.assertEqual(stored['row_height'], 300, '失败保存不得覆盖已有值')
        self.assertEqual(stored['sort_by'], 'name')

    def test_global_scope_and_admin_keys_do_not_leak_into_folder(self):
        plugin = self._plugin()
        result = plugin.save_folder_settings('作者A/作品4', {
            'root_dir': 'D:/不该写进文件夹',
            'extra_roots': 'D:/也不该',
            'row_height': 200,
            '不存在的键': 'x',
        })
        self.assertTrue(result.get('success'), result)
        stored = self._folders(plugin)['作者A/作品4']
        self.assertNotIn('root_dir', stored)
        self.assertNotIn('extra_roots', stored)
        self.assertNotIn('不存在的键', stored)
        self.assertEqual(stored['row_height'], 200)

    def test_global_save_path_still_validates(self):
        plugin = self._plugin()
        bad = plugin.save_folder_settings('', {'row_height': 9999})
        self.assertFalse(bad.get('success'), bad)
        self.assertIn('row_height', (bad.get('errors') or {}))
        good = plugin.save_folder_settings('', {'row_height': 300, 'per_page': 60})
        self.assertTrue(good.get('success'), good)
        self.assertEqual(plugin.get_settings('')['row_height'], 300)


class ManifestChromeTests(unittest.TestCase):
    """迁移尾项：`frontend.chrome.content` 是壳未消费的死声明，已删除。"""

    def test_manifest_chrome_has_no_content_field(self):
        import json

        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        chrome = manifest['frontend']['chrome']
        self.assertNotIn('content', chrome)
        # 其它骨架字段仍必须在（删的是死字段，不是整块声明）
        for key in ('brand', 'nav', 'toolbar', 'search', 'status', 'freshness', 'extensions'):
            self.assertIn(key, chrome, key)

    def test_status_kind_is_dot_text(self):
        import json

        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        status = manifest['frontend']['chrome']['status']
        self.assertEqual(status, {'kind': 'dot-text'})


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
