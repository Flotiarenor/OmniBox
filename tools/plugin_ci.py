#!/usr/bin/env python3
"""按插件拆分 CI 测试：``--core`` 跑共享测试，``--plugin`` 跑单个插件测试。

插件清单从 ``plugins/*/manifest.json`` 读取，不在这里维护第二份列表。测试归属由
文件名前缀决定：``test_document_reader_*`` 属于 document-reader，``test_reader_*``
是 document-reader 的历史别名；其余都算共享测试。新增插件时不需要改这份脚本，
只要测试文件按 ``test_<插件名下划线>_*`` 命名。
"""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = PROJECT_ROOT / 'tests'
PLUGINS_DIR = PROJECT_ROOT / 'plugins'
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 只有测试文件名与插件目录名不一致时，才需要在这里登记别名。
PLUGIN_TEST_ALIASES: dict[str, tuple[str, ...]] = {
    'document-reader': ('document_reader', 'reader'),
}


def plugin_names(plugins_dir: Path = PLUGINS_DIR) -> list[str]:
    names = []
    for path in sorted(plugins_dir.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or not (path / 'manifest.json').is_file():
            continue
        names.append(path.name)
    return names


def _prefixes(plugin: str, aliases: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    stem = plugin.replace('-', '_')
    extra = aliases.get(plugin, ())
    return tuple(dict.fromkeys((stem, *extra)))


def test_owner(stem: str, plugins: Iterable[str] | None = None) -> str | None:
    """返回测试文件所属插件；``stem`` 是去掉 ``test_`` 后的文件名。"""
    for plugin in plugins_names(plugins):
        for prefix in _prefixes(plugin, PLUGIN_TEST_ALIASES):
            if stem == prefix or stem.startswith(prefix + '_'):
                return plugin
    return None


def plugins_names(plugins: Iterable[str] | None) -> list[str]:
    if plugins is None:
        return plugin_names()
    return list(plugins)


def selected_stems(mode: str, plugin: str | None, tests_dir: Path = TESTS_DIR) -> list[str]:
    names = plugin_names()
    if mode == 'core':
        return [
            path.stem for path in sorted(tests_dir.glob('test_*.py'))
            if test_owner(path.stem.removeprefix('test_'), names) is None
        ]
    if mode != 'plugin' or plugin not in names:
        raise ValueError(f'未知插件: {plugin!r}')
    prefixes = _prefixes(plugin, PLUGIN_TEST_ALIASES)
    return [
        path.stem for path in sorted(tests_dir.glob('test_*.py'))
        if any(path.stem.removeprefix('test_') == prefix
               or path.stem.removeprefix('test_').startswith(prefix + '_')
               for prefix in prefixes)
    ]


def run_tests(stems: Iterable[str], verbosity: int = 2) -> int:
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for stem in sorted(set(stems)):
        suite.addTests(loader.loadTestsFromName(f'tests.{stem}'))
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true', help='输出插件名的 JSON 数组')
    parser.add_argument('--core', action='store_true', help='运行共享测试')
    parser.add_argument('--plugin', help='运行单个插件的测试')
    parser.add_argument('--list-tests', action='store_true', help='只列出选中的测试文件')
    parser.add_argument('--dry-run', action='store_true', help='不执行测试，只打印选择结果')
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(plugin_names(), ensure_ascii=False))
        return 0

    modes = [bool(args.core), bool(args.plugin)]
    if sum(modes) != 1:
        parser.error('必须且只能指定 --core 或 --plugin')
    mode = 'core' if args.core else 'plugin'
    try:
        stems = selected_stems(mode, args.plugin)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.list_tests or args.dry_run:
        print(json.dumps(stems, ensure_ascii=False, indent=1))
        return 0
    if not stems:
        print(f'{mode} 没有选中的测试文件', file=sys.stderr)
        return 1
    return run_tests(stems)


if __name__ == '__main__':
    sys.exit(main())
