"""`check_plugins` 的「附属插件按实例取宿主成员」契约。

为什么需要：image-viewer 组件化时，`delete_files` / `list_roots` 的实现搬进了组件，宿主
实例上就没有这两个成员了；image-cleaner 是按实例取的（`host.delete_files(...)` 与
`getattr(host, 'list_roots')`），而 `getattr` 带默认值 —— 漏掉**不报错**，只是静默退回
单根，表现是"额外图库里的文件不参与扫描"。这条用例把该判据钉住：

1. 宿主没有的成员 → 报错并指出成员名与行号；
2. 宿主 `register_api` 里有的成员 → 通过；
3. `getattr(host, '<成员>')` 与宿主实例上直接调用同样受检；
4. `PluginBase` 提供的成员（如 `name`）不算缺失；
5. 没有声明 `dependencies` 的插件不参与此项检查。

运行：python -m unittest tests.test_check_companion_contract -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.check_plugins import check_plugins

_HOST_TEMPLATE = '''from shell.backend.plugin_base import PluginBase


class HostPlugin(PluginBase):
    def register_api(self):
        return {
__KEYS__
        }

    def list_roots(self):
        return []

    def get_data_root(self):
        return None
'''

_COMPANION_TEMPLATE = '''from shell.backend.plugin_base import PluginBase


class CompanionPlugin(PluginBase):
    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self._host = None

    def _get_host(self):
        if self._host is None:
            self._host = self.get_dependency('host-plugin')
        return self._host

    def delete(self, paths):
        return self._get_host().delete_files(paths)

    def roots(self):
        lister = getattr(self._get_host(), 'list_roots', None)
        return lister() if callable(lister) else []

__EXTRA__
'''


def _plugin(plugins: Path, name: str, entry_body: str, dependencies: list) -> None:
    backend = plugins / name / 'backend'
    backend.mkdir(parents=True)
    (plugins / name / 'manifest.json').write_text(json.dumps({
        'name': name,
        'version': '1.0.0',
        'displayName': name,
        'backend': {'entry': 'backend/main.py', 'class': 'X'},
        'frontend': {'route': f'/{name}', 'entry': 'frontend/index.html'},
        'dependencies': dependencies,
    }), encoding='utf-8')
    (backend / 'main.py').write_text(entry_body, encoding='utf-8')


class CompanionHostContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plugins = Path(self._tmp.name) / 'plugins'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _build(self, *, expose_delete: bool = True, extra: str = '') -> list[str]:
        keys = ("            'delete_files': self.delete_files,\n"
                if expose_delete else '')
        _plugin(self.plugins, 'host-plugin',
                _HOST_TEMPLATE.replace('__KEYS__', keys), [])
        _plugin(self.plugins, 'companion',
                _COMPANION_TEMPLATE.replace('__EXTRA__', extra), ['host-plugin'])
        errors, _warnings = check_plugins(self.plugins, load_backends=False)
        return [error for error in errors if '按实例从宿主' in error]

    def test_missing_host_member_is_reported(self):
        errors = self._build(expose_delete=False)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('delete_files', errors[0])
        self.assertIn('companion/backend/main.py', errors[0])
        # 消息里带行号，便于直接跳到调用点
        self.assertRegex(errors[0], r'第 \d+ 行按实例从宿主')

    def test_register_api_key_counts_as_exposed(self):
        self.assertEqual(self._build(), [])

    def test_getattr_on_missing_member_is_reported(self):
        errors = self._build(extra="    def extra(self):\n"
                                  "        return getattr(self._get_host(), 'missing_one', None)\n")
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('missing_one', errors[0])

    def test_plugin_base_member_is_not_missing(self):
        errors = self._build(extra="    def host_name(self):\n"
                                  "        return self._get_host().name\n")
        self.assertEqual(errors, [])

    def test_plugin_without_dependencies_is_not_checked(self):
        _plugin(self.plugins, 'solo',
                _COMPANION_TEMPLATE.replace('__EXTRA__', ''), [])
        errors, _warnings = check_plugins(self.plugins, load_backends=False)
        self.assertEqual([error for error in errors if '按实例从宿主' in error], [])


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
