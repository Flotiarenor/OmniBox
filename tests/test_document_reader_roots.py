"""文档根目录的旧插件名迁移：写入失败不得报"已迁移"。

`migrate_legacy_root_dir()` 在 `on_load` 里把旧插件名（novel-reader）设置里的
`root_dir` 补写到新名下。原先它不检查 `update_setting` 的返回值：写失败时照样
`log.info('已从 novel-reader 的设置迁移 root_dir')` 并返回旧值，入口据此
`_rebind_roots` —— 本次会话按旧根跑，而新名下仍是空的，下次启动再迁一次，
日志里两次都写着成功。

本文件用真实 `SettingsStore`（临时目录）锁两条路径：

* 写入成功：返回迁来的 root_dir、新名下真有值、日志是"已迁移"，入口会换根；
* 写入失败：返回 None（入口按"没有迁移"处理、不换根）、日志是 warning 且不提
  "已迁移"。

写入失败通过 `SettingsStore` 子类实现：只让**新插件名**的落盘那一步抛
`OSError`（模拟设置目录只读），路径派生、每插件锁与读回仍是真实实现；旧名下的
设置照常写入，因为那是迁移的输入，必须真实存在。

运行：
    venv\\Scripts\\python.exe -m unittest tests.test_document_reader_roots -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'document-reader'
MAIN_FILE = PLUGIN_DIR / 'backend' / 'main.py'

from shell.backend.plugin_utils import load_sibling
from shell.backend.settings_store import SettingsStore

# 与入口装配时同一套加载方式（module_name 相同，因此拿到的就是插件正在用的模块）
_const = load_sibling(str(MAIN_FILE), 'const', 'document_reader')
_roots = load_sibling(str(MAIN_FILE), 'roots', 'document_reader')

LEGACY_NAME = str(_const.LAYOUT.legacy_plugin)
NEW_NAME = 'document-reader'


class _WriteBlockedStore(SettingsStore):
    """真实存储，只让指定插件名的落盘失败（模拟设置目录只读）。"""

    def __init__(self, settings_dir: str, blocked_name: str) -> None:
        super().__init__(settings_dir)
        self._blocked = f'{blocked_name}.json'

    def _atomic_write(self, file: Path, values: Dict) -> None:
        if file.name == self._blocked:
            raise OSError('设置目录只读（用例注入）')
        super()._atomic_write(file, values)


class RootDirMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.docs = self.home / '文档'
        self.docs.mkdir()
        self.settings_dir = self.home / '.config' / 'plugins'
        self.data_root = self.home / 'data'
        self.data_root.mkdir()

    def _plugin(self, store: Any) -> Any:
        from tools.check_plugins import _load_backend_class

        # 不喂启动预设（`_resolved_config` 留空）：`setting('root_dir')` 应当只在设置
        # 存储里找，这才走迁移分支
        cls = _load_backend_class(PLUGIN_DIR, 'backend/main.py', 'DocumentReaderPlugin')
        plugin = cls({'name': NEW_NAME},
                     {'directories': {'data_root': str(self.data_root)}})
        plugin._settings_store = store
        return plugin

    def _migrate(self, plugin, store):
        return _roots.migrate_legacy_root_dir(
            plugin, store, plugin.name, LEGACY_NAME, plugin.update_setting)

    def test_success_migrates_and_logs(self):
        store = SettingsStore(str(self.settings_dir))
        store.set(LEGACY_NAME, {'root_dir': str(self.docs)})
        plugin = self._plugin(store)

        with self.assertLogs('document_reader_roots', level='INFO') as captured:
            migrated = self._migrate(plugin, store)

        self.assertEqual(migrated, str(self.docs))
        self.assertEqual(store.get(NEW_NAME)['root_dir'], str(self.docs),
                         '迁移成功后新插件名下必须有值')
        records = '\n'.join(captured.output)
        self.assertIn('INFO:document_reader_roots', records)
        self.assertIn('已从 novel-reader 的设置迁移 root_dir', records)

    def test_write_failure_returns_none_without_claiming_migration(self):
        store = _WriteBlockedStore(str(self.settings_dir), NEW_NAME)
        store.set(LEGACY_NAME, {'root_dir': str(self.docs)})
        plugin = self._plugin(store)

        with self.assertLogs('document_reader_roots', level='INFO') as captured:
            migrated = self._migrate(plugin, store)

        self.assertIsNone(migrated, '写失败时必须返回 None，让入口按"没有迁移"处理')
        self.assertEqual(store.get(NEW_NAME), {}, '写失败时新插件名下不该有内容')
        records = '\n'.join(captured.output)
        self.assertIn('WARNING:', records)
        self.assertIn('root_dir', records, '失败日志要点出设置键')
        self.assertIn('设置未写入', records, '失败日志要说明原因')
        self.assertIn(LEGACY_NAME, records)
        self.assertNotIn('已迁移', records, '写失败时不能打"已迁移"')

    def test_on_load_keeps_default_root_when_write_fails(self):
        """入口拿到 None = "没有迁移"：不得按旧插件名的根改根。"""
        store = _WriteBlockedStore(str(self.settings_dir), NEW_NAME)
        store.set(LEGACY_NAME, {'root_dir': str(self.docs)})
        plugin = self._plugin(store)
        default_root = self.data_root.resolve()
        self.assertEqual(Path(plugin.roots.document_dir), default_root)

        with self.assertLogs('document_reader_roots', level='INFO'):
            plugin.on_load()

        self.assertEqual(Path(plugin.roots.document_dir), default_root,
                         '迁移写失败后入口仍按旧根改根了')
        self.assertNotEqual(Path(plugin.roots.document_dir), self.docs.resolve())

    def test_on_load_rebinds_after_successful_migration(self):
        store = SettingsStore(str(self.settings_dir))
        store.set(LEGACY_NAME, {'root_dir': str(self.docs)})
        plugin = self._plugin(store)

        with self.assertLogs('document_reader_roots', level='INFO'):
            plugin.on_load()

        self.assertEqual(Path(plugin.roots.document_dir), self.docs.resolve(),
                         '迁移成功后入口应按迁来的根换根')


if __name__ == '__main__':
    unittest.main()
