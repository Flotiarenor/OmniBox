"""`check_pyright_plugins` 的豁免语义与诊断解析。

为什么要用例：这条门禁是"新插件零错误、老插件挂账"的棘轮。规则写松了不会有症状
（插件可以随便带错误进门），写严了会让未迁移的插件无故变红。两类错误都只在若干周后
才显形，所以把判据钉成用例：

1. 解析：只取 `severity == error`；`plugins/<name>/` 之外的诊断不计；行列从 0 基转成 1 基；
2. 没有豁免文件时，任一条诊断都是错误；
3. 有豁免文件时只留 warning（欠账可见），不阻断；
4. 豁免文件过期（插件已 0 错误）是错误 —— 否则棘轮对该插件永久失效；
5. 孤儿豁免文件是错误；
6. 仓库现有的豁免文件恰好覆盖有错误的插件（防止"改完忘了删"与"新插件偷偷带错误"）。

运行：python -m unittest tests.test_check_pyright_plugins -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import check_pyright_plugins as gate


def _payload(*items: dict) -> dict:
    return {'generalDiagnostics': list(items)}


def _error(path: Path, line: int = 0, column: int = 0,
           message: str = 'boom', rule: str = 'reportX') -> dict:
    return {
        'file': str(path),
        'severity': 'error',
        'message': message,
        'rule': rule,
        'range': {'start': {'line': line, 'character': column}},
    }


class ParseDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.plugins = self.root / 'plugins'
        self.plugin = self.plugins / 'demo' / 'backend'
        self.plugin.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_error_is_attributed_to_plugin(self):
        diagnostics = gate.parse_diagnostics(
            _payload(_error(self.plugin / 'main.py', line=4, column=8)), self.plugins)
        self.assertEqual(len(diagnostics), 1)
        one = diagnostics[0]
        self.assertEqual(one.plugin, 'demo')
        self.assertTrue(one.where.endswith('main.py:5:9'), one.where)
        self.assertEqual(one.rule, 'reportX')

    def test_warning_and_info_are_ignored(self):
        payload = _payload(
            dict(_error(self.plugin / 'main.py'), severity='warning'),
            dict(_error(self.plugin / 'main.py'), severity='information'),
        )
        self.assertEqual(gate.parse_diagnostics(payload, self.plugins), [])

    def test_files_outside_plugins_are_ignored(self):
        payload = _payload(_error(self.root / 'shell' / 'backend' / 'x.py'))
        self.assertEqual(gate.parse_diagnostics(payload, self.plugins), [])

    def test_multiline_message_is_flattened(self):
        payload = _payload(_error(self.plugin / 'main.py', message='a\n  b'))
        self.assertEqual(gate.parse_diagnostics(payload, self.plugins)[0].message, 'a   b')


class WaiverTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.allowlist = self.root / 'allowlist'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _waive(self, plugin: str) -> None:
        self.allowlist.mkdir(parents=True, exist_ok=True)
        (self.allowlist / f'{plugin}.txt').write_text('# 豁免\n', encoding='utf-8')

    @staticmethod
    def _diagnostic(plugin: str) -> gate.Diagnostic:
        return gate.Diagnostic(
            plugin=plugin, where=f'plugins/{plugin}/backend/main.py:1:1',
            rule='reportX', message='boom')

    def test_error_without_waiver_fails(self):
        errors, warnings = gate.check(
            {'demo': [self._diagnostic('demo')]}, self.allowlist,
            known_plugins=['demo'])
        self.assertEqual(len(errors), 1)
        self.assertIn('plugins/demo/backend/main.py:1:1', errors[0])
        self.assertEqual(warnings, [])

    def test_waiver_turns_errors_into_warning(self):
        self._waive('demo')
        errors, warnings = gate.check(
            {'demo': [self._diagnostic('demo')]}, self.allowlist,
            known_plugins=['demo'])
        self.assertEqual(errors, [])
        self.assertTrue(any('豁免生效中' in warning for warning in warnings), warnings)
        self.assertTrue(any('合计 1 个插件' in warning for warning in warnings), warnings)

    def test_stale_waiver_fails(self):
        """插件已 0 错误却还留着豁免文件：棘轮会永久失效，必须报错。"""
        self._waive('demo')
        errors, _ = gate.check({}, self.allowlist, known_plugins=['demo'])
        self.assertEqual(len(errors), 1)
        self.assertIn('已过期', errors[0])

    def test_orphan_waiver_fails(self):
        self._waive('ghost')
        errors, _ = gate.check({}, self.allowlist, known_plugins=['demo'])
        self.assertEqual(len(errors), 1)
        self.assertIn('没有对应的插件', errors[0])

    def test_clean_plugin_without_waiver_passes(self):
        errors, warnings = gate.check({}, self.allowlist, known_plugins=['demo'])
        self.assertEqual((errors, warnings), ([], []))


class RepositoryWaiverTest(unittest.TestCase):
    def test_every_waiver_names_an_existing_plugin(self):
        plugins = {path.name for path in gate.DEFAULT_PLUGINS_DIR.iterdir()
                   if path.is_dir()}
        for waiver in sorted(gate.DEFAULT_ALLOWLIST_DIR.glob('*.txt')):
            self.assertIn(waiver.stem, plugins,
                          f'{waiver.name} 没有对应的插件目录')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
