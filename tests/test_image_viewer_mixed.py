"""image-viewer 混合瀑布流的单元测试。

场景：Pixiv 模式目录 —— 一个文件夹里既有子文件夹（画师某次作品的差分图
xxx_p0 / xxx_p1 ...），又有单图。验证：

- 相册封面取文件名自然序第一张（p0）
- list_folder_items 返回「子相册卡片 + 单图」的混合列表（只处理一层嵌套）
- 子相册卡片带 p0 封面、圆圈数量角标所需计数、封面尺寸
- 图片默认按文件名自然排序（p0 < p1 < p2 < ... < p10）
- 分页、缓存命中、设置默认值

运行：
    python -m unittest tests.test_image_viewer_mixed -v
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


def _load_plugin_module():
    main_path = PROJECT_ROOT / 'plugins' / 'image-viewer' / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('image_viewer_main_test', str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_image(path: Path, w: int, h: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (w, h), (200, 120, 40)).save(path)


class ImageViewerMixedTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)

        # workA：差分图 p0 / p1 / p2 / p10（验证自然排序与 p0 封面）
        for name, w, h in [('111_p0.png', 300, 200), ('111_p1.png', 300, 300),
                           ('111_p2.png', 200, 300), ('111_p10.png', 400, 200)]:
            _make_image(cls.root / 'workA' / name, w, h)
        # workB：两张图
        for name in ['222_p0.png', '222_p1.png']:
            _make_image(cls.root / 'workB' / name, 250, 250)
        # 根目录单图（混杂场景中的单图）
        _make_image(cls.root / 'single.jpg', 320, 180)
        # 空文件夹
        (cls.root / 'empty').mkdir()
        # 隐藏项 / 缓存目录应被忽略
        _make_image(cls.root / '.hidden.png', 10, 10)
        (cls.root / '.cache').mkdir(exist_ok=True)

        cls.module = _load_plugin_module()
        cls.module.ImageViewerPlugin._resolved_config = {'root_dir': str(cls.root)}
        cls.plugin = cls.module.ImageViewerPlugin(
            {'name': 'image-viewer'},
            {'directories': {'data_root': str(cls.root)}},
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_album_cover_is_p0(self):
        albums = self.plugin.list_albums()['albums']
        workA = next(a for a in albums if a['path'] == 'workA')
        self.assertEqual(workA['direct_count'], 4)
        self.assertEqual(workA['cover'], 'workA/111_p0.png')
        self.assertFalse(workA['use_time_name'])  # 默认插件无 Pixiv 设置
        workB = next(a for a in albums if a['path'] == 'workB')
        self.assertEqual(workB['cover'], 'workB/222_p0.png')

    def test_folder_items_mixed_root(self):
        data = self.plugin.list_folder_items('', 1, 40)
        items = data['items']
        # empty + workA + workB 三个子相册卡片 + 1 张根目录单图
        self.assertEqual(data['total'], 4)
        self.assertEqual(data['image_total'], 1)
        types = [it['type'] for it in items]
        self.assertEqual(types[:3], ['album', 'album', 'album'])
        self.assertEqual(types[3], 'image')
        # 子相册卡片：p0 封面 + 数量 + 封面尺寸
        workA = next(it for it in items if it['path'] == 'workA')
        self.assertEqual(workA['cover'], 'workA/111_p0.png')
        self.assertEqual(workA['image_count'], 4)
        self.assertEqual((workA['width'], workA['height']), (300, 200))
        empty = next(it for it in items if it['path'] == 'empty')
        self.assertEqual(empty['image_count'], 0)
        self.assertEqual(empty['cover'], '')

    def test_folder_items_order_p0_to_p1(self):
        data = self.plugin.list_folder_items('workA', 1, 40)
        urls = [it['url'] for it in data['items']]
        self.assertEqual(urls, ['workA/111_p0.png', 'workA/111_p1.png',
                                'workA/111_p2.png', 'workA/111_p10.png'])

    def test_folder_items_pagination(self):
        page1 = self.plugin.list_folder_items('', 1, 3)
        self.assertEqual(len(page1['items']), 3)
        self.assertTrue(page1['has_next'])
        page2 = self.plugin.list_folder_items('', 2, 3)
        self.assertEqual(len(page2['items']), 1)
        self.assertFalse(page2['has_next'])

    def test_folder_items_all_offset_pagination(self):
        """分页对齐：第 2+ 页瓦片在完整连续序列中的起始偏移 all_offset 必须正确，
        否则点击灯箱会错位打开到序列开头的图片。"""
        page1 = self.plugin.list_folder_items('', 1, 3, 'name', 'asc')
        self.assertEqual(page1['all_offset'], 0)
        # page2 只剩 single.jpg；之前为 empty(0) + workA(4) + workB(2) = 6 张
        page2 = self.plugin.list_folder_items('', 2, 3, 'name', 'asc')
        self.assertEqual(page2['all_offset'], 6)
        self.assertEqual(page2['items'][0]['url'], 'single.jpg')
        # 第 2 页首项（single.jpg）应对齐完整序列的第 all_offset 张
        self.assertEqual(page2['all_images'][page2['all_offset']]['url'], 'single.jpg')
        # 序列长度 = 各子文件夹图片 + 单图
        self.assertEqual(len(page2['all_images']), 7)

    def test_list_images_natural_sort(self):
        data = self.plugin.list_images('workA', 1, 40, 'name', 'asc')
        urls = [im['url'] for im in data['images']]
        self.assertEqual(urls, ['workA/111_p0.png', 'workA/111_p1.png',
                                'workA/111_p2.png', 'workA/111_p10.png'])
        data_desc = self.plugin.list_images('workA', 1, 40, 'name', 'desc')
        self.assertEqual([im['url'] for im in data_desc['images']],
                         list(reversed(urls)))

    def test_folder_items_time_name_standard(self):
        """新标准「时间+文件名」：图片一律按文件名自然序（p0 → p1），
        不受文件 mtime 差异影响；正序/倒序选择无效。"""
        base = 1_700_000_000.0
        for name, delta in [('111_p0.png', 0), ('111_p1.png', 100),
                            ('111_p2.png', 200), ('111_p10.png', 50)]:
            p = self.root / 'workA' / name
            os.utime(p, (base + delta, base + delta))

        data_desc = self.plugin.list_folder_items('workA', 1, 40, 'time_name', 'desc')
        urls_desc = [it['url'] for it in data_desc['items']]
        # mtime 各不相同，但 time_name 下图片仍按 p0 → p1 → p2 → p10
        self.assertEqual(urls_desc, ['workA/111_p0.png', 'workA/111_p1.png',
                                     'workA/111_p2.png', 'workA/111_p10.png'])
        # 正序/倒序选择无效：asc 结果与 desc 完全一致
        data_asc = self.plugin.list_folder_items('workA', 1, 40, 'time_name', 'asc')
        self.assertEqual([it['url'] for it in data_asc['items']], urls_desc)

    def test_folder_items_all_images_flat_sequence(self):
        """连续浏览序列：子文件夹内部按【它自己生效的设置】排序，不受当前视图排序影响。"""
        base = 1_700_000_000.0
        for name, delta in [('111_p0.png', 0), ('111_p1.png', 100),
                            ('111_p2.png', 200), ('111_p10.png', 50)]:
            os.utime(self.root / 'workA' / name, (base + delta, base + delta))
        module = self.module
        orig = module.ImageViewerPlugin._resolved_config
        try:
            module.ImageViewerPlugin._resolved_config = {
                'root_dir': str(self.root),
                'folders': {
                    'workA': {'sort_by': 'time_name', 'sort_order': 'asc'},
                    'workB': {'sort_by': 'name', 'sort_order': 'asc'},
                },
            }
            plugin = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = orig
        # 当前视图按 name 排序，但 workA 自己有 time_name → 其图片按 p0→p1 展开（不受 mtime 影响）
        data = plugin.list_folder_items('', 1, 40, 'name', 'asc')
        seq = [im['url'] for im in data['all_images']]
        self.assertEqual(seq, [
            'workA/111_p0.png', 'workA/111_p1.png', 'workA/111_p2.png', 'workA/111_p10.png',
            'workB/222_p0.png', 'workB/222_p1.png',
            'single.jpg',
        ])
        # 封面始终是 p0（文件名自然序第一张），即使 p2 的 mtime 最新
        workA = next(it for it in data['items'] if it['path'] == 'workA')
        self.assertEqual(workA['cover'], 'workA/111_p0.png')
        # use_time_name 角标标记按子文件夹自身设置
        self.assertTrue(workA['use_time_name'])
        workB = next(it for it in data['items'] if it['path'] == 'workB')
        self.assertFalse(workB['use_time_name'])

    def test_folder_items_all_images_time_name(self):
        """time_name 下连续序列：同一作品内按文件名自然序（p0 → p1），不受 mtime 差异影响。"""
        base = 1_700_000_000.0
        for name, delta in [('111_p0.png', 0), ('111_p1.png', 100),
                            ('111_p2.png', 200), ('111_p10.png', 50)]:
            os.utime(self.root / 'workA' / name, (base + delta, base + delta))
        data = self.plugin.list_folder_items('workA', 1, 40, 'time_name', 'desc')
        seq = [im['url'] for im in data['all_images']]
        self.assertEqual(seq, ['workA/111_p0.png', 'workA/111_p1.png',
                               'workA/111_p2.png', 'workA/111_p10.png'])

    def test_album_cache_version_invalidation(self):
        """旧版(version 2)相册索引缓存作废：封面重新按当前规则计算，不复用旧的 mtime 封面。"""
        import json
        cache_file = self.root / '.cache' / 'albums_index.json'
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        # 模拟旧代码按最新 mtime 取的封面（p2），并带上旧版本号
        cache_file.write_text(json.dumps({
            'version': 2,
            'dirs': {'workA': {
                'path': 'workA', 'name': 'workA', 'depth': 1, 'parent': '',
                'direct_count': 4, 'direct_cover': 'workA/111_p2.png',
                'direct_mtime': 123.0, 'mtime': 123.0,
                'has_children': False, 'children': [],
            }},
        }), encoding='utf-8')
        module = self.module
        orig = module.ImageViewerPlugin._resolved_config
        try:
            module.ImageViewerPlugin._resolved_config = {'root_dir': str(self.root)}
            plugin = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = orig
        albums = plugin.list_albums()['albums']
        workA = next(a for a in albums if a['path'] == 'workA')
        self.assertEqual(workA['cover'], 'workA/111_p0.png')

    def test_album_cache_persist_current_version(self):
        """当前版本相册索引应写入 _ALBUM_CACHE_VERSION，避免每次重启都被当作旧缓存作废。"""
        version = self.module.ImageViewerPlugin._ALBUM_CACHE_VERSION
        self.plugin.list_albums()
        self.assertEqual(self.plugin._album_cache.get('version'), version)
        cache_file = self.root / '.cache' / 'albums_index.json'
        if cache_file.exists():
            import json
            saved = json.loads(cache_file.read_text(encoding='utf-8'))
            self.assertEqual(saved.get('version'), version)

    def test_folder_settings_ignore_root_dir(self):
        """文件夹级设置不应保存 root_dir，避免出现“保存了但根目录没变”的困惑。"""
        class FakeStore:
            def __init__(self):
                self.data = {}
            def get(self, name):
                return self.data.get(name, {})
            def set(self, name, value):
                self.data[name] = value

        plugin = self.module.ImageViewerPlugin(
            {'name': 'image-viewer'},
            {'directories': {'data_root': str(self.root)}},
        )
        plugin._settings_store = FakeStore()
        plugin.save_folder_settings('photos/2026', {
            'root_dir': '/tmp/should-not-be-stored',
            'sort_by': 'name',
            'sort_order': 'asc',
        })
        stored = plugin._settings_store.get('image-viewer')
        folder_settings = stored.get('folders', {}).get('photos/2026', {})
        self.assertNotIn('root_dir', folder_settings)
        self.assertEqual(folder_settings['sort_by'], 'name')

    def test_settings_defaults(self):
        s = self.plugin.get_settings('')
        self.assertEqual(s['sort_by'], 'mtime')
        self.assertEqual(s['sort_order'], 'desc')

    def test_global_settings_persist_across_instances(self):
        """全局设置必须能读回：壳设置面板与 save_folder_settings('') 写的是插件级键
        （sort_by / row_height / …），而 get_settings 原先只读 folders['__global__']
        （全仓没有写入方），于是"保存到全局"当场生效、重启后静默回落到硬默认值。"""

        class FakeStore:
            def __init__(self):
                self.data = {}

            def get(self, name):
                return self.data.get(name, {})

            def set(self, name, value):
                self.data[name] = value

            def update(self, name, values):
                merged = dict(self.data.get(name, {}))
                merged.update(values)
                self.data[name] = merged
                return merged

        store = FakeStore()
        plugin = self.module.ImageViewerPlugin(
            {'name': 'image-viewer'},
            {'directories': {'data_root': str(self.root)}},
        )
        plugin._settings_store = store
        result = plugin.save_folder_settings('', {
            'row_height': 300, 'per_page': 60, 'sort_by': 'name', 'sort_order': 'asc',
        })
        self.assertTrue(result.get('success'))
        applied = plugin.get_settings('')
        self.assertEqual(applied['row_height'], 300)
        self.assertEqual(applied['sort_by'], 'name')
        # 新实例 = 重启后重新加载，仍能读到同一份设置
        reopened = self.module.ImageViewerPlugin(
            {'name': 'image-viewer'},
            {'directories': {'data_root': str(self.root)}},
        )
        reopened._settings_store = store
        persisted = reopened.get_settings('')
        self.assertEqual(persisted['row_height'], 300)
        self.assertEqual(persisted['sort_by'], 'name')
        self.assertEqual(persisted['sort_order'], 'asc')

    def test_legacy_global_key_does_not_shadow_new_save(self):
        """folders['__global__'] 是旧写入路径：它要能补齐插件级键没存过的项，
        但不能压住新保存的值（否则设置页保存后重启仍看到旧值）。"""

        class FakeStore:
            def __init__(self, data=None):
                self.data = data or {}

            def get(self, name):
                return self.data.get(name, {})

            def set(self, name, value):
                self.data[name] = value

            def update(self, name, values):
                merged = dict(self.data.get(name, {}))
                merged.update(values)
                self.data[name] = merged
                return merged

        # 插件级键 = 新写入路径保存过的值；__global__ = 旧版本留下的全局值
        store = FakeStore({'image-viewer': {'row_height': 300, 'sort_by': 'name'}})
        module = self.module
        original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
        try:
            module.ImageViewerPlugin._resolved_config = {
                'root_dir': str(self.root),
                'folders': {'__global__': {'sort_by': 'time_name', 'per_page': 60}},
            }
            plugin = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = original
        plugin._settings_store = store
        settings = plugin.get_settings('')
        self.assertEqual(settings['sort_by'], 'name')     # 新保存优先于 __global__
        self.assertEqual(settings['row_height'], 300)     # 新保存的插件级键生效
        self.assertEqual(settings['per_page'], 60)        # __global__ 补齐没存过的项

    def test_settings_cascade(self):
        """父文件夹启用 time_name 后，其下所有子文件夹继承；单独修改的子文件夹以自己为准。"""
        module = self.module
        orig = module.ImageViewerPlugin._resolved_config
        try:
            module.ImageViewerPlugin._resolved_config = {
                'root_dir': str(self.root),
                'folders': {
                    'pixiv/following': {'sort_by': 'time_name', 'sort_order': 'asc'},
                    'pixiv/following/artistA': {'sort_by': 'name', 'sort_order': 'asc'},
                },
            }
            plugin = module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = orig
        # 自身精确命中（显式配置点）
        s_self = plugin.get_settings('pixiv/following')
        self.assertEqual(s_self['sort_by'], 'time_name')
        self.assertTrue(s_self['pixiv_explicit'])
        # 子文件夹继承父文件夹的 time_name，但自身非显式配置点
        s_child = plugin.get_settings('pixiv/following/artistB')
        self.assertEqual(s_child['sort_by'], 'time_name')
        self.assertFalse(s_child['pixiv_explicit'])
        # 未存任何 Pixiv 相关键的目录也不是配置点（只继承全局/父级）
        self.assertFalse(plugin.get_settings('pixiv/following/artistB/deep')['pixiv_explicit'])
        # 单独修改的子文件夹以自己为准（不继承父级）
        self.assertEqual(plugin.get_settings('pixiv/following/artistA')['sort_by'], 'name')
        # 单独修改的子文件夹继续向其子文件夹传播
        self.assertEqual(plugin.get_settings('pixiv/following/artistA/deep')['sort_by'], 'name')
        # 无关文件夹回退到全局/默认
        self.assertEqual(plugin.get_settings('photos/2026')['sort_by'], 'mtime')

    def test_pixiv_sort_by_number(self):
        """Pixiv 排序支持：顶层按前导数字（作品 ID）排序且方向生效，无数字名排最后；
        作品内部多图片仍以 p0 开始（不受方向影响）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for fid in ['1000', '200', '9999', 'abc-artist']:
                (root / fid).mkdir()
                _make_image(root / fid / f'{fid}_p0.jpg', 100, 100)
                _make_image(root / fid / f'{fid}_p1.jpg', 100, 100)
            module = self.module
            orig = module.ImageViewerPlugin._resolved_config
            try:
                # 全局启用 Pixiv 排序，作品文件夹内部继承 → p0 起
                module.ImageViewerPlugin._resolved_config = {
                    'root_dir': str(root),
                    'folders': {'__global__': {'sort_by': 'time_name', 'sort_order': 'desc'}},
                }
                plugin = module.ImageViewerPlugin(
                    {'name': 'image-viewer'},
                    {'directories': {'data_root': str(root)}},
                )
            finally:
                module.ImageViewerPlugin._resolved_config = orig
            # 倒序：数字大在前（新作品在前），无数字名排最后
            desc = plugin.list_folder_items('', 1, 40, 'time_name', 'desc')
            self.assertEqual([it['path'] for it in desc['items']],
                             ['9999', '1000', '200', 'abc-artist'])
            # 正序：数字小在前
            asc = plugin.list_folder_items('', 1, 40, 'time_name', 'asc')
            self.assertEqual([it['path'] for it in asc['items']],
                             ['200', '1000', '9999', 'abc-artist'])
            # 作品内部多图片仍以 p0 开始（倒序下也从 p0 起）
            seq = [im['url'] for im in desc['all_images']]
            self.assertEqual(seq[:2], ['9999/9999_p0.jpg', '9999/9999_p1.jpg'])

    def test_regenerate_thumbs(self):
        """更新缩略图：删除 SQLite 缓存并重新生成；非法路径报错。"""
        result = self.plugin.regenerate_thumbs(['workA/111_p0.png'])
        self.assertEqual(result['errors'], [])
        self.assertIn('workA/111_p0.png', result['regenerated'])
        thumb_data = self.plugin.get_thumb_data('workA/111_p0.png')
        self.assertIsNotNone(thumb_data)
        self.assertGreater(len(thumb_data[0]), 0)
        db_path = self.root / '.cache' / 'thumbs.db'
        self.assertTrue(db_path.exists())
        # 非法路径报错
        bad = self.plugin.regenerate_thumbs(['../evil.png'])
        self.assertTrue(bad['errors'])

    def test_refresh(self):
        """全局刷新：清空缓存后视图仍可正常加载。"""
        r = self.plugin.refresh()
        self.assertTrue(r.get('success'))
        data = self.plugin.list_folder_items('', 1, 40)
        self.assertGreater(data['total'], 0)
        albums = self.plugin.list_albums()['albums']
        self.assertGreater(len(albums), 0)

    def test_rebuild_all_clears_cache_and_rebuilds(self):
        """全量重建：清空旧缓存并后台生成全部缩略图。"""
        import sqlite3
        import time
        first = self.plugin.get_thumb_data('workA/111_p0.png')
        self.assertIsNotNone(first)
        result = self.plugin.rebuild_all()
        self.assertTrue(result.get('started'))
        # 等待后台任务完成
        status = {}
        for _ in range(300):
            status = self.plugin.rebuild_status()
            if status.get('done'):
                break
            time.sleep(0.02)
        self.assertTrue(status.get('done'))
        self.assertTrue(status.get('success'))
        # 旧文件缩略图目录已被清空/重建
        self.assertFalse((self.root / '.cache' / 'thumbs' / 'workA' / '111_p0.png').exists())
        # SQLite 中已生成全部缩略图
        conn = sqlite3.connect(self.root / '.cache' / 'thumbs.db')
        try:
            count = conn.execute('SELECT COUNT(*) FROM thumbs').fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(count, 0)

    def test_container_folder_card(self):
        """纯容器子文件夹（只有子文件夹、无直接图片）：卡片带递归总数与代表封面
        （画师文件夹 = 最新作品 p0 + 总图片数），image_count 仍为 0（不参与连续序列展开）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artist = root / 'artist100'
            for wid in ['500', '9000']:
                (artist / wid).mkdir(parents=True)
                _make_image(artist / wid / f'{wid}_p0.jpg', 100, 100)
                _make_image(artist / wid / f'{wid}_p1.jpg', 100, 100)
            module = self.module
            orig = module.ImageViewerPlugin._resolved_config
            try:
                module.ImageViewerPlugin._resolved_config = {
                    'root_dir': str(root),
                    'folders': {'__global__': {'sort_by': 'time_name', 'sort_order': 'desc'}},
                }
                plugin = module.ImageViewerPlugin(
                    {'name': 'image-viewer'},
                    {'directories': {'data_root': str(root)}},
                )
            finally:
                module.ImageViewerPlugin._resolved_config = orig
            data = plugin.list_folder_items('', 1, 40, 'time_name', 'desc')
            card = data['items'][0]
            self.assertEqual(card['path'], 'artist100')
            self.assertEqual(card['image_count'], 0)          # 无直接图片
            self.assertTrue(card['has_children'])
            self.assertTrue(card['pending'])                  # 索引未建 → 二级加载
            self.assertIsNone(card['total_count'])            # 待补，0 是"确实是空相册"
            self.assertEqual(card['cover'], '')
            self.assertEqual(data['pending_count'], 1)
            self.assertTrue(data['sequence_pending'])
            # 容器（待补）不参与连续序列展开
            self.assertEqual(data['all_images'], [])

            # 二级加载补齐：封面 = pixiv 倒序第一个含图子目录（9000）的 p0，
            # 计数 = 递归总数 2 + 2
            tiles = plugin.load_album_tiles(['artist100'])['tiles']
            self.assertEqual(tiles['artist100']['total_count'], 4)
            self.assertEqual(tiles['artist100']['cover'], 'artist100/9000/9000_p0.jpg')
            # 补齐结果回写索引缓存：再列一次目录就直接拿得到，不必再走子树
            again = plugin.list_folder_items('', 1, 40, 'time_name', 'desc')
            card2 = again['items'][0]
            self.assertFalse(card2['pending'])
            self.assertEqual(card2['total_count'], 4)
            self.assertEqual(card2['cover'], 'artist100/9000/9000_p0.jpg')
            self.assertEqual(again['pending_count'], 0)
            # 瓦片补好了，但连续序列仍然不含容器里的图片：序列要等打开灯箱时
            # 由 `list_album_images` 取（展开容器要读整棵子树，不能放在列目录里）
            self.assertTrue(again['sequence_pending'])
            seq = plugin.list_album_images('')['images']
            # artist100 是容器：序列里锚一张代表封面（9000 的 p0）当入口，不展开子树
            self.assertEqual([im['url'] for im in seq], ['artist100/9000/9000_p0.jpg'])
            self.assertEqual(plugin.list_album_images('')['offset']['artist100'], 0)
            # 进到该容器内一层就能拿到作品级的完整序列（作品按文件名自然序）
            inner_seq = plugin.list_album_images('artist100')['images']
            self.assertEqual([im['url'] for im in inner_seq],
                             ['artist100/500/500_p0.jpg', 'artist100/500/500_p1.jpg',
                              'artist100/9000/9000_p0.jpg', 'artist100/9000/9000_p1.jpg'])
            self.assertEqual(plugin.list_album_images('artist100')['offset']['artist100/500'], 0)
            self.assertEqual(plugin.list_album_images('artist100')['offset']['artist100/9000'], 2)


class FreshnessTimingTestCase(unittest.TestCase):
    """统一刷新接入后的两个时序契约（都是实测出来的用户可见卡顿）。

    1. **校验结束 = 立刻可读**：相册索引重建（全树枚举 + 变化目录重扫）实测在
       6000 张 / 1400 目录上要 1.4-2.7s。它原先留在任务外，用户看到的是"进度条满了、
       界面还要卡几秒"；现在由 `on_pass_end` 在任务里做完，"完成"就意味着下一帧
       命中缓存（`cached: True`、零重扫）。
    2. **只作废受影响的相册节点**：整表作废会让 1400 个目录里没变的 1399 个一起
       重扫（实测 2.74s vs 1.40s）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for name in ['111_p0.png', '111_p1.png']:
            _make_image(self.root / 'workA' / name, 300, 200)
        _make_image(self.root / 'workB' / '222_p0.png', 250, 250)
        module = _load_plugin_module()
        # 每次调用都是新模块对象（`_load_plugin_module` 重新 exec），直接赋值即可
        module.ImageViewerPlugin._resolved_config = {'root_dir': str(self.root)}
        self.plugin = module.ImageViewerPlugin(
            {'name': 'image-viewer'},
            {'directories': {'data_root': str(self.root)}},
        )
        from shell.backend.freshness import engine_for

        self.engine = engine_for(self.plugin)
        self.assertIsNotNone(self.engine, 'image-viewer 必须声明 freshness_spec()')
        self.engine.spec.min_sync_interval = 0.0

    def test_verify_leaves_a_warm_album_index(self):
        report = self.engine.verify()

        self.assertIn('albums', report['pass_end'], '校验要在任务里把索引重建完')
        stamp = self.plugin._albums_cached_at
        result = self.plugin.list_albums()
        self.assertTrue(result.get('cached'), '校验结束后第一帧必须直接命中缓存')
        self.assertEqual(self.plugin._albums_cached_at, stamp, '不该再重扫一遍目录树')

    def test_sync_invalidates_only_touched_album_nodes(self):
        self.engine.sync('', force=True)               # 先建指纹，之后的同步才是增量的
        self.plugin.list_albums()                      # 再建索引
        cache = self.plugin._album_cache['dirs']
        self.assertIn('workA', cache)
        self.assertIn('workB', cache)

        _make_image(self.root / 'workA' / '111_p2.png', 300, 200)   # 只动 workA
        self.engine.sync('', force=True)

        cache = self.plugin._album_cache['dirs']
        self.assertNotIn('workA', cache, '变化目录要作废')
        self.assertNotIn('', cache, '合成根聚合整棵树，必须作废')
        self.assertIn('workB', cache, '没变的目录要继续复用，否则整库重扫')

    def test_stat_entries_stays_off_for_the_album_tree(self):
        """整树被动同步不逐条 stat：进一次相册页不该把整库每个文件 stat 一遍。

        边界由校验兜底（audit 逐项比对指纹），插件读图时也按 mtime 校验尺寸元数据。
        """
        self.assertFalse(self.engine.spec.stat_entries)
        self.engine.sync('', force=True)               # 铺底

        _make_image(self.root / 'workA' / '111_p2.png', 300, 200)
        report = self.engine.sync('', force=True)
        self.assertEqual(report['added'], 1, '新增文件仍要被同步发现')

        # 原地改写：文件 mtime 变了，但**目录 mtime 不变**（改写文件不会动父目录）
        target = self.root / 'workB' / '222_p0.png'
        _make_image(target, 250, 250)
        report = self.engine.sync('', force=True)
        self.assertEqual(report['added'] + report['changed'], 0,
                         '目录级短路按设计看不见原地改写（同步只看目录 mtime 与名字个数）')
        report = self.engine.verify()
        self.assertEqual(report['changed'], 1, '校验要逐项比对指纹，看见原地改写')


if __name__ == '__main__':
    unittest.main()
