"""image-viewer 三个功能增强的回归用例：多根目录、空目录隐藏、子相册默认折叠。

1. **多根目录**（`extra_roots`）：第一根沿用相对路径（既有链接/缓存/缩略图库
   都不变），第二根起在相册树里以 `__<目录名>` 命名空间节点作为顶层；
   `depth` 按**根内**相对路径计算，所以额外根下的作者/作品与第一根处在同一
   层级，两层 Pixiv 布局不会被命名空间顶掉。
2. **完全无图目录隐藏**：递归（含下级）都没有可读图片的目录不下发可见性；
   「新建相册」建出来的空目录记进 `visible_empty_dirs` 保留可见，直到里面
   进了图片或目录被删除。
3. **子相册默认折叠**：`set_album_config` 的 expand/collapse 落进
   `expanded` / `collapsed`，折叠是默认状态（前端据此隐藏下级）。

运行：
    python -m unittest tests.test_image_viewer_multi_root -v
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image

from shell.backend.media_catalog import DRIVES_SENTINEL
from shell.backend.settings_store import SettingsStore


def _same(a, b) -> bool:
    """路径同一性比较：临时目录在 Windows 上可能是 8.3 短名，字符串会不等。"""
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _load_plugin_module():
    main_path = PROJECT_ROOT / 'plugins' / 'image-viewer' / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('image_viewer_multi_root_test', str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_image(path: Path, w: int = 12, h: int = 12):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (w, h), (90, 130, 60)).save(path)


class ImageViewerMultiRootTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        # 第一根：作者/作品/图片
        cls.root = base / '主图库'
        _make_image(cls.root / '作者A' / '作品1' / '1.jpg')
        _make_image(cls.root / '作者A' / '作品1' / '2.jpg')
        _make_image(cls.root / '作者A' / '作品2' / '1.jpg')
        # 递归也无图的两棵空目录树
        (cls.root / '空目录').mkdir(parents=True)
        (cls.root / '空容器' / '深一层').mkdir(parents=True)
        # 第二根：目录名与第一根的同名作者目录重复（命名空间必须避开冲突）
        cls.extra = base / '外部' / '额外图库'
        _make_image(cls.extra / '作者A' / '作品9' / '1.jpg')
        _make_image(cls.extra / '作者B' / '作品1' / '1.jpg')
        # 第三根：目录名与第二根的根目录名相同（命名空间需要加序号）
        cls.extra2 = base / '另一个' / '额外图库'
        _make_image(cls.extra2 / '作者C' / '作品1' / '1.jpg')
        cls.module = _load_plugin_module()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self._settings_tmp = tempfile.TemporaryDirectory()
        self.settings_dir = Path(self._settings_tmp.name)

    def tearDown(self):
        self._settings_tmp.cleanup()

    def _plugin(self, config: dict, with_store: bool = True):
        module = self.module
        original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
        module.ImageViewerPlugin._resolved_config = config
        try:
            plugin = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = original
        if with_store:
            plugin._settings_store = SettingsStore(str(self.settings_dir))
        return plugin

    def _multi(self, **extra):
        return self._plugin({
            'root_dir': str(self.root),
            'extra_roots': '\n'.join([str(self.extra), str(self.extra2)]),
            **extra,
        })

    # ---------- 多根目录：路径与命名空间 ----------

    def test_single_root_paths_unchanged(self):
        """只有第一根时，路径/根内相对路径与历史版本一致。"""
        plugin = self._plugin({'root_dir': str(self.root)})
        root, inner = plugin._split_virtual('')
        self.assertTrue(_same(root, self.root))
        self.assertEqual(inner, '')
        root, inner = plugin._split_virtual('作者A/作品1')
        self.assertTrue(_same(root, self.root))
        self.assertEqual(inner, '作者A/作品1')
        self.assertEqual(plugin._virtual_path(self.root, '作者A/作品1'), '作者A/作品1')
        self.assertTrue(_same(plugin.get_file_roots()[0], self.root))
        self.assertEqual(len(plugin.get_file_roots()), 1)

    def test_extra_roots_get_namespace_prefix(self):
        """第二根起以 `__<目录名>` 出现；第一根路径不受影响。"""
        plugin = self._multi()
        # 两个根目录同名（都叫「额外图库」）：按配置顺序得到 `额外图库` 与 `额外图库 (2)`
        ns1 = f'__{self.extra.name}'
        ns2 = f'__{self.extra2.name} (2)'
        self.assertEqual(plugin._virtual_path(self.root, '作者A'), '作者A')
        self.assertEqual(plugin._virtual_path(self.extra, '作者A'), f'{ns1}/作者A')
        self.assertEqual(plugin._virtual_path(self.extra2, '作者C'), f'{ns2}/作者C')
        root, inner = plugin._split_virtual(f'{ns1}/作者A')
        self.assertTrue(_same(root, self.extra))
        self.assertEqual(inner, '作者A')
        # 未知命名空间不落到第一根的物理目录里
        self.assertEqual(plugin._split_virtual('__不存在/作者A'), (None, ''))
        self.assertFalse(plugin._is_safe('__不存在/作者A'))
        self.assertEqual(len(plugin.get_file_roots()), 3)

    def test_namespace_avoids_first_root_dir_names(self):
        """命名空间是虚拟顶层节点，不能和第一根的真实目录同名。"""
        conflict = self.root / '额外图库'
        conflict.mkdir(exist_ok=True)
        try:
            plugin = self._plugin({
                'root_dir': str(self.root),
                'extra_roots': str(self.extra),
            })
            self.assertEqual(plugin._namespace(self.extra), '额外图库 (2)')
            # 两个同名根目录按配置顺序继续排号
            both = self._multi()
            self.assertEqual(both._namespace(self.extra), '额外图库 (2)')
            self.assertEqual(both._namespace(self.extra2), '额外图库 (3)')
        finally:
            # 必须把本次产生的冲突目录全部清掉：setUpClass 的根目录是共享的，
            # 残留会污染后续用例的命名空间分配
            for path in self.root.glob('额外图库*'):
                if path.is_dir():
                    path.rmdir()

    def test_depth_is_relative_to_own_root(self):
        """额外根下的作者层 depth 与第一根一致（否则作者网格/瀑布流会错层）。"""
        plugin = self._multi()
        albums = {a['path']: a for a in plugin.list_albums()['albums']}
        ns1 = f'__{self.extra.name}'
        self.assertEqual(albums['作者A']['depth'], 1)
        self.assertEqual(albums[f'{ns1}']['depth'], 0)
        self.assertTrue(albums[f'{ns1}']['root_scope'])
        self.assertEqual(albums[f'{ns1}/作者A']['depth'], 1)
        self.assertEqual(albums[f'{ns1}/作者A/作品9']['depth'], 2)

    def test_list_albums_covers_all_roots(self):
        """相册树包含全部根：命名空间节点带递归总数与封面。"""
        plugin = self._multi()
        albums = {a['path']: a for a in plugin.list_albums()['albums']}
        ns1 = f'__{self.extra.name}'
        ns2 = f'__{self.extra2.name} (2)'
        self.assertEqual(albums[ns1]['image_count'], 2)
        self.assertTrue(albums[ns1]['cover'].startswith(f'{ns1}/'))
        self.assertEqual(albums[ns2]['image_count'], 1)
        # 第一根 + 两个命名空间根 + 各自下级
        self.assertEqual(albums['作者A']['image_count'], 3)
        self.assertIn(f'{ns1}/作者B/作品1', albums)
        # 合成根的递归总数 = 全部根之和
        self.assertEqual(albums['']['image_count'], 6)

    def test_namespace_root_lists_child_albums(self):
        """打开命名空间节点：顶层把子目录作为瓦片列出（不会去扫不存在的物理目录）。"""
        plugin = self._multi()
        ns1 = f'__{self.extra.name}'
        data = plugin.list_folder_items('', 1, 40, 'name', 'asc')
        card = next(it for it in data['items'] if it['path'] == ns1)
        self.assertEqual(card['name'], self.extra.name)
        self.assertTrue(card['root_scope'])
        self.assertEqual(card['total_count'], 2)
        self.assertEqual(card['image_count'], 0)      # 命名空间没有直接图片

        inner = plugin.list_folder_items(ns1, 1, 40, 'name', 'asc')
        names = sorted(it['name'] for it in inner['items'])
        self.assertEqual(names, ['作者A', '作者B'])
        # 缩略图/原图 URL 都带命名空间前缀，Shell 侧按 get_file_roots 逐根校验
        self.assertTrue(inner['items'][0]['cover'].startswith(f'{ns1}/'))

    def test_collect_all_images_across_roots(self):
        plugin = self._multi()
        ns1 = f'__{self.extra.name}'
        all_images = plugin._collect_all_images()
        self.assertEqual(len(all_images), 6)
        self.assertIn(f'{ns1}/作者B/作品1/1.jpg', all_images)
        only_extra = plugin._collect_all_images(ns1)
        self.assertEqual(len(only_extra), 2)
        self.assertTrue(all(p.startswith(ns1) for p in only_extra))

    def test_settings_inherit_through_namespace(self):
        """命名空间节点没有文件夹级设置，直接吃全局值（作为该根的继承源头）。"""
        plugin = self._multi(sort_by='time_name')
        ns1 = f'__{self.extra.name}'
        self.assertEqual(plugin.get_settings(ns1)['sort_by'], 'time_name')
        self.assertEqual(plugin.get_settings(f'{ns1}/作者A')['sort_by'], 'time_name')
        # 在命名空间下的作者目录上单独配置同样生效
        plugin.save_folder_settings(f'{ns1}/作者A', {'sort_by': 'name'})
        self.assertEqual(plugin.get_settings(f'{ns1}/作者A')['sort_by'], 'name')
        self.assertEqual(plugin.get_settings(f'{ns1}/作者B')['sort_by'], 'time_name')

    # ---------- 空目录隐藏 + 新建目录保留可见 ----------

    def test_empty_dirs_are_marked_unreadable(self):
        plugin = self._plugin({'root_dir': str(self.root)})
        albums = {a['path']: a for a in plugin.list_albums()['albums']}
        self.assertEqual(albums['空目录']['readable'], 0)
        self.assertEqual(albums['空容器']['readable'], 0)
        self.assertEqual(albums['空容器/深一层']['readable'], 0)
        self.assertEqual(albums['作者A']['readable'], 3)

    def test_created_folder_kept_visible_until_has_images(self):
        plugin = self._plugin({'root_dir': str(self.root)})
        result = plugin.create_folder('新相册/子目录')
        self.assertTrue(result['success'])
        marks = plugin.get_album_config()['visible_empty_dirs']
        self.assertIn('新相册', marks)
        self.assertIn('新相册/子目录', marks)
        # 目录整个删掉后标记被清理
        self.assertTrue(plugin.delete_folder('新相册')['success'])
        self.assertFalse((self.root / '新相册').exists())
        self.assertEqual(plugin.get_album_config()['visible_empty_dirs'], [])

    def test_delete_folder_refuses_when_images_inside(self):
        plugin = self._plugin({'root_dir': str(self.root)})
        result = plugin.delete_folder('作者A')
        self.assertFalse(result['success'])
        self.assertTrue((self.root / '作者A').exists())

    def test_delete_namespace_root_refused(self):
        plugin = self._multi()
        ns1 = f'__{self.extra.name}'
        self.assertFalse(plugin.delete_folder(ns1)['success'])
        self.assertFalse(plugin.create_folder(f'{ns1}/新目录')['success'])

    # ---------- 子相册默认折叠 ----------

    def test_expand_collapse_tracked_separately(self):
        plugin = self._plugin({'root_dir': str(self.root)})
        plugin.set_album_config('作者A', 'expand')
        cfg = plugin.get_album_config()
        self.assertEqual(cfg['expanded'], ['作者A'])
        self.assertEqual(cfg['collapsed'], [])
        plugin.set_album_config('作者A', 'collapse')
        cfg = plugin.get_album_config()
        self.assertEqual(cfg['expanded'], [])
        self.assertEqual(cfg['collapsed'], ['作者A'])
        # 提升/收回不会清掉其它配置键
        plugin.set_album_config('作者A/作品1', 'promote')
        cfg = plugin.get_album_config()
        self.assertEqual(cfg['promoted'], ['作者A/作品1'])
        self.assertEqual(cfg['collapsed'], ['作者A'])

    # ---------- 目录选择器 ----------

    def test_browse_dir_lists_drives_and_subdirs(self):
        plugin = self._plugin({'root_dir': str(self.root)})
        top = plugin.browse_dir('')
        self.assertTrue(top['entries'])                      # 盘符/文件系统根
        self.assertIsNone(top['parent'])
        listed = plugin.browse_dir(str(self.root))
        names = {e['name'] for e in listed['entries']}
        self.assertIn('作者A', names)
        self.assertIn('空目录', names)
        author = next(e for e in listed['entries'] if e['name'] == '作者A')
        self.assertFalse(author['is_image_dir'])             # 作者目录本身没有直接图片
        work = next(e for e in plugin.browse_dir(str(self.root / '作者A'))['entries']
                    if e['name'] == '作品1')
        self.assertTrue(work['is_image_dir'])
        # 上级目录用绝对路径（临时目录在 Windows 上可能是 8.3 短名，比对统一格式）
        self.assertEqual(Path(listed['parent']),
                         Path(str(self.root.parent)).resolve())
        # 不存在的目录给出错误而不是抛异常
        bad = plugin.browse_dir(str(self.root / '不存在'))
        self.assertEqual(bad['entries'], [])
        self.assertTrue(bad.get('error'))

    def test_browse_dir_drives_sentinel(self):
        """「我的电脑」层：哨兵路径与空路径等价，且没有上级（不会越过顶层）。"""
        plugin = self._plugin({'root_dir': str(self.root)})
        drives = plugin.browse_dir(DRIVES_SENTINEL)
        self.assertEqual([e['path'] for e in drives['entries']],
                         [e['path'] for e in plugin.browse_dir('')['entries']])
        self.assertIsNone(drives['parent'])
        # 盘符根是自己的父目录，所以 parent 为 None；关键是能继续往下浏览
        first = drives['entries'][0]
        inside = plugin.browse_dir(first['path'])
        self.assertEqual(inside['path'], first['path'])
        self.assertFalse(inside.get('error'))

    def test_browse_dir_reports_media_kinds(self):
        """目录选择器同时标出含图片/视频/音乐的目录（共享基建的扩展名分类）。"""
        media = self.root / '混合目录'
        _make_image(media / 'a.jpg')
        (media / 'b.mp4').write_bytes(b'not really a video')
        (media / 'c.mp3').write_bytes(b'not really audio')
        try:
            plugin = self._plugin({'root_dir': str(self.root)})
            entries = {e['name']: e for e in plugin.browse_dir(str(self.root))['entries']}
            self.assertEqual(sorted(entries['混合目录']['kinds']),
                             ['audio', 'image', 'video'])
            self.assertTrue(entries['混合目录']['is_image_dir'])
            self.assertEqual(entries['作者A']['kinds'], [])
            self.assertFalse(entries['作者A']['is_image_dir'])
        finally:
            import shutil
            shutil.rmtree(media, ignore_errors=True)

    def test_list_roots_reports_namespace(self):
        plugin = self._multi()
        roots = plugin.list_roots()
        self.assertEqual(len(roots), 3)
        self.assertTrue(roots[0]['is_primary'])
        self.assertEqual(roots[0]['namespace'], '')
        self.assertEqual(roots[1]['namespace'], self.extra.name)
        self.assertFalse(roots[1]['is_primary'])


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
