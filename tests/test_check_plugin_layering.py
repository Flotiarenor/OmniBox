"""`check_plugin_layering` 的三条规则与豁免语义。

为什么要用例：这条门禁是"逐插件收紧"的棘轮 —— 规则写松了不会有症状，写严了会让
迁移中途的插件无故变红。两类错误都只在若干周后才显形，所以把判据钉成用例：

1. R1 能识别「定义 Mixin」与「入口继承 Mixin」两种形状；
2. R2 的边界（≤400 / 入口 ≤250）是"等于上限通过、多一行报错"；
3. R3 只拦**非入口**模块加载兄弟模块，入口自己 `load_sibling` 是正常写法；
4. 豁免：文件存在即免拦并留 warning，插件已经合规却还留着豁免是错误（否则棘轮永久失效），
   没有对应插件的豁免文件同样是错误；
5. `backend/libs`（vendored 第三方库）不参与检查。

运行：python -m unittest tests.test_check_plugin_layering -v
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import check_plugin_layering as gate

_MANIFEST = {
    'name': 'demo',
    'backend': {'entry': 'backend/main.py', 'class': 'DemoPlugin'},
}


def _make_plugin(
    root: Path,
    name: str = 'demo',
    files: dict[str, str] | None = None,
    *,
    entry: str = 'backend/main.py',
    waiver: bool = False,
) -> Path:
    """在临时仓库里造一个插件；`files` 的键是插件内相对路径。"""
    plugin = root / 'plugins' / name
    (plugin / 'backend').mkdir(parents=True, exist_ok=True)
    manifest = dict(_MANIFEST, name=name)
    manifest['backend'] = {'entry': entry, 'class': 'DemoPlugin'}
    (plugin / 'manifest.json').write_text(
        json.dumps(manifest), encoding='utf-8')
    for rel, text in (files or {}).items():
        target = plugin / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    if waiver:
        allowlist = root / 'allowlist'
        allowlist.mkdir(parents=True, exist_ok=True)
        (allowlist / f'{name}.txt').write_text('# 豁免\n', encoding='utf-8')
    return plugin


def _check(root: Path) -> tuple[list[str], list[str]]:
    return gate.check_plugins(root / 'plugins', root / 'allowlist')


def _rules(errors: list[str]) -> set[str]:
    return {rule for error in errors for rule in ('R1', 'R2', 'R3') if f' {rule} ' in error}


_ENTRY_OK = "from shell.backend.plugin_base import PluginBase\n"


class _TempRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class MixinRuleTest(_TempRepoCase):
    def test_mixin_class_is_error(self):
        _make_plugin(self.root, files={
            'backend/main.py': _ENTRY_OK,
            'backend/parts.py': 'class PartsMixin:\n    def work(self):\n        return 1\n',
        })
        errors, _ = _check(self.root)
        self.assertEqual(_rules(errors), {'R1'})
        self.assertTrue(any('PartsMixin' in error for error in errors), errors)

    def test_entry_inheriting_mixin_is_error(self):
        _make_plugin(self.root, files={
            'backend/main.py': 'class Plugin(SomeMixin, PluginBase):\n    pass\n',
        })
        errors, _ = _check(self.root)
        self.assertEqual(_rules(errors), {'R1'})
        self.assertTrue(any('继承了' in error for error in errors), errors)

    def test_plain_inheritance_is_fine(self):
        _make_plugin(self.root, files={'backend/main.py': _ENTRY_OK})
        errors, _ = _check(self.root)
        self.assertEqual(errors, [])


class LineLimitTest(_TempRepoCase):
    def test_backend_file_over_limit_is_error(self):
        _make_plugin(self.root, files={
            'backend/main.py': _ENTRY_OK,
            'backend/big.py': '\n'.join(['# 填充'] * (gate.MAX_FILE_LINES + 1)),
        })
        errors, _ = _check(self.root)
        self.assertEqual(_rules(errors), {'R2'})
        self.assertTrue(any('400' in error for error in errors), errors)

    def test_entry_over_its_own_limit_is_error(self):
        _make_plugin(self.root, files={
            'backend/main.py': '\n'.join(['# 填充'] * (gate.MAX_ENTRY_LINES + 1)),
        })
        errors, _ = _check(self.root)
        self.assertEqual(_rules(errors), {'R2'})
        self.assertTrue(any('入口' in error for error in errors), errors)

    def test_exactly_at_limit_passes(self):
        _make_plugin(self.root, files={
            'backend/main.py': '\n'.join(['# 填充'] * gate.MAX_ENTRY_LINES),
            'backend/big.py': '\n'.join(['# 填充'] * gate.MAX_FILE_LINES),
        })
        errors, _ = _check(self.root)
        self.assertEqual(errors, [])

    def test_a_long_entry_under_the_looser_limit_is_still_an_error(self):
        """入口用严格上限：300 行对普通文件合法、对入口不合法。"""
        _make_plugin(self.root, files={
            'backend/main.py': '\n'.join(['# 填充'] * (gate.MAX_ENTRY_LINES + 50)),
            'backend/other.py': '\n'.join(['# 填充'] * (gate.MAX_ENTRY_LINES + 50)),
        })
        errors, _ = _check(self.root)
        self.assertEqual(_rules(errors), {'R2'})
        self.assertEqual(len(errors), 1, errors)


class SiblingUsageTest(_TempRepoCase):
    _LOAD = (
        "from shell.backend.plugin_utils import load_sibling\n"
        "_common = load_sibling(__file__, 'common', 'demo')\n"
    )

    def test_entry_may_load_siblings(self):
        _make_plugin(self.root, files={'backend/main.py': self._LOAD})
        errors, _ = _check(self.root)
        self.assertEqual(errors, [])

    def test_non_entry_calling_load_sibling_is_error(self):
        _make_plugin(self.root, files={
            'backend/main.py': _ENTRY_OK,
            'backend/part.py': self._LOAD,
        })
        errors, _ = _check(self.root)
        self.assertEqual(_rules(errors), {'R3'})
        self.assertTrue(any('load_sibling' in error for error in errors), errors)

    def test_non_entry_importing_sibling_is_error(self):
        _make_plugin(self.root, files={
            'backend/main.py': _ENTRY_OK,
            'backend/common.py': 'VALUE = 1\n',
            'backend/part.py': 'from common import VALUE\n',
        })
        errors, _ = _check(self.root)
        self.assertEqual(_rules(errors), {'R3'})
        self.assertTrue(any('ModuleNotFoundError' in error for error in errors), errors)

    def test_importing_shell_backend_is_allowed(self):
        _make_plugin(self.root, files={
            'backend/main.py': _ENTRY_OK,
            'backend/part.py': 'from shell.backend import media_catalog\n',
        })
        errors, _ = _check(self.root)
        self.assertEqual(errors, [])

    def test_vendored_libs_are_skipped(self):
        _make_plugin(self.root, files={
            'backend/main.py': _ENTRY_OK,
            'backend/libs/vendor/thing.py': 'class VendorMixin:\n    pass\n',
        })
        errors, _ = _check(self.root)
        self.assertEqual(errors, [])


class WaiverTest(_TempRepoCase):
    def test_waiver_suppresses_errors_but_warns(self):
        _make_plugin(self.root, waiver=True, files={
            'backend/main.py': _ENTRY_OK,
            'backend/parts.py': 'class PartsMixin:\n    pass\n',
        })
        errors, warnings = _check(self.root)
        self.assertEqual(errors, [])
        self.assertTrue(any('豁免生效中' in warning for warning in warnings), warnings)

    def test_stale_waiver_is_error(self):
        """插件已合规却留着豁免文件：棘轮会永久失效，必须报错提醒删掉。"""
        _make_plugin(self.root, waiver=True, files={'backend/main.py': _ENTRY_OK})
        errors, _ = _check(self.root)
        self.assertTrue(any('已过期' in error for error in errors), errors)

    def test_orphan_waiver_is_error(self):
        allowlist = self.root / 'allowlist'
        allowlist.mkdir(parents=True)
        (allowlist / 'ghost.txt').write_text('# 豁免\n', encoding='utf-8')
        _make_plugin(self.root, files={'backend/main.py': _ENTRY_OK})
        errors, _ = _check(self.root)
        self.assertTrue(any('没有对应的插件' in error for error in errors), errors)

    def test_no_manifest_means_no_plugin(self):
        """没有 manifest 的目录不属于插件矩阵，豁免文件因此会被判为孤儿。"""
        (self.root / 'plugins' / 'empty').mkdir(parents=True)
        errors, _ = _check(self.root)
        self.assertEqual(errors, [])


class MainEntryTest(unittest.TestCase):
    def test_repository_tree_passes(self):
        """仓库现有树必须通过（豁免文件在迁移期间负责这一点）。"""
        errors, _warnings = gate.check_plugins()
        self.assertEqual(errors, [], f'仓库当前树不通过分层门禁：{errors[:3]}')

    def test_exit_code_matches_error_presence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, files={
                'backend/main.py': _ENTRY_OK,
                'backend/parts.py': 'class PartsMixin:\n    pass\n',
            })
            with redirect_stdout(io.StringIO()):
                code = gate.main([
                    '--plugins-dir', str(root / 'plugins'),
                    '--allowlist-dir', str(root / 'allowlist'),
                ])
            self.assertEqual(code, 1)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
