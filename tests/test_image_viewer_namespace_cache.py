"""命名空间映射必须是**缓存的派生值**，不能每次解析路径都重算。

背景（真实缺陷）：命名空间 token 的规则是"根目录名，重名或与第一根的一级子目录同名时
加序号"，而拿第一根的一级子目录名要 `os.scandir(第一根)`。旧实现每次 `virtual_path`
都重算一遍，于是**额外根下的每个目录**都会触发一次第一根目录扫描 —— 实测第一根 200 个
目录 + 额外根 200 个目录时，一次全树枚举触发 603 次 `os.scandir`，其中第一根被扫 202
次。额外根下上万目录时这个数字同样上万。

这条用例钉住两件事：

1. 第一根目录的扫描次数与额外根下的目录数**无关**（O(1)，冷缓存下也就一两次）；
2. 扫描过滤不能把结果弄丢：目录快照里仍然有全部目录，名字冲突时 token 仍然加序号。

运行：
    venv/Scripts/python.exe -m unittest tests.test_image_viewer_namespace_cache -v
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_plugin_module():
    main_path = PROJECT_ROOT / 'plugins' / 'image-viewer' / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('image_viewer_namespace_cache_test',
                                                  str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_tree(base: Path, first_dirs: int, extra_dirs: int):
    """建两棵树：第一根（含若干一级子目录）+ 额外根（含若干一级子目录）。"""
    first = base / '主图库'
    first.mkdir(parents=True)
    for index in range(first_dirs):
        (first / f'a{index:03d}').mkdir()
    extra = base / '外部' / '额外图库'
    extra.mkdir(parents=True)
    for index in range(extra_dirs):
        (extra / f'b{index:03d}').mkdir()
    return first, extra


class NamespaceCacheTests(unittest.TestCase):
    """第一根目录的扫描次数不随额外根目录数线性增长。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.module = _load_plugin_module()

    def _plugin(self, first: Path, extra: Path):
        module = self.module
        original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
        module.ImageViewerPlugin._resolved_config = {
            'root_dir': str(first),
            'extra_roots': str(extra),
        }
        try:
            plugin = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(first)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = original
        return plugin

    def _count_walk_scans(self, extra_dirs: int):
        """量一次全树枚举里对第一根目录的扫描次数与目录总数。"""
        first_root, extra = _make_tree(self.base / f'case{extra_dirs}', 30, extra_dirs)
        plugin = self._plugin(first_root, extra)
        # 冷缓存：让命名空间映射与目录快照都从零开始算，才量得到"重算一次"的代价
        plugin.paths.invalidate()
        plugin.dirs_snapshot.reset()

        scanned: list[str] = []
        real_scandir = os.scandir

        def counting_scandir(path='.'):
            scanned.append(str(path))
            return real_scandir(path)

        with mock.patch('os.scandir', side_effect=counting_scandir):
            dirs = plugin.dirs_snapshot.walk()

        first_scans = sum(1 for path in scanned if path == str(first_root))
        # 目录枚举本身必须仍然正确：合成根 + 第一根 30 个子目录 + 额外根的命名空间节点
        # + 额外根下 extra_dirs 个子目录
        self.assertEqual(len(dirs), 2 + 30 + extra_dirs)
        self.assertEqual(plugin.paths.namespace_for(extra), '额外图库')
        return first_scans, len(scanned), plugin

    def test_first_root_is_scanned_a_constant_number_of_times(self):
        small_scans, small_total, _ = self._count_walk_scans(40)
        large_scans, large_total, _ = self._count_walk_scans(160)

        # 额外根目录数翻了 4 倍，第一根的扫描次数不跟着涨（旧实现是 40 → 160）
        self.assertEqual(small_scans, large_scans,
                         f'第一根扫描次数随目录数增长了：{small_scans} → {large_scans}')
        self.assertLessEqual(large_scans, 3,
                             f'冷缓存下一次全树枚举最多扫第一根一两次，实际 {large_scans}')
        # 对照组：总扫描次数确实随目录数增长 —— 证明计数器是活的，不是恒为 0
        self.assertGreater(large_total, small_total + 100)

    def test_namespace_token_still_avoids_name_collision(self):
        """缓存不能改变取名规则：与第一根一级子目录同名时仍然加序号。"""
        first, extra = _make_tree(self.base / 'collision', 5, 3)
        # 在第一根里造一个与额外根同名的目录：token 必须让位成「额外图库 (2)」
        (first / '额外图库').mkdir()
        plugin = self._plugin(first, extra)
        self.assertEqual(plugin.paths.namespace_for(extra), '额外图库 (2)')
        self.assertEqual(plugin.paths.virtual_path(extra, 'b000'), '__额外图库 (2)/b000')
        # 再次取用命中同一个缓存，结果稳定
        self.assertEqual(plugin.paths.namespace_for(extra), '额外图库 (2)')
        self.assertEqual(list(plugin.paths.roots_index()), ['额外图库 (2)'])


if __name__ == '__main__':
    unittest.main()
