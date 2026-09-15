"""插件申报受保护路径的契约测试（`PluginBase.get_protected_paths`）。

背景：文件路由的放行依据是插件自己给出的根（`get_file_roots()` / `thumb_dir`），
而根可以由插件设置改写。开发模式下 `<data_root>`（默认 ./data）与 `<config_dir>`
（./.config）是兄弟目录 —— 于是"某个插件的根恰好覆盖了配置目录"是结构性的，
Shell 无法靠猜避免。唯一可靠的信息来源是插件自己**申报**：它只说"什么是敏感的"，
执行点始终在 Shell（见 shell/backend/file_server.py 的 `_reject_protected_file`）。

本文件锁住三件事：
  1. 常见情况一行都不用写：设置项声明 `"secret": True` 即自动申报设置文件；
  2. 申报的路径规则与 `SettingsStore` **同一处定义**（不要各拼一份 `<name>.json`）；
  3. 越界申报被忽略并留痕，而不是被信任 —— 一个笔误不该把整个文件服务钉死。

文件路由的端到端行为（403）由 tests/test_file_server_paths.py 覆盖。

运行：
    python -m unittest tests.test_protected_paths -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_manager import collect_protected_paths
from shell.backend.settings_store import SettingsStore


class _DeclaringPlugin(PluginBase):
    """最小插件：只声明 schema，用于驱动 get_protected_paths 的默认实现。"""

    def __init__(self, manifest, config, schema=None):
        super().__init__(manifest, config)
        self.settings_schema = schema if schema is not None else []

    def register_api(self):
        return {}


def _make_plugin(tmp: Path, name: str, schema):
    config = {'directories': {'data_root': str(tmp / 'data')}}
    plugin = _DeclaringPlugin({'name': name}, config, schema=schema)
    plugin._settings_store = SettingsStore(str(tmp / 'config' / 'plugins'))
    return plugin


class ProtectedPathsDeclarationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_no_secret_flag_protects_nothing(self):
        """没声明 secret 的插件行为完全不变：申报是可选能力。"""
        plugin = _make_plugin(self.tmp, 'demo', [
            {'key': 'root_dir', 'label': '根目录', 'type': 'text'},
            {'key': 'per_page', 'label': '每页', 'type': 'number'},
        ])
        self.assertEqual(plugin.get_protected_paths(), [])

    def test_secret_flag_protects_the_settings_file(self):
        """`"secret": True` → 自动申报本插件的统一设置文件（凭据就存在那里）。"""
        plugin = _make_plugin(self.tmp, 'demo', [
            {'key': 'root_dir', 'label': '根目录', 'type': 'text'},
            {'key': 'refresh_token', 'label': '令牌', 'type': 'text', 'secret': True},
        ])
        protected = plugin.get_protected_paths()
        self.assertEqual(len(protected), 1)
        self.assertEqual(protected[0].name, 'demo.json')

    def test_path_rule_comes_from_settings_store(self):
        """路径只由 SettingsStore 定义一次：两处各拼一份就会静默指向不存在的文件。"""
        plugin = _make_plugin(self.tmp, 'demo', [
            {'key': 'a_token', 'label': '令牌', 'type': 'text', 'secret': True},
        ])
        expected = plugin._settings_store.path_for('demo')
        self.assertEqual(plugin.get_protected_paths(), [expected])

    def test_no_settings_store_protects_nothing(self):
        """没有设置存储（例如纯内存的最小插件）时不得抛异常。"""
        plugin = _make_plugin(self.tmp, 'demo', [
            {'key': 'a_token', 'label': '令牌', 'type': 'text', 'secret': True},
        ])
        plugin._settings_store = None
        self.assertEqual(plugin.get_protected_paths(), [])

    def test_invalid_plugin_name_is_reported_not_raised(self):
        """插件名非法（SettingsStore 会拒）时降级为空申报，不能把加载流程带下水。"""
        plugin = _make_plugin(self.tmp, '../escape', [
            {'key': 'a_token', 'label': '令牌', 'type': 'text', 'secret': True},
        ])
        self.assertEqual(plugin.get_protected_paths(), [])


class ProtectedPathsAggregationTests(unittest.TestCase):
    """聚合与边界校验：`collect_protected_paths()`（PluginManager 与测试桩共用）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config_dir = self.tmp / 'config' / 'plugins'
        self.instances: dict = {}

    def _register(self, name, schema, class_data_root=None):
        instance = _DeclaringPlugin(
            {'name': name}, {'directories': {'data_root': str(self.tmp / 'data')}}, schema=schema)
        instance._settings_store = SettingsStore(str(self.config_dir))
        if class_data_root is not None:
            instance.get_data_root = lambda: Path(class_data_root)
        self.instances[name] = instance
        return instance

    def _collect(self):
        return collect_protected_paths(self.instances, self.config_dir)

    def _secret_schema(self, key='refresh_token'):
        return [{'key': key, 'label': '令牌', 'type': 'text', 'secret': True}]

    def test_aggregates_declarations_from_all_plugins(self):
        self._register('alpha', self._secret_schema())
        self._register('beta', self._secret_schema('api_token'))
        names = sorted(path.name for path in self._collect())
        self.assertEqual(names, ['alpha.json', 'beta.json'])

    def test_declaration_inside_plugin_data_root_is_accepted(self):
        data_root = self.tmp / 'data' / 'alpha'
        instance = self._register('alpha', self._secret_schema(), class_data_root=data_root)
        self.assertEqual(instance.get_protected_paths(), [self.config_dir / 'alpha.json'])
        self.assertEqual(len(self._collect()), 1)

    def test_out_of_bounds_declaration_is_ignored(self):
        """申报既不在自身数据根、也不在 <config>/plugins 之下的路径 → 忽略。"""
        instance = self._register('alpha', [])
        instance.get_protected_paths = lambda: [Path(tempfile.gettempdir())]
        self.assertEqual(self._collect(), [])

    def test_failing_plugin_does_not_break_aggregation(self):
        """一个插件申报时抛异常，不得连累其他插件的申报。"""
        bad = self._register('bad', [])
        bad.get_protected_paths = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        self._register('good', self._secret_schema())
        names = [path.name for path in self._collect()]
        self.assertEqual(names, ['good.json'])

    def test_non_list_return_is_ignored(self):
        instance = self._register('alpha', [])
        instance.get_protected_paths = lambda: 'not-a-list'
        self.assertEqual(self._collect(), [])

    def test_unresolvable_entry_is_ignored(self):
        """申报里混入无法解析的项（None / 非路径类型）只跳过该项，不影响其余。"""
        instance = self._register('alpha', [])
        instance.get_protected_paths = lambda: [None, 42, self.config_dir / 'alpha.json']
        names = [path.name for path in self._collect()]
        self.assertEqual(names, ['alpha.json'])


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
