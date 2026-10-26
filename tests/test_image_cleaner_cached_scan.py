"""`get_cached_scan` 的失效缓存过滤（提交 2d65cab）回归用例。

背景：扫描结果缓存保存的是扫描当时的相对路径。用户移动 / 删除图片后，缓存仍指向
旧路径，原生面板按这些路径请求 `/thumbs` 只会得到 404，整屏缩略图都会裂。修法是
`get_cached_scan` 返回前按父目录一次 `scandir` 过滤掉已不存在的文件、组内不足两张
的组一并丢弃，并在有剔除时标记 `stale`；缓存文件与用户文件都**不**改动。

本用例用真实插件实例锁住：
  1. 缺失文件被剔除、不足两张的组被丢弃、`stale` 置位，`scanned` 保留；
  2. 不过滤时 `stale` 保持原值（不能每读一次就"脏"一次）；
  3. 过滤是只读的：缓存文件字节不变，用户文件一个不少；
  4. `..` 穿越与绝对路径不会被当成"存在"，即使目标文件真的存在；
  5. 额外根（`__<命名空间>/…`）里的文件按宿主给自己的前缀解析，不会被误删。

运行：
    python -m unittest tests.test_image_cleaner_cached_scan -v
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.settings_store import SettingsStore


def _load(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(PROJECT_ROOT / rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'not-a-real-image')


class CachedScanFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.viewer_module = _load('plugins/image-viewer/backend/main.py', 'iv_main_cached_scan_test')
        cls.cleaner_module = _load('plugins/image-cleaner/backend/main.py', 'ic_main_cached_scan_test')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.root = self.tmp / 'data'
        self.root.mkdir(parents=True, exist_ok=True)
        self._settings_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._settings_tmp.cleanup)
        self.settings_dir = Path(self._settings_tmp.name)

    def _viewer(self, extra_roots: str = ''):
        module = self.viewer_module
        original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
        module.ImageViewerPlugin._resolved_config = {
            'root_dir': str(self.root), 'extra_roots': extra_roots,
        }
        try:
            viewer = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = original
        viewer._settings_store = SettingsStore(str(self.settings_dir))
        return viewer

    def _cleaner(self, extra_roots: str = ''):
        cleaner = self.cleaner_module.ImageCleanerPlugin(
            {'name': 'image-cleaner'},
            {'directories': {'data_root': str(self.root)}},
        )
        cleaner._host = self._viewer(extra_roots)
        cleaner._settings_store = SettingsStore(str(self.settings_dir))
        return cleaner

    def _cache_path(self) -> Path:
        return self.root / '.cache' / 'image-cleaner' / 'scan_cache.json'

    def _write_cache(self, groups, scanned=None, stale=False):
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'dupe': {
                'groups': groups,
                'scanned': 9 if scanned is None else scanned,
                'saved_at': 123.0,
                'stale': stale,
            },
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    def _files_in(self, directory: Path):
        return sorted(p.relative_to(directory).as_posix() for p in directory.rglob('*'))

    # ---------- 1. 剔除失效项 ----------

    def test_filters_missing_files_drops_small_groups_and_marks_stale(self):
        _touch(self.root / 'a.jpg')
        _touch(self.root / 'sub' / 'b.jpg')
        _touch(self.root / 'c.jpg')
        # 组内少一张 → 保留；组内缺到不足两张 → 整组丢弃；非 dict / files 非列表 → 丢弃
        groups = [
            {'hash': 'h1', 'size': 10, 'files': ['a.jpg', 'sub/b.jpg', 'gone.jpg']},
            {'hash': 'h2', 'size': 10, 'files': ['c.jpg', 'ghost1.jpg', 'ghost2.jpg']},
            {'hash': 'h3', 'size': 10, 'files': []},
            {'hash': 'h4', 'size': 10, 'files': ['ghost3.jpg']},
            {'hash': 'h5', 'size': 10, 'files': 'not-a-list'},
            'not-a-group',
        ]
        self._write_cache(groups)
        cleaner = self._cleaner()

        result = cleaner.get_cached_scan('dupe')

        self.assertTrue(result['cached'])
        self.assertEqual(result['scanned'], 9, 'scanned 是扫描口径，不因剔除而改')
        self.assertEqual([g['files'] for g in result['groups']],
                         [['a.jpg', 'sub/b.jpg']], '缺失文件与不足两张的组都要剔除')
        self.assertTrue(result['stale'], '剔除过就要提示"磁盘有变化"')
        self.assertEqual(result['groups'][0]['hash'], 'h1', '保留组里的其它字段')

    def test_healthy_cache_is_not_marked_stale(self):
        _touch(self.root / 'a.jpg')
        _touch(self.root / 'b.jpg')
        groups = [{'files': ['a.jpg', 'b.jpg']}]
        self._write_cache(groups)
        cleaner = self._cleaner()

        result = cleaner.get_cached_scan('dupe')

        self.assertFalse(result['stale'], '文件都在时不能凭空标 stale')
        self.assertEqual(result['groups'], groups)

    # ---------- 2. 只读：不写缓存、不动用户文件 ----------

    def test_filter_does_not_modify_cache_or_user_files(self):
        _touch(self.root / 'a.jpg')
        _touch(self.root / 'b.jpg')
        _touch(self.root / 'keep-me.jpg')
        groups = [{'files': ['a.jpg', 'b.jpg', 'gone.jpg']}]
        cleaner = self._cleaner()
        cache_path = self._write_cache(groups)
        before_cache = cache_path.read_bytes()
        before_files = self._files_in(self.root)

        cleaner.get_cached_scan('dupe')

        self.assertEqual(cache_path.read_bytes(), before_cache,
                         '过滤只发生在返回值里，缓存文件必须原样保留')
        self.assertEqual(self._files_in(self.root), before_files,
                         '读缓存不能删除 / 新建任何用户文件')

    # ---------- 3. 路径穿越与绝对路径 ----------

    def test_path_traversal_is_rejected_even_when_target_exists(self):
        _touch(self.root / 'a.jpg')
        outside = self.tmp / 'outside' / 'secret.jpg'
        _touch(outside)
        # `../outside/secret.jpg` 真实存在，但不在扫描根内：不能算"缓存里还有这张图"
        self._write_cache([{'files': ['../outside/secret.jpg', 'a.jpg']}])
        cleaner = self._cleaner()

        result = cleaner.get_cached_scan('dupe')

        self.assertEqual(result['groups'], [], '穿越路径不算命中，组内只剩 a.jpg 一张 → 丢组')
        self.assertTrue(result['stale'])
        self.assertTrue(outside.exists(), '过滤是只读判断，不能碰根外文件')

    def test_absolute_paths_are_rejected(self):
        _touch(self.root / 'a.jpg')
        _touch(self.root / 'b.jpg')
        outside = self.tmp / 'outside' / 'secret.jpg'
        _touch(outside)
        self._write_cache([{'files': [str(outside), 'a.jpg', 'b.jpg']}])
        cleaner = self._cleaner()

        result = cleaner.get_cached_scan('dupe')

        self.assertEqual([g['files'] for g in result['groups']], [['a.jpg', 'b.jpg']],
                         '绝对路径必须剔除（否则可以拿根外文件当"缓存命中"）')

    # ---------- 4. 额外根的前缀 ----------

    def test_extra_root_prefix_resolves_to_its_own_root(self):
        _touch(self.root / 'a.jpg')
        extra = self.tmp / '外部' / '额外图库'
        _touch(extra / 'x.jpg')
        _touch(extra / 'y.jpg')
        prefix = self._cleaner(extra_roots=str(extra))._scan_roots()[1][1]
        self.assertTrue(prefix, '额外根必须有前缀')
        self._write_cache([{'files': [f'{prefix}/x.jpg', f'{prefix}/y.jpg', f'{prefix}/gone.jpg']}])
        cleaner = self._cleaner(extra_roots=str(extra))

        result = cleaner.get_cached_scan('dupe')

        self.assertEqual([g['files'] for g in result['groups']],
                         [[f'{prefix}/x.jpg', f'{prefix}/y.jpg']],
                         '带前缀的路径要按额外根解释，不能按主根判成不存在')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
