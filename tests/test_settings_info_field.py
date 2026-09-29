"""`type: "info"` 只读信息行在后端一侧的契约（`PluginBase.save_settings`）。

背景：image-cleaner 的设置弹窗要显示一行"相册根目录"——它是宿主 image-viewer 的数据根
目录，在插件里不可改，所以 schema 里声明成 `type: "info"`，值由 `get_settings()` 现取。
前端（`createSettingsForm`）不会提交这一行，但"前端不提交"不是契约：`/api/<插件>__save_settings`
是同源可调的，一次手写的请求就能把展示用的键写进设置文件（下次 `get_settings()` 里就多出
一个来自磁盘的假根目录）。因此可写键的判定放在基类里。

同时锁住一条容易写错的边界：只声明 info 字段的插件，`allowed` 会算成空集 —— 此时**不能**
退回"什么键都收"的旧分支。

运行：
    python -m unittest tests.test_settings_info_field -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.plugin_base import PluginBase
from shell.backend.settings_store import SettingsStore


class _Plugin(PluginBase):
    def __init__(self, manifest, config, schema):
        super().__init__(manifest, config)
        self.settings_schema = schema
        self.changed_calls: list = []

    def register_api(self):
        return {'get_settings': self.get_settings, 'save_settings': self.save_settings}

    def on_settings_changed(self, changed_keys):
        self.changed_calls.append(set(changed_keys))


WITH_WRITABLE = [
    {'key': 'root_dir', 'label': '相册根目录', 'type': 'info'},
    {'key': 'threshold', 'label': '相似判定阈值', 'type': 'range', 'default': 8},
]
ONLY_INFO = [{'key': 'root_dir', 'label': '相册根目录', 'type': 'info'}]


class InfoFieldSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _plugin(self, schema):
        plugin = _Plugin(
            {'name': 'demo'},
            {'directories': {'data_root': str(self.tmp / 'data')}},
            schema,
        )
        plugin._settings_store = SettingsStore(str(self.tmp / 'config' / 'plugins'))
        return plugin

    def _stored(self, plugin) -> dict:
        """磁盘上真正存下来的键（`_raw_settings()` 会把 schema 的 default 一起合并进来，
        所以判"有没有落盘"必须看 store，不能看 _raw_settings）。"""
        return plugin._settings_store.get('demo') or {}

    def test_info_key_is_not_persisted(self):
        plugin = self._plugin(WITH_WRITABLE)
        result = plugin.save_settings({'root_dir': 'D:\\somewhere-else', 'threshold': 4})
        self.assertTrue(result.get('success'), result)
        self.assertNotIn('root_dir', self._stored(plugin), 'info 字段被写进设置文件了')
        self.assertEqual(self._stored(plugin)['threshold'], 4, '普通字段照常落盘')

    def test_info_key_is_not_reported_as_changed(self):
        plugin = self._plugin(WITH_WRITABLE)
        plugin.save_settings({'root_dir': 'D:\\somewhere-else', 'threshold': 4})
        self.assertEqual(plugin.changed_calls, [{'threshold'}])

    def test_plugin_declaring_only_info_fields_accepts_nothing(self):
        plugin = self._plugin(ONLY_INFO)
        plugin.save_settings({'root_dir': 'D:\\somewhere-else', 'anything': 1})
        self.assertEqual(self._stored(plugin), {}, '没有可写字段时不该退回"什么键都收"')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
