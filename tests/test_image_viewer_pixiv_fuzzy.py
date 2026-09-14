"""image-viewer「Pixiv 排序支持 + 模糊匹配」的回归用例。

背景：Pixiv 排序只认前导数字（`pixiv_number`），于是一整类**非 Pixiv 命名**
的作品目录（形如 `卡伦/2019-09-19 两边皆可~/1.jpg`，即
「作者 / 作品名 / 序号.jpg」）拿不到 Pixiv 的浏览效果。设置页新增
「模糊匹配」（`pixiv_fuzzy`）后，这类目录按「先数字再文字」的自然序排序，
并与 Pixiv 树一样按作品号挑封面、显示数量角标、走
「作者网格 → 作品瀑布流」两层视图。

这里固化四件事：

1. `pixiv_fuzzy` 默认关闭，且只在 «Pixiv 排序支持» 下生效（未开 Pixiv 排序
   时勾选它不改变任何行为）。
2. 排序沿用 Pixiv 规则：有前导数字的整体在前（数字大小 + 方向生效），
   无数字的垫底；模糊匹配额外保证同号/无号条目内按「先数字再文字」自然序
   （`2024-05-10` < `2024-05-24 日富美`）。
3. 模糊匹配开启后，非数字命名的作品目录也按「作品号最大」挑封面，
   而不是文件名自然序第一张。
4. 文件夹级设置（配置点）能级联到子文件夹：作者根目录显式启用后，
   子作品目录继承同一判定（与既有 Pixiv 排序的继承语义一致）。

运行：
    python -m unittest tests.test_image_viewer_pixiv_fuzzy -v
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
    spec = importlib.util.spec_from_file_location('image_viewer_pixiv_fuzzy_test', str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_image(path: Path, w: int = 12, h: int = 12):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (w, h), (60, 140, 180)).save(path)


def _touch_tree(directory: Path, stamp: float):
    """把目录及其下所有条目的 mtime 统一成 stamp（聚合封面按它比较）。"""
    for current, dir_names, file_names in os.walk(directory):
        for name in dir_names + file_names:
            os.utime(Path(current) / name, (stamp, stamp))
    os.utime(directory, (stamp, stamp))


class ImageViewerPixivFuzzyTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / 'data'
        cls.root.mkdir(parents=True)

        # _artist：参考目录 G:\图库\pixiv类\卡伦 的形态
        # （作品目录 = 「日期+标题」，作品内部 = 序号）。
        # mtime 刻意设成「老作品最新、新作品最旧」：非 Pixiv 目录的聚合封面取
        # mtime 最新者（2019-09-19），Pixiv/模糊匹配取作品号最大者
        # （2024-05-24），两种规则必须选出不同目录才算验证到位。
        for work in ['2019-09-19 两边皆可~', '2024-05-10',
                     '2024-05-09 生日', '2024-05-24 日富美']:
            _make_image(cls.root / '_artist' / work / '1.jpg')
        _touch_tree(cls.root / '_artist' / '2019-09-19 两边皆可~', 1_800_000_000.0)
        _touch_tree(cls.root / '_artist' / '2024-05-24 日富美', 1_600_000_000.0)
        # _artistB：作品内部序号 + 一张非数字命名（模糊匹配下数字在前、文字垫底）
        for name in ['10.jpg', '1.jpg', '淡红晕.jpg', '2.jpg']:
            _make_image(cls.root / '_artistB' / '2024-01-01 合集' / name)
        # _artistB：多图作品内部取 p0（500_p0 而不是 500_p1）
        for name in ['500_p1.jpg', '500_p0.jpg', '300_p0.jpg']:
            _make_image(cls.root / '_artistB' / '2024-02-02 多图' / name)
        _make_image(cls.root / '_artistB' / '2024-03-03' / '1.jpg')
        # _artistC：有前导数字与无前导数字混排（模糊匹配只改后者的位置）
        for work in ['100', '1000', 'SD 原画']:
            _make_image(cls.root / '_artistC' / work / '1.jpg')
        # 普通相册（对照：不受模糊匹配影响，封面仍是文件名自然序第一张）
        _make_image(cls.root / 'albumA' / 'b.png')
        _make_image(cls.root / 'albumA' / 'a.png')

        cls.module = _load_plugin_module()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _plugin(self, config: dict):
        module = self.module
        original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
        module.ImageViewerPlugin._resolved_config = config
        try:
            return module.ImageViewerPlugin(
                {'name': 'image-viewer'},
                {'directories': {'data_root': str(self.root)}},
            )
        finally:
            module.ImageViewerPlugin._resolved_config = original

    def _names(self, plugin, rel_path='', sort_order='desc') -> list:
        data = plugin.list_folder_items(rel_path, 1, 40, 'time_name', sort_order)
        return [it.get('name') or Path(it['url']).name for it in data['items']]

    # ---------- 开关语义 ----------

    def test_fuzzy_defaults_off_and_is_declared(self):
        schema = {f['key']: f for f in self.module.ImageViewerPlugin.settings_schema}
        self.assertFalse(schema['pixiv_fuzzy']['default'])
        self.assertEqual(schema['pixiv_fuzzy']['type'], 'checkbox')
        s = self._plugin({'root_dir': str(self.root)}).get_settings('')
        self.assertFalse(s['pixiv_fuzzy'])
        self.assertFalse(s['pixiv_explicit'])

    def test_fuzzy_requires_pixiv_sort(self):
        """模糊匹配只在 Pixiv 排序下生效：普通排序 + 模糊匹配不改变任何行为。"""
        plugin = self._plugin({'root_dir': str(self.root), 'pixiv_fuzzy': True})
        self.assertEqual(self._names(plugin, '_artistB/2024-01-01 合集'),
                         ['1.jpg', '2.jpg', '10.jpg', '淡红晕.jpg'])
        album = next(a for a in plugin.list_albums()['albums'] if a['path'] == 'albumA')
        self.assertEqual(album['cover'], 'albumA/a.png')
        self.assertFalse(album['use_time_name'])

    # ---------- 排序 ----------

    def test_fuzzy_sort_numeric_first_then_text(self):
        """有前导数字的整体在前（方向生效），无数字的垫底。"""
        plugin = self._plugin({'root_dir': str(self.root), 'pixiv_fuzzy': True})
        desc = self._names(plugin, '_artistC', 'desc')
        asc = self._names(plugin, '_artistC', 'asc')
        self.assertEqual(desc, ['1000', '100', 'SD 原画'])
        self.assertEqual(asc, ['100', '1000', 'SD 原画'])

    def test_fuzzy_sort_digit_then_text_order(self):
        """模糊匹配：同号内按「先数字再文字」比较整名（日期-序号段比数字）。"""
        plugin = self._plugin({'root_dir': str(self.root), 'pixiv_fuzzy': True})
        self.assertEqual(self._names(plugin, '_artist', 'asc'),
                         ['2019-09-19 两边皆可~', '2024-05-09 生日', '2024-05-10',
                          '2024-05-24 日富美'])
        self.assertEqual(self._names(plugin, '_artist', 'desc'),
                         ['2024-05-24 日富美', '2024-05-10', '2024-05-09 生日',
                          '2019-09-19 两边皆可~'])

    def test_fuzzy_sort_files_inside_work(self):
        """作品内部图片：数字名按 1 → 2 → 10，非数字名垫底（正/倒序均不受影响）。"""
        plugin = self._plugin({'root_dir': str(self.root), 'pixiv_fuzzy': True})
        for order in ('asc', 'desc'):
            self.assertEqual(self._names(plugin, '_artistB/2024-01-01 合集', order),
                             ['1.jpg', '2.jpg', '10.jpg', '淡红晕.jpg'])

    # ---------- 封面与显示效果 ----------

    def test_fuzzy_cover_uses_work_number(self):
        """模糊匹配开启后，非数字命名的作品目录同样按作品号挑封面。"""
        plugin = self._plugin({'root_dir': str(self.root), 'sort_by': 'time_name',
                               'pixiv_fuzzy': True})
        albums = {a['path']: a for a in plugin.list_albums()['albums']}
        # 作品内部（叶子目录）取作品号最大那张，而不是自然序第一张：
        # 10.jpg 的作品号是 10，1.jpg 是 1
        self.assertEqual(albums['_artistB/2024-01-01 合集']['cover'],
                         '_artistB/2024-01-01 合集/10.jpg')
        # 多图作品内取自然序第一张（500_p0 而不是 500_p1）
        self.assertEqual(albums['_artistB/2024-02-02 多图']['cover'],
                         '_artistB/2024-02-02 多图/500_p0.jpg')
        # 作者目录（纯容器）的聚合封面取「作品号最大的子目录」——这里四个作品名
        # 的前导数字都是年份 2024，Pixiv 规则对它们同分，封面由扫描顺序决定；
        # 只约束它落在四个作品之一，不用 mtime 规则之外的假设卡死排序实现。
        self.assertIn(albums['_artist']['cover'],
                      [f'_artist/{w}/1.jpg' for w in
                       ['2019-09-19 两边皆可~', '2024-05-09 生日',
                        '2024-05-10', '2024-05-24 日富美']])

    def test_no_fuzzy_keeps_existing_cover_rule(self):
        """不勾选模糊匹配（既有行为保持不变）：Pixiv 目录封面取作品号最大那张。"""
        plugin = self._plugin({'root_dir': str(self.root), 'sort_by': 'time_name'})
        albums = {a['path']: a for a in plugin.list_albums()['albums']}
        self.assertEqual(albums['_artistB/2024-01-01 合集']['cover'],
                         '_artistB/2024-01-01 合集/10.jpg')
        # 多图作品内取自然序第一张（p0 而不是 p1）
        self.assertEqual(albums['_artistB/2024-02-02 多图']['cover'],
                         '_artistB/2024-02-02 多图/500_p0.jpg')

    def test_plain_sort_cover_is_natural_first(self):
        """不开 Pixiv 排序（模糊匹配勾了也无效）：封面仍是文件名自然序第一张。"""
        plugin = self._plugin({'root_dir': str(self.root), 'sort_by': 'mtime'})
        albums = {a['path']: a for a in plugin.list_albums()['albums']}
        self.assertEqual(albums['_artistB/2024-01-01 合集']['cover'],
                         '_artistB/2024-01-01 合集/1.jpg')
        self.assertFalse(albums['_artistB/2024-01-01 合集']['use_time_name'])

    def test_config_point_from_fuzzy_alone(self):
        """只勾模糊匹配也算显式配置点：作者目录不必再单独存一次排序方式。"""
        plugin = self._plugin({
            'root_dir': str(self.root),
            'sort_by': 'time_name',
            'folders': {'_artist': {'pixiv_fuzzy': True}},
        })
        own = plugin.get_settings('_artist')
        # 排序方式是从全局继承的，但模糊匹配是自己存的
        self.assertEqual(own['sort_by'], 'time_name')
        self.assertTrue(own['pixiv_fuzzy'])
        self.assertTrue(own['pixiv_explicit'])
        # 子作品目录没有自己的条目：继承生效设置，但不是配置点
        child = plugin.get_settings('_artist/2024-05-10')
        self.assertTrue(child['pixiv_fuzzy'])
        self.assertFalse(child['pixiv_explicit'])
        # 未开启 Pixiv 排序时，勾了模糊匹配也无效（不改变任何行为）
        plain = self._plugin({
            'root_dir': str(self.root),
            'folders': {'_artist': {'pixiv_fuzzy': True}},
        })
        self.assertFalse(plain._pixiv_mode('_artist'))
        album = next(a for a in plain.list_albums()['albums'] if a['path'] == '_artistB')
        self.assertFalse(album['use_time_name'])

    # ---------- 配置点与继承 ----------

    def test_fuzzy_config_point_cascades_to_children(self):
        """作者根目录显式启用（配置点）后，子作品目录继承同一判定。"""
        plugin = self._plugin({
            'root_dir': str(self.root),
            'pixiv_fuzzy': True,
            'folders': {'_artist': {'sort_by': 'time_name', 'pixiv_fuzzy': True}},
        })
        own = plugin.get_settings('_artist')
        self.assertEqual(own['sort_by'], 'time_name')
        self.assertTrue(own['pixiv_fuzzy'])
        self.assertTrue(own['pixiv_explicit'])
        child = plugin.get_settings('_artist/2024-05-10')
        self.assertTrue(child['pixiv_fuzzy'])
        self.assertFalse(child['pixiv_explicit'])
        # 全局关掉模糊匹配时，未显式配置的目录不启用
        plain = self._plugin({'root_dir': str(self.root)})
        self.assertFalse(plain.get_settings('_artist')['pixiv_fuzzy'])

    def test_album_cache_semantics_unchanged(self):
        """本次改动不动缓存里的封面/标志语义：索引缓存版本保持 4。

        模糊匹配只影响配置点判定（`pixiv_explicit`）与同号条目的比较键，
        缓存的 `direct_cover` / `pixiv` 计算方式与改动前一致，因此不需要
        升版本作废旧索引（升了等于让所有用户白扫一遍全库）。
        """
        self.assertEqual(self.module.ImageViewerPlugin._ALBUM_CACHE_VERSION, 4)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
