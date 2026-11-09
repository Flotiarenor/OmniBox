"""image-viewer「最近更新的图片」分页的单元测试（以「作品」为更新单元）。

场景：时间线视图的「按图片」模式。真实图库是 `pixiv/<画师>/<作品号>/<作品号>_p0..pN`，
同一个作品的差分页必须只占一张卡（封面 p0 + 页数角标）；漫画目录
`本子/1479415/00001.jpg…` 是连号页码，同样整本只占一张卡（封面第一页、标题取目录名）。

验证契约：

- 多页作品合成一个作品单元（`type='album'`，封面 p0、image_count 为页数），其余文件
  各成一个单图单元；
- 相册单元带 `cover_mtime`（封面文件自身的 mtime，整本取书封面对应文件，取不到为 0.0），
  与 `list_albums` / `list_folder_items` / `load_album_tiles` 同名字段：替换封面后
  前端缩略图 URL 的版本号能前进；
- 连号页码的目录（漫画 / 本子）整本一个单元；页码稀疏（画师目录里平铺的作品号）或
  命名混合（散图）时各张分列，不会被误并；
- 单元按 mtime 倒序（作品取组内最新页），同 mtime 按代表 url（作品封面 / 单图自身）升序；
- 按单元分页不重不漏；`total` 是单元数、`image_total` 是整库图片数；
- 第 i 个单元的封面在 `all_images` 里的下标 = 前面各单元 image_count / 1 的累加；
- 只展开有新单元的目录（`scanned_dirs` 明显小于目录总数，退化成扫全树时会相等）；
- 空库；page / per_page 夹到 [1, 200]；
- 扫描预算用尽时 `partial=True`，且 `total` 收敛为本次能排列出的单元数。

运行：
    venv/Scripts/python.exe -m unittest tests.test_image_viewer_recent -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image


def _load_plugin_module():
    main_path = PROJECT_ROOT / 'plugins' / 'image-viewer' / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('image_viewer_recent_test', str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_image(path: Path, mtime: float, w: int = 64, h: int = 48) -> None:
    """生成一张指定修改时间的图片（mtime 是时间线排序的唯一依据）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (w, h), (200, 120, 40)).save(path)
    os.utime(path, (mtime, mtime))


def _representative(item):
    """单元的代表 url：作品卡用封面，单图用自身（排序与分页都按它定次序）。"""
    return item['cover'] if item.get('type') == 'album' else item['url']


class RecentImagesTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.module = _load_plugin_module()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _plugin(self, manga_root=None):
        """用本用例的临时目录建一个插件实例（相册索引因此从零开始）。

        `manga_root` 走入口的预设（`_resolved_config`），等价于漫画阅读插件配好的漫画根：
        用例不起另一个插件，但读书范围的组件与真实路径一致。
        """
        config = {'root_dir': str(self.root)}
        if manga_root is not None:
            config['manga_root'] = str(manga_root)
        self.module.ImageViewerPlugin._resolved_config = config
        return self.module.ImageViewerPlugin(
            {'name': 'image-viewer'},
            {'directories': {'data_root': str(self.root)}},
        )

    # ---------- 分组：一个作品一张卡 ----------

    def test_work_directory_becomes_one_album_unit(self):
        """作品目录里的 p0..pN 合成一张卡：封面 p0、页数角标、组内最新页的 mtime。"""
        base = 1_700_000_000.0
        for page in range(3):
            _make_image(self.root / 'pixiv' / 'artistA' / '800' / f'800_p{page}.png',
                        mtime=base + 10 + page)

        data = self._plugin().recent.list_recent_images(1, 40)

        self.assertEqual(len(data['items']), 1)
        card = data['items'][0]
        self.assertEqual(set(card),
                         {'type', 'path', 'name', 'cover', 'cover_mtime', 'image_count',
                          'total_count', 'has_children', 'mtime', 'width', 'height',
                          'use_time_name', 'pending'})
        self.assertEqual(card['type'], 'album')
        self.assertEqual(card['path'], 'pixiv/artistA/800')
        self.assertEqual(card['name'], '800')
        self.assertEqual(card['cover'], 'pixiv/artistA/800/800_p0.png')
        self.assertEqual(card['cover_mtime'], base + 10)      # 封面 p0 自身的 mtime
        self.assertEqual((card['image_count'], card['total_count']), (3, 3))
        self.assertFalse(card['has_children'])
        self.assertFalse(card['pending'])
        self.assertEqual(card['mtime'], base + 12)          # 组内最新页
        self.assertEqual((card['width'], card['height']), (64, 48))
        self.assertEqual([im['url'] for im in data['all_images']],
                         ['pixiv/artistA/800/800_p0.png',
                          'pixiv/artistA/800/800_p1.png',
                          'pixiv/artistA/800/800_p2.png'])
        self.assertEqual((data['total'], data['image_total']), (1, 3))
        self.assertEqual(data['scanned_dirs'], 4)            # 根 → pixiv → artistA → 800

    def test_cover_mtime_follows_replaced_cover_file(self):
        """替换封面文件后 `cover_mtime` 前进：前端据此换缩略图 URL 的版本号。"""
        base = 1_700_000_000.0
        for page in range(2):
            _make_image(self.root / 'works' / '800' / f'800_p{page}.png',
                        mtime=base + 10 + page)

        plugin = self._plugin()
        before = plugin.recent.list_recent_images(1, 40)['items'][0]
        self.assertEqual(before['cover'], 'works/800/800_p0.png')
        self.assertEqual(before['cover_mtime'], base + 10)

        replaced = base + 500
        os.utime(self.root / 'works' / '800' / '800_p0.png', (replaced, replaced))

        after = plugin.recent.list_recent_images(1, 40)['items'][0]
        self.assertEqual(after['cover'], before['cover'])
        self.assertEqual(after['cover_mtime'], replaced)
        self.assertGreater(after['cover_mtime'], before['cover_mtime'])

    def test_album_unit_without_cover_time_reports_zero(self):
        """取不到封面文件的 mtime 时给 0.0：前端回退用单元的 mtime。"""
        unit = {
            'type': 'album', 'path': 'x', 'name': 'x', 'mtime': 5.0,
            'use_time_name': False,
            'pages': [{'url': 'x/cover.png', 'name': 'cover.png',
                       'width': 1, 'height': 1}],
        }

        item = self.module._recent.RecentImages._public_unit(unit)

        self.assertEqual(item['cover_mtime'], 0.0)

    def test_flat_singles_stay_separate_image_units(self):
        """画师目录里平铺的单图作品各占一个单图单元，条目只有 image 的字段。"""
        base = 1_700_000_000.0
        _make_image(self.root / 'artistB' / '900.jpg', mtime=base + 20)
        _make_image(self.root / 'artistB' / '901.jpg', mtime=base + 21)

        data = self._plugin().recent.list_recent_images(1, 40)

        self.assertEqual([item['url'] for item in data['items']],
                         ['artistB/901.jpg', 'artistB/900.jpg'])
        for item in data['items']:
            self.assertEqual(item['type'], 'image')
            self.assertEqual(set(item),
                             {'type', 'url', 'name', 'mtime', 'width', 'height'})
        self.assertEqual((data['total'], data['image_total']), (2, 2))
        self.assertEqual([im['url'] for im in data['all_images']],
                         ['artistB/901.jpg', 'artistB/900.jpg'])

    def test_flat_pages_and_single_in_same_dir(self):
        """同一目录里平铺的 `800_p0/800_p1/900.jpg`：800 成组，900 仍是单图。"""
        base = 1_700_000_000.0
        _make_image(self.root / '800_p0.png', mtime=base + 10)
        _make_image(self.root / '800_p1.png', mtime=base + 11)
        _make_image(self.root / '900.jpg', mtime=base + 12)

        data = self._plugin().recent.list_recent_images(1, 40)

        self.assertEqual([_representative(item) for item in data['items']],
                         ['900.jpg', '800_p0.png'])
        card = data['items'][1]
        self.assertEqual(card['type'], 'album')
        self.assertEqual(card['path'], '')
        self.assertEqual(card['name'], '800')
        self.assertEqual(card['image_count'], 2)
        self.assertEqual([im['url'] for im in data['all_images']],
                         ['900.jpg', '800_p0.png', '800_p1.png'])
        self.assertEqual((data['total'], data['image_total']), (2, 3))

    def test_work_units_grouping_is_shared_with_scanner(self):
        """`work_units` 是扫描与时间线共用的分组实现：它的组数必须等于相册索引的单元数。"""
        naming = self.module._naming.PixivNaming()
        self.assertEqual(
            naming.work_units(['800_p0.png', '800_p1.png', '900.jpg',
                               '801_p0.png', 'z.png']),
            [['800_p0.png', '800_p1.png'], ['801_p0.png'], ['900.jpg'], ['z.png']])

        base = 1_700_000_000.0
        _make_image(self.root / 'w' / '800_p0.png', mtime=base + 1)
        _make_image(self.root / 'w' / '800_p1.png', mtime=base + 2)
        _make_image(self.root / 'w' / '801_p0.png', mtime=base + 3)   # 单页作品：仍算一个单元
        _make_image(self.root / 'w' / '900.jpg', mtime=base + 4)
        plugin = self._plugin()
        albums = {album['path']: album for album in plugin.albums.list_albums()['albums']}
        self.assertEqual((albums['w']['image_count'], albums['w']['unit_count']), (4, 3))
        self.assertEqual((albums['']['image_count'], albums['']['unit_count']), (4, 3))
        data = plugin.recent.list_recent_images(1, 40)
        self.assertEqual((data['total'], data['image_total']),
                         (albums['']['unit_count'], albums['']['image_count']))
        # 单页的 `801_p0` 不成作品卡：按单图返回
        kinds = {_representative(item): item['type'] for item in data['items']}
        self.assertEqual(kinds['w/801_p0.png'], 'image')
        self.assertEqual(kinds['w/800_p0.png'], 'album')

    # ---------- 漫画书：边界由漫画阅读插件的漫画根决定 ----------

    def test_manga_book_collapses_to_one_unit(self):
        """漫画根下的一级子目录是一本书：整本一张卡，标题取目录名、封面取第一页。"""
        base = 1_700_000_000.0
        manga = self.root / '本子'
        for index in range(5):
            _make_image(manga / '1479415' / f'{index + 1:05d}.jpg', mtime=base + index)

        plugin = self._plugin(manga)
        data = plugin.recent.list_recent_images(1, 40)

        self.assertEqual(len(data['items']), 1)
        card = data['items'][0]
        self.assertEqual(card['type'], 'album')
        self.assertEqual(card['path'], '本子/1479415')
        self.assertEqual(card['name'], '1479415')
        self.assertEqual(card['cover'], '本子/1479415/00001.jpg')
        self.assertEqual(card['cover_mtime'], base)        # 封面第一页自身的 mtime
        self.assertEqual((card['image_count'], card['total_count']), (5, 5))
        self.assertEqual(card['mtime'], base + 4)          # 组内最新页
        self.assertEqual([im['url'] for im in data['all_images']],
                         [f'本子/1479415/{index + 1:05d}.jpg' for index in range(5)])
        self.assertEqual((data['total'], data['image_total']), (1, 5))
        # 相册索引的单元数与时间线一致，并且书被标记出来
        albums = {album['path']: album for album in plugin.albums.list_albums()['albums']}
        self.assertTrue(albums['本子/1479415']['book'])
        self.assertEqual((albums['本子']['image_count'], albums['本子']['unit_count']), (5, 1))

    def test_manga_multichapter_book_is_one_unit(self):
        """多章本子整本一张卡：章按自然序拼接，章不会单独成为单元。"""
        base = 1_700_000_000.0
        manga = self.root / '本子'
        first = manga / '1479415' / '第1話 起'
        second = manga / '1479415' / '第2話 承'
        for index in range(3):
            _make_image(first / f'{index + 1:05d}.jpg', mtime=base + index)
        for index in range(2):
            _make_image(second / f'{index + 1:05d}.jpg', mtime=base + 10 + index)

        plugin = self._plugin(manga)
        data = plugin.recent.list_recent_images(1, 40)

        self.assertEqual(len(data['items']), 1)
        card = data['items'][0]
        self.assertEqual(card['image_count'], 5)
        self.assertEqual(card['cover_mtime'], base)   # 书封面 = 第一章第一页
        self.assertEqual([im['url'] for im in data['all_images']], [
            '本子/1479415/第1話 起/00001.jpg',
            '本子/1479415/第1話 起/00002.jpg',
            '本子/1479415/第1話 起/00003.jpg',
            '本子/1479415/第2話 承/00001.jpg',
            '本子/1479415/第2話 承/00002.jpg',
        ])
        self.assertEqual((data['total'], data['image_total']), (1, 5))
        albums = {album['path']: album for album in plugin.albums.list_albums()['albums']}
        self.assertEqual((albums['本子']['image_count'], albums['本子']['unit_count']), (5, 1))

    def test_manga_book_cover_and_title_follow_manga_plugin(self):
        """封面优先名字含 cover 的图、标题取 album_info.json，且封面排在序列首位。"""
        base = 1_700_000_000.0
        book = self.root / '本子' / '1479415'
        for index in range(3):
            _make_image(book / f'{index + 1:05d}.jpg', mtime=base + index)
        _make_image(book / 'cover.jpg', mtime=base + 100)
        (book / 'album_info.json').write_text(
            json.dumps({'title': '标题不是目录名的书'}, ensure_ascii=False), encoding='utf-8')

        plugin = self._plugin(self.root / '本子')
        data = plugin.recent.list_recent_images(1, 40)

        card = data['items'][0]
        self.assertEqual(card['name'], '标题不是目录名的书')
        self.assertEqual(card['cover'], '本子/1479415/cover.jpg')
        self.assertEqual(card['cover_mtime'], base + 100)  # 书封面文件自身的 mtime
        self.assertEqual(card['mtime'], base + 100)
        # 封面恒在序列首位：前端按"前面各单元张数累加"定位灯箱起点，否则会打开到别的页
        self.assertEqual(data['all_images'][0]['url'], '本子/1479415/cover.jpg')
        self.assertEqual(len(data['all_images']), 4)

    def test_numbered_pages_outside_manga_root_stay_separate(self):
        """没有漫画根时数字命名不合并：一份 00001 起连号的目录仍是逐张图片。

        时间线不靠文件名猜「这是不是一本书」；书只由漫画插件的漫画根界定（`books.py`）。
        """
        base = 1_700_000_000.0
        for index in range(4):
            _make_image(self.root / '普通目录' / f'{index + 1:05d}.jpg', mtime=base + index)

        plugin = self._plugin()
        data = plugin.recent.list_recent_images(1, 40)

        self.assertEqual(len(data['items']), 4)
        self.assertTrue(all(item['type'] == 'image' for item in data['items']))
        self.assertEqual((data['total'], data['image_total']), (4, 4))

    def test_manga_root_outside_image_roots_is_ignored(self):
        """漫画根不在图片根里时整体退化：这些页面本来也不该按书合并。"""
        base = 1_700_000_000.0
        for index in range(3):
            _make_image(self.root / '本子' / '1479415' / f'{index + 1:05d}.jpg',
                        mtime=base + index)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)

        plugin = self._plugin(Path(outside.name))
        data = plugin.recent.list_recent_images(1, 40)

        self.assertEqual(len(data['items']), 3)
        self.assertTrue(all(item['type'] == 'image' for item in data['items']))
        albums = {album['path']: album for album in plugin.albums.list_albums()['albums']}
        self.assertFalse(albums['本子/1479415']['book'])

    # ---------- 排序与次序 ----------

    def test_cross_directory_units_are_mtime_desc(self):
        """单元 mtime 倒序跨目录生效，而不是「先目录、再目录内」的拼接顺序。"""
        base = 1_700_000_000.0
        _make_image(self.root / 'a' / 'old.png', mtime=base + 10)
        _make_image(self.root / 'b' / 'new.png', mtime=base + 30)
        _make_image(self.root / 'c' / 'mid.png', mtime=base + 20)
        _make_image(self.root / 'single.png', mtime=base + 40)

        data = self._plugin().recent.list_recent_images(1, 40)

        self.assertEqual([_representative(item) for item in data['items']],
                         ['single.png', 'b/new.png', 'c/mid.png', 'a/old.png'])
        self.assertEqual((data['total'], data['image_total']), (4, 4))

    def test_same_mtime_breaks_ties_by_representative_url(self):
        """mtime 相同时按代表 url 升序：作品用封面参与比较，单图用自身。"""
        base = 1_700_000_000.0
        for name in ['b.png', 'a.png', 'c.png']:
            _make_image(self.root / 'dirB' / name, mtime=base + 5)
        for name in ['b.png', 'a.png']:
            _make_image(self.root / 'dirA' / name, mtime=base + 5)
        _make_image(self.root / 'dirC' / '800_p0.png', mtime=base + 5)
        _make_image(self.root / 'dirC' / '800_p1.png', mtime=base + 5)

        data = self._plugin().recent.list_recent_images(1, 40)

        reps = [_representative(item) for item in data['items']]
        self.assertEqual(reps, ['dirA/a.png', 'dirA/b.png', 'dirB/a.png',
                                'dirB/b.png', 'dirB/c.png', 'dirC/800_p0.png'])

    def test_tied_mtime_unit_pagination_is_stable(self):
        """全部 mtime 相同时，容量为 K 的堆内淘汰也按代表 url：前 N 个单元即 url 最小的 N 个。"""
        base = 1_700_000_000.0
        expected = []
        for index in range(25):
            rel = f'd{index % 5}/f{index:02d}.png'
            _make_image(self.root / rel, mtime=base)
            expected.append(Path(rel).as_posix())
        expected.sort()

        plugin = self._plugin()
        collected = []
        for page in (1, 2, 3):
            data = plugin.recent.list_recent_images(page, 10)
            collected.extend(_representative(item) for item in data['items'])

        self.assertEqual(collected, expected)

    # ---------- 分页 ----------

    def test_unit_pagination_matches_total_without_duplicates(self):
        """按单元分页：拼接等于全部单元次序，total 是单元数、image_total 是图片数。"""
        base = 1_700_000_000.0
        mtimes = {}
        for work in range(4):
            number = 800 + work * 100
            for page in range(3):
                _make_image(self.root / 'works' / f'w{work}' / f'{number}_p{page}.png',
                            mtime=base + work * 100 + page)
            mtimes[f'works/w{work}/{number}_p0.png'] = base + work * 100 + 2
        for index in range(8):
            _make_image(self.root / 'singles' / f's{index}.png',
                        mtime=base + 1000 + index * 10)
            mtimes[f'singles/s{index}.png'] = base + 1000 + index * 10
        expected = sorted(mtimes, key=lambda url: (-mtimes[url], url))

        plugin = self._plugin()
        collected = []
        page = 1
        while True:
            data = plugin.recent.list_recent_images(page, 5)
            collected.extend(_representative(item) for item in data['items'])
            self.assertEqual((data['total'], data['image_total']), (12, 20))
            self.assertEqual(data['has_prev'], page > 1)
            if not data['has_next']:
                break
            page += 1
            self.assertLessEqual(page, 10, '分页没有收敛：has_next 永远为真')

        self.assertEqual(page, 3)
        self.assertEqual(collected, expected)
        self.assertEqual(len(set(collected)), 12)

    def test_item_cover_aligns_with_all_images(self):
        """第 i 个单元的封面在 all_images 的下标 = 前面各单元 image_count / 1 的累加。"""
        base = 1_700_000_000.0
        _make_image(self.root / 'a' / '800_p0.png', mtime=base + 29)
        _make_image(self.root / 'a' / '800_p1.png', mtime=base + 30)
        _make_image(self.root / 'b' / 'b.png', mtime=base + 25)
        _make_image(self.root / 'c' / '900_p0.png', mtime=base + 18)
        _make_image(self.root / 'c' / '900_p1.png', mtime=base + 19)
        _make_image(self.root / 'c' / '900_p2.png', mtime=base + 20)
        _make_image(self.root / 'd' / 'd.png', mtime=base + 10)

        data = self._plugin().recent.list_recent_images(1, 40)

        self.assertEqual(len(data['items']), 4)
        cursor = 0
        for item in data['items']:
            self.assertEqual(data['all_images'][cursor]['url'], _representative(item))
            cursor += item['image_count'] if item['type'] == 'album' else 1
        self.assertEqual(cursor, len(data['all_images']))
        self.assertEqual(len(data['all_images']), data['image_total'])

    # ---------- 剪枝、空库、夹取、预算 ----------

    def test_older_subtrees_are_not_scanned(self):
        """更早的子树整棵不会被扫：第一页只走"根 + 最近目录"两个目录。

        构造 1 个最近目录（20 个作品、各 2 页 = 40 张）+ 10 个老目录（各 2 张）：相册树
        共 12 个目录，而第一页的 `scanned_dirs` 必须远小于它。退化成"扫全树再排序"时
        这个断言会失败（等于 12），老目录里的单元也会出现在结果里。
        """
        base = 1_700_000_000.0
        for work in range(20):
            for page in range(2):
                _make_image(self.root / 'recent' / f'{8000 + work}_p{page}.png',
                            mtime=base + 1000 + work * 2 + page)
        for dir_index in range(10):
            for file_index in range(2):
                _make_image(self.root / f'old{dir_index}' / f'o{file_index}.png',
                            mtime=base + dir_index * 10 + file_index)

        plugin = self._plugin()
        data = plugin.recent.list_recent_images(1, 20)

        dir_count = len(plugin.albums.list_albums()['albums'])
        self.assertEqual(dir_count, 12)
        # 20 个作品单元 + 10 个老目录 × 2 张单图
        self.assertEqual((data['total'], data['image_total']), (40, 60))
        self.assertTrue(data['has_next'])
        self.assertEqual(len(data['items']), 20)
        self.assertTrue(all(item['type'] == 'album' for item in data['items']))
        self.assertTrue(all(item['path'] == 'recent' for item in data['items']))
        self.assertTrue(all(not _representative(item).startswith('old')
                            for item in data['items']), '第一页里出现了老目录的单元')
        self.assertFalse(data['partial'])
        self.assertLessEqual(data['scanned_dirs'], 3)
        self.assertLess(data['scanned_dirs'], dir_count)

    def test_empty_library(self):
        """空库：空列表、两个总数都是 0，且不展开任何目录。"""
        data = self._plugin().recent.list_recent_images()

        self.assertEqual(data['items'], [])
        self.assertEqual(data['all_images'], [])
        self.assertEqual((data['total'], data['image_total']), (0, 0))
        self.assertFalse(data['has_next'])
        self.assertEqual(data['scanned_dirs'], 0)
        self.assertFalse(data['partial'])

    def test_page_and_per_page_are_clamped(self):
        """page 至少 1；per_page 夹到 [1, 200]。"""
        base = 1_700_000_000.0
        for index, name in enumerate(['a.png', 'b.png', 'c.png']):
            _make_image(self.root / 'd' / name, mtime=base + index)

        plugin = self._plugin()
        low = plugin.recent.list_recent_images(0, 0)
        self.assertEqual((low['page'], low['per_page']), (1, 1))
        self.assertEqual(len(low['items']), 1)
        self.assertEqual(low['items'][0]['url'], 'd/c.png')

        high = plugin.recent.list_recent_images(1, 1000)
        self.assertEqual(high['per_page'], 200)
        self.assertEqual(len(high['items']), 3)

    def test_namespace_node_expands_children_only(self):
        """额外根的命名空间节点只展开子目录：它的直接图片不计入相册树，也不出现在这里。"""
        base = 1_700_000_000.0
        main_root = self.root / '主图库'
        extra = self.root / '额外图库'
        _make_image(main_root / 'a.png', mtime=base + 5)
        _make_image(extra / '作者' / 'b.png', mtime=base + 10)
        # 额外根顶层的直接图片：相册树把命名空间节点算作纯容器，不计入递归单元数
        _make_image(extra / 'c.png', mtime=base + 20)
        self.module.ImageViewerPlugin._resolved_config = {
            'root_dir': str(main_root), 'extra_roots': str(extra)}
        plugin = self.module.ImageViewerPlugin(
            {'name': 'image-viewer'},
            {'directories': {'data_root': str(main_root)}},
        )

        data = plugin.recent.list_recent_images(1, 40)

        self.assertEqual([item['url'] for item in data['items']],
                         ['__额外图库/作者/b.png', 'a.png'])
        self.assertEqual((data['total'], data['image_total']), (2, 2))
        paths = [album['path'] for album in plugin.albums.list_albums()['albums']]
        self.assertIn('__额外图库/作者', paths)

    def test_partial_page_that_is_full_still_offers_next(self):
        """预算用尽但本页被填满时仍给"下一页"：`partial` 不等于列表到边界。

        排序依据不完整（未展开的子树里可能有更靠前的单元）由 `partial` 告知；能不能往
        后翻只看这一页有没有被填满（`reach >= 页末`）。
        """
        base = 1_700_000_000.0
        # mtime 全相同：剪枝判据（剩余上界 < 第 K 名）在这里不生效，预算成为截断原因
        for index in range(6):
            _make_image(self.root / f'd{index}' / 'x.png', mtime=base)

        plugin = self._plugin()
        tight = types.SimpleNamespace(max_recent_dirs=3)
        recent = self.module._recent.RecentImages(
            plugin.paths, plugin.albums, plugin.thumbs, plugin.naming,
            self.module._const.IMAGE_EXTENSIONS, tight, plugin.books)

        data = recent.list_recent_images(1, 2)

        self.assertTrue(data['partial'])
        self.assertEqual(len(data['items']), 2)     # 根占一次，之后还能展开两个目录
        self.assertEqual(data['reach'], 2)
        self.assertTrue(data['has_next'], '本页填满了，应该还能往后翻')

        # 再往前时预算不足以填满这一页：列表到边界，不再给"下一页"
        far = recent.list_recent_images(2, 2)
        self.assertGreater(far['scanned_dirs'], 0)
        self.assertFalse(far['has_next'])

    def test_scan_budget_exhausted_marks_partial(self):
        """扫描预算用尽时 `partial=True`：`total` 仍是整库单元数，`reach` 是本次边界。

        前端据此只保留能打开的页码并提示"仅列出最近 N 项"；同一预算下更深的页必然为空，
        因此 `has_next` 在 partial 时为假。
        """
        base = 1_700_000_000.0
        for index in range(3):
            _make_image(self.root / f'd{index}' / 'x.png', mtime=base + index)

        plugin = self._plugin()
        tight = types.SimpleNamespace(max_recent_dirs=3)
        recent = self.module._recent.RecentImages(
            plugin.paths, plugin.albums, plugin.thumbs, plugin.naming,
            self.module._const.IMAGE_EXTENSIONS, tight, plugin.books)

        data = recent.list_recent_images(1, 40)

        self.assertTrue(data['partial'])
        self.assertEqual(data['scanned_dirs'], 3)
        self.assertEqual(data['total'], 3)          # 整库单元数照实报
        self.assertEqual(data['reach'], 2)          # 预算内排列出的单元数
        self.assertEqual(data['image_total'], 3)    # 整库图片数不受预算影响
        self.assertFalse(data['has_next'], '预算用尽后不该再宣称有下一页')
        self.assertEqual([_representative(item) for item in data['items']],
                         ['d2/x.png', 'd1/x.png'])
        # 更深的分页在同样预算下是空页，但 total / reach 仍然自洽
        deeper = recent.list_recent_images(2, 40)
        self.assertEqual(deeper['items'], [])
        self.assertEqual((deeper['total'], deeper['reach']), (3, 2))
        self.assertFalse(deeper['has_next'])
        self.assertTrue(deeper['partial'])


if __name__ == '__main__':
    unittest.main()
