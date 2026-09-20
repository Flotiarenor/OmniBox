"""`tools/check_tests.py` 的判定逻辑（构造临时目录，不依赖真实测试文件）。

为什么单独有用例
----------------
这条门禁的价值在两侧：既要在"文件里根本没有用例"时报错，也不能把正常的
TestCase、条件跳过（`skipIf` / `skipUnless`）或同模块基类的继承用法误判。
用例直接构造 `tests/test_*.py` 的源码文本喂给纯函数 `check_tests()`，不导入它们。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

import sys

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.check_tests import check_tests


def _run(files: dict) -> list:
    with tempfile.TemporaryDirectory() as td:
        tests_dir = Path(td) / 'tests'
        tests_dir.mkdir()
        for name, content in files.items():
            (tests_dir / name).write_text(content, encoding='utf-8')
        return check_tests(tests_dir)


NORMAL = """\
import unittest


class SampleTests(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""


class CheckTestsGateTests(unittest.TestCase):
    def test_real_repo_tests_dir_passes(self):
        """仓库自己的 tests/ 必须过门禁（防止门禁与现状漂移）。"""
        self.assertEqual(check_tests(PROJECT_ROOT / 'tests'), [])

    def test_normal_test_case_passes(self):
        self.assertEqual(_run({'test_ok.py': NORMAL}), [])

    def test_module_without_any_test_is_reported(self):
        errors = _run({'test_manual.py': 'def main():\n    pass\n'})
        self.assertTrue(
            any('test_manual.py' in e and '没有可被 unittest 收集' in e for e in errors),
            errors,
        )

    def test_only_top_level_test_functions_are_reported(self):
        """unittest discover 不收集模块级 `def test_*`，所以它同样是用例数 0。"""
        errors = _run({'test_pytest_style.py': 'def test_one():\n    assert True\n'})
        self.assertTrue(any('test_pytest_style.py' in e for e in errors), errors)

    def test_test_case_without_test_methods_is_reported(self):
        errors = _run({'test_empty.py': (
            'import unittest\n\n\nclass Empty(unittest.TestCase):\n    pass\n'
        )})
        self.assertTrue(any('test_empty.py' in e for e in errors), errors)

    def test_conditional_skip_is_not_flagged(self):
        code = (
            'import unittest\n\n\n'
            'class Conditional(unittest.TestCase):\n'
            "    @unittest.skipIf(False, '本机不满足条件')\n"
            '    def test_maybe(self):\n'
            '        self.assertTrue(True)\n'
        )
        self.assertEqual(_run({'test_conditional.py': code}), [])

    def test_unconditional_skip_on_method_is_reported(self):
        code = (
            'import unittest\n\n\n'
            'class Disabled(unittest.TestCase):\n'
            "    @unittest.skip('暂时关闭')\n"
            '    def test_never_runs(self):\n'
            '        self.assertTrue(True)\n'
        )
        errors = _run({'test_disabled.py': code})
        self.assertTrue(any('test_never_runs' in e for e in errors), errors)

    def test_unconditional_skip_on_class_is_reported(self):
        code = (
            'import unittest\n\n\n'
            "@unittest.skip('整类关闭')\n"
            'class DisabledClass(unittest.TestCase):\n'
            '    def test_never_runs(self):\n'
            '        self.assertTrue(True)\n'
        )
        errors = _run({'test_disabled_class.py': code})
        self.assertTrue(any('DisabledClass' in e for e in errors), errors)

    def test_test_case_overriding_init_is_reported(self):
        code = (
            'import unittest\n\n\n'
            'class BadInit(unittest.TestCase):\n'
            '    def __init__(self):\n'
            '        super().__init__()\n\n'
            '    def test_x(self):\n'
            '        self.assertTrue(True)\n'
        )
        errors = _run({'test_bad_init.py': code})
        self.assertTrue(any('BadInit' in e for e in errors), errors)

    def test_inherited_test_methods_are_counted(self):
        """同模块基类里的 test* 方法由子类继承时，子类应有可收集的用例。"""
        code = (
            'import unittest\n\n\n'
            'class BaseTests(unittest.TestCase):\n'
            '    def test_inherited(self):\n'
            '        self.assertTrue(True)\n\n\n'
            'class ChildTests(BaseTests):\n'
            '    pass\n'
        )
        self.assertEqual(_run({'test_inherited.py': code}), [])

    def test_no_test_modules_at_all_is_reported(self):
        errors = _run({'helper.py': 'x = 1\n'})
        self.assertTrue(any('没有 tests/test_*.py' in e for e in errors), errors)


if __name__ == '__main__':
    unittest.main()
