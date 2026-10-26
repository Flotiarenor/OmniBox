"""CI 测试矩阵的覆盖不变量（`tools/plugin_ci.py` 的归属规则）。

`ci.yml` 的 `test` job 只跑 `--core`，`plugin` 矩阵每个插件跑 `--plugin <name>`；
两者都不再是 `unittest discover`。因此"某个测试文件两个桶都不要"等于**它从此
不进 CI**，而且本地全量 `discover` 仍然通过，看不出缺口。这里把归属规则钉成
可复核的不变量：

1. `tests/test_*.py` 全集 = core ∪ 各插件桶，且每个文件恰好落在一个桶里；
2. 插件桶里的每个文件，`test_owner()` 也判给同一个插件（选择器与归属判据一致，
   而不是两套前缀各自匹配）；
3. core 桶里的每个文件都不属于任何插件；
4. 插件前缀两两不互相包含（例如将来出现 `image` 与 `image-viewer` 时，
   `test_image_viewer_*` 不能同时被两边选中）；
5. 每个插件前缀（含 `document-reader` 的 `reader` 历史别名）至少命中一个现存
   测试文件 —— 没有命中的别名只会成为下一个误吞无关文件的入口；
6. 每个插件至少有一个测试文件：否则矩阵 job 会以"没有选中的测试文件"退出 1；
7. 本文件自己必须被 core 选中（新增的评审用例不能自己成为落单文件）。

这些用例只读 `tests/` 与 `plugins/*/manifest.json`，不导入任何插件、不跑子进程。

运行：
    python -m unittest tests.test_gate_coverage_review -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import plugin_ci


def _test_stems() -> list[str]:
    return sorted(path.stem for path in plugin_ci.TESTS_DIR.glob('test_*.py'))


def _buckets() -> dict[str, set[str]]:
    """`{'core': {...}, '<插件>': {...}}`，值为带 `test_` 前缀的文件 stem。"""
    buckets = {'core': set(plugin_ci.selected_stems('core', None))}
    for plugin in plugin_ci.plugin_names():
        buckets[plugin] = set(plugin_ci.selected_stems('plugin', plugin))
    return buckets


class TestMatrixCoverage(unittest.TestCase):
    def test_every_test_file_is_selected_exactly_once(self):
        files = set(_test_stems())
        buckets = _buckets()
        union = set().union(*buckets.values())
        self.assertEqual(
            files - union, set(),
            '这些测试文件不在任何 CI 桶里，本地 discover 会跑、CI 永远不跑',
        )
        self.assertEqual(union - files, set(), '选择器选中了不存在的测试文件')
        total = sum(len(bucket) for bucket in buckets.values())
        self.assertEqual(
            total, len(union),
            '同一个测试文件被多个桶选中：\n' + '\n'.join(
                f'{name}: {sorted(bucket)}' for name, bucket in buckets.items()
                if bucket),
        )

    def test_plugin_bucket_matches_test_owner(self):
        names = plugin_ci.plugin_names()
        for plugin in names:
            for stem in plugin_ci.selected_stems('plugin', plugin):
                with self.subTest(plugin=plugin, stem=stem):
                    self.assertEqual(
                        plugin_ci.test_owner(stem.removeprefix('test_'), names),
                        plugin,
                        '插件桶与 test_owner 判据不一致：改归属规则时只改了一处',
                    )

    def test_core_bucket_has_no_plugin_owned_file(self):
        names = plugin_ci.plugin_names()
        for stem in plugin_ci.selected_stems('core', None):
            with self.subTest(stem=stem):
                self.assertIsNone(
                    plugin_ci.test_owner(stem.removeprefix('test_'), names),
                    f'{stem} 属于某个插件，core job 不该跑它',
                )

    def test_plugin_prefixes_do_not_overlap(self):
        prefixes = {
            plugin: plugin_ci._prefixes(plugin, plugin_ci.PLUGIN_TEST_ALIASES)
            for plugin in plugin_ci.plugin_names()
        }
        for plugin, values in prefixes.items():
            for other, other_values in prefixes.items():
                if plugin >= other:
                    continue
                for value in values:
                    for other_value in other_values:
                        with self.subTest(plugin=plugin, other=other):
                            self.assertFalse(
                                value == other_value
                                or value.startswith(other_value + '_')
                                or other_value.startswith(value + '_'),
                                f'前缀 {value!r} 与 {other_value!r} 互相包含：'
                                f'测试文件会被两个插件桶同时选中',
                            )

    def test_every_registered_prefix_matches_a_test(self):
        all_stems = _test_stems()
        for plugin in plugin_ci.plugin_names():
            for prefix in plugin_ci._prefixes(plugin, plugin_ci.PLUGIN_TEST_ALIASES):
                matched = [
                    stem for stem in all_stems
                    if (stem.removeprefix('test_') == prefix
                        or stem.removeprefix('test_').startswith(prefix + '_'))
                ]
                with self.subTest(plugin=plugin, prefix=prefix):
                    self.assertTrue(
                        matched,
                        f'插件 {plugin} 的前缀 {prefix!r} 没有任何测试文件命中：'
                        f'要么删掉过期别名，要么按前缀补测试',
                    )

    def test_every_plugin_has_at_least_one_test(self):
        """空矩阵 job 会以 exit 1 失败（`plugin_ci.main` 的"没有选中"分支）。"""
        for plugin in plugin_ci.plugin_names():
            with self.subTest(plugin=plugin):
                self.assertTrue(
                    plugin_ci.selected_stems('plugin', plugin),
                    f'{plugin} 在矩阵里没有测试文件，CI job 必然失败',
                )

    def test_matrix_names_come_from_plugin_manifests(self):
        on_disk = sorted(
            path.name for path in plugin_ci.PLUGINS_DIR.iterdir()
            if path.is_dir() and (path / 'manifest.json').is_file()
        )
        self.assertEqual(plugin_ci.plugin_names(), on_disk)
        # check_plugins.py 跳过 `_` 前缀目录（脚手架/草稿），矩阵若收录它们会得到
        # 一个永远"没有测试文件"的 job。
        self.assertFalse([name for name in on_disk if name.startswith('_')])

    def test_this_review_file_is_selected_by_core(self):
        self.assertIn(Path(__file__).stem, plugin_ci.selected_stems('core', None))


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
