"""tests/ 目录门禁：以 `test_` 开头的文件必须真的能跑出 unittest 用例。

为什么需要
----------
文件名匹配 `test_*.py` 时，`unittest discover` 会**导入**它，但导入不等于收集到用例：
文件里没有 TestCase 子类（或 `test*` 方法）时，discover 收集 0 条、退出码 0 ——
CI 全绿，这个文件实际什么都没测。仓库里曾有两个这样的手工脚本
（`tests/test_media_scan_task.py`、`tests/test_parallel_tmp.py`，现在改名为
`tests/debug_*.py`），它们只在手工执行时才有意义，不属于测试套件。

手工脚本的命名约定见 `docs/group-mesh-design.md`：放 `tests/debug_*.py`。

判定（逐个 `tests/test_*.py`）
-----------------------------
1. 至少有一个可被 unittest 收集的用例：TestCase 子类里存在 `test*` 方法
   （含同模块基类继承来的），且该方法没有被无条件的 `@unittest.skip` 跳过；
2. TestCase 子类不得覆写 `__init__`（覆写后 unittest 无法用方法名实例化）；
3. `test*` 方法（或整个类）不得使用无条件的 `@unittest.skip` —— 永远不执行的
   用例同样不可能发现问题。

`@unittest.skipUnless(...)` / `@unittest.skipIf(...)` 是条件跳过（例如具体平台、
具体可选依赖），不属于本门禁的拦截范围。

用法：
    python tools/check_tests.py            # 检查仓库的 tests/
    python tools/check_tests.py <目录>      # 检查指定目录（用例里构造反例用）

退出码 0 = 通过，1 = 存在不可运行的测试文件。
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TESTS_DIR = PROJECT_ROOT / 'tests'

# 无条件跳过装饰器：`@skip` / `@skip('理由')` / `@unittest.skip` / `@unittest.skip('理由')`。
# 带条件的 skipIf / skipUnless 不在此列。
_UNCONDITIONAL_SKIP_NAMES = ('skip', 'unittest.skip')


def _is_test_case_class(
    node: ast.ClassDef,
    classes_by_name: dict[str, ast.ClassDef],
    visited: set[str] | None = None,
) -> bool:
    """类是否继承 unittest.TestCase（沿同模块基类递归，处理 BaseTests 这类写法）。"""
    if visited is None:
        visited = set()
    if node.name in visited:
        return False
    visited.add(node.name)
    for base in node.bases:
        text = ast.unparse(base).strip()
        if text == 'TestCase' or text.endswith('.TestCase'):
            return True
        if isinstance(base, ast.Name):
            parent = classes_by_name.get(base.id)
            if parent is not None and _is_test_case_class(parent, classes_by_name, visited):
                return True
    return False


def _is_unconditional_skip(decorators: list[ast.expr]) -> bool:
    for decorator in decorators:
        text = ast.unparse(decorator).strip()
        for name in _UNCONDITIONAL_SKIP_NAMES:
            if text == name or text.startswith(f'{name}('):
                return True
    return False


def _defined_test_methods(node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        item for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name.startswith('test')
    ]


def _all_test_methods(
    node: ast.ClassDef,
    classes_by_name: dict[str, ast.ClassDef],
    visited: set[str] | None = None,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """把同模块基类里的 test* 方法也算进来（子类只覆写部分方法时的写法）。"""
    if visited is None:
        visited = set()
    if node.name in visited:
        return []
    visited.add(node.name)
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in classes_by_name:
            methods.extend(_all_test_methods(classes_by_name[base.id], classes_by_name, visited))
    methods.extend(_defined_test_methods(node))
    return methods


def _has_init(node: ast.ClassDef) -> bool:
    return any(
        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == '__init__'
        for item in node.body
    )


def check_tests(tests_dir: Path) -> list[str]:
    """返回问题清单；空列表表示通过。"""
    errors: list[str] = []
    module_paths = sorted(tests_dir.glob('test_*.py'))
    if not module_paths:
        errors.append(f'{tests_dir} 下没有 tests/test_*.py 文件')
        return errors

    for path in module_paths:
        rel = path.relative_to(tests_dir.parent).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f'{rel}: 无法解析: {exc}')
            continue

        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        classes_by_name = {node.name: node for node in classes}
        runnable = 0
        for node in classes:
            if not _is_test_case_class(node, classes_by_name):
                continue
            if _is_unconditional_skip(node.decorator_list):
                errors.append(
                    f'{rel}: {node.name} 使用无条件的 unittest.skip，该类永远不会执行'
                )
                continue
            if _has_init(node):
                errors.append(
                    f'{rel}: {node.name} 覆写了 __init__，unittest 无法用方法名实例化该类'
                )
                continue
            for method in _all_test_methods(node, classes_by_name):
                if _is_unconditional_skip(method.decorator_list):
                    errors.append(
                        f'{rel}: {node.name}.{method.name} 使用无条件的 unittest.skip，'
                        f'该用例永远不会执行'
                    )
                    continue
                runnable += 1

        if runnable == 0:
            errors.append(
                f'{rel}: 没有可被 unittest 收集的用例（无 TestCase 子类，或 test* 方法'
                f'全部被无条件跳过）—— 手工脚本请改名 tests/debug_*.py'
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='校验 tests/ 下的测试文件真的能跑出用例')
    parser.add_argument('tests_dir', nargs='?', type=Path, default=DEFAULT_TESTS_DIR)
    args = parser.parse_args(argv)

    errors = check_tests(args.tests_dir)
    if errors:
        print(f'check_tests: {len(errors)} 个错误', file=sys.stderr)
        for error in errors:
            print(f'  - {error}', file=sys.stderr)
        return 1
    print('check_tests: OK（每个 tests/test_*.py 都有可运行的 unittest 用例）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
