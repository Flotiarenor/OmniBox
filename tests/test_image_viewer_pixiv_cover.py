"""image-viewer 画师封面与作者视图二次排序的回归用例。

三件事在这里被固化：

1. **画师封面取最近的作品**：pixiv-sync 把作品平铺在画师目录下
   （`<pid>.jpg` / `<pid>_p0.jpg`）。旧规则"封面 = 文件名自然序第一张"等于
   永远展示该画师最老的图（pid 最小 / 最先下载的那张），实际想看的是最近风格；
   开启 Pixiv 排序后改为取**作品号最大**的那张（同一作品内仍取 p0），
   非 Pixiv 目录保持原行为不变。
2. **作者网格的二次排序持久化**：原先只存在于显示页顶部排序栏的 localStorage，
   设置页没有入口；现在落库到 `album_sort_by` / `album_sort_order`，
   默认"更新时间 / 倒序"，设置页与显示页排序栏共用同一份值。
3. **全局设置能读回**：`save_folder_settings('')` 走 PluginBase.save_settings()，
   写的是插件级键，而 get_settings 原先只读 `folders['__global__']`
   （全仓没有写入方），于是"保存到全局"重启后静默回落到硬默认值。

运行：
    python -m unittest tests.test_image_viewer_pixiv_cover -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PIL import Image

from shell.backend.settings_store import SettingsStore


def _load_plugin_module():
    main_path = PROJECT_ROOT / 'plugins' / 'image-viewer' / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('image_viewer_pixiv_test', str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_image(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (12, 12), (180, 140, 60)).save(path)


class ImageViewerPixivCoverTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / 'data'
        cls.root.mkdir(parents=True)

        # 画师目录：平铺 <pid>.jpg / <pid>_pN.jpg（pixiv-sync 的落盘规则）
        for name in ['100_p0.png', '100_p1.png', '500_p0.png', '500_p1.png', '900.jpg']:
            _make_image(cls.root / 'pixiv' / 'artistA' / name)
        # 嵌套结构：画师目录下按作品分子文件夹
        for pid in (200, 800):
            _make_image(cls.root / 'pixiv' / 'artistB' / f'{pid}_title' / f'{pid}_p0.png')
            _make_image(cls.root / 'pixiv' / 'artistB' / f'{pid}_title' / f'{pid}_p1.png')
        # 普通相册：文件名自然序第一张
        _make_image(cls.root / 'albumA' / 'b.png')
        _make_image(cls.root / 'albumA' / 'a.png')

        cls.module = _load_plugin_module()
        cls.manifest = {'name': 'image-viewer'}
        cls.config = {'directories': {'data_root': str(cls.root)}}

    def setUp(self):
        # 每个用例一份干净的设置存储：文件夹级设置会按路径继承，
        # 共用一个存储会让"某个用例保存过 pixiv/time_name"污染后面的封面断言
        self._settings_tmp = tempfile.TemporaryDirectory()
        self.settings_dir = Path(self._settings_tmp.name)

    def tearDown(self):
        self._settings_tmp.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _plugin(self, config: dict, with_store: bool = True):
        module = self.module
        original = getattr(module.ImageViewerPlugin, '_resolved_config', None)
        module.ImageViewerPlugin._resolved_config = config
        try:
            plugin = module.ImageViewerPlugin(self.manifest, self.config)
        finally:
            module.ImageViewerPlugin._resolved_config = original
        if with_store:
            plugin._settings_store = SettingsStore(str(self.settings_dir))
        return plugin

    def _covers(self, config: dict) -> dict:
        albums = self._plugin(config).list_albums()['albums']
        return {a['path']: a['cover'] for a in albums}

    # ---------- 封面 ----------

    def test_pixiv_cover_is_newest_work(self):
        """开启 Pixiv 排序：平铺目录取作品号最大的一张，嵌套目录取最新作品。"""
        covers = self._covers({'root_dir': str(self.root), 'sort_by': 'time_name'})
        self.assertEqual(covers['pixiv/artistA'], 'pixiv/artistA/900.jpg')
        self.assertEqual(covers['pixiv/artistB'], 'pixiv/artistB/800_title/800_p0.png')

    def test_pixiv_cover_same_work_prefers_p0(self):
        """同一作品（同 pid）内仍取 p0，而不是 p1 / p10。"""
        covers = self._covers({'root_dir': str(self.root), 'sort_by': 'time_name'})
        # artistA 的 900.jpg 是单图；换一张多图作品验证同号取 p0
        self.assertEqual(covers['pixiv/artistB'], 'pixiv/artistB/800_title/800_p0.png')
        pick = self.module._pick_cover(
            [{'rel': '500_p10.png'}, {'rel': '500_p0.png'}, {'rel': '500_p1.png'},
             {'rel': '100_p0.png'}],
            True)
        self.assertEqual(pick, '500_p0.png')

    def test_non_pixiv_cover_unchanged(self):
        """未开启 Pixiv 排序的目录：封面仍是文件名自然序第一张。"""
        covers = self._covers({'root_dir': str(self.root)})
        self.assertEqual(covers['pixiv/artistA'], 'pixiv/artistA/100_p0.png')
        self.assertEqual(covers['albumA'], 'albumA/a.png')

    def test_pixiv_cover_ignores_mtime_of_redownloaded_old_work(self):
        """老作品被重新下载（目录 mtime 更新）也不能抢走封面：Pixiv 树按作品号。"""
        artist = self.root / 'pixiv' / 'artistC'
        for pid in (300, 700):
            _make_image(artist / f'{pid}_title' / f'{pid}_p0.png')
        # 老作品 300 的文件被重新下载/改写（mtime 最新），新作品 700 的文件更旧
        old_repost, older = 1_800_000_000.0, 1_700_000_000.0
        for path in (artist / '300_title').iterdir():
            os.utime(path, (old_repost, old_repost))
        for path in (artist / '700_title').iterdir():
            os.utime(path, (older, older))
        try:
            covered = {'root_dir': str(self.root), 'sort_by': 'time_name'}
            self.assertEqual(self._covers(covered)['pixiv/artistC'],
                             'pixiv/artistC/700_title/700_p0.png')
            # 非 Pixiv 目录仍按 mtime 最新者取封面（保持原行为）
            self.assertEqual(self._covers({'root_dir': str(self.root)})['pixiv/artistC'],
                             'pixiv/artistC/300_title/300_p0.png')
        finally:
            import shutil
            shutil.rmtree(artist, ignore_errors=True)

    def test_cover_recomputed_after_toggling_pixiv(self):
        """切换 Pixiv 排序后旧封面必须重算：索引缓存按目录 mtime 复用，
        只认 mtime 会让封面永远停在切换前的规则。"""
        data_root = Path(self._tmp.name) / 'data_toggle'
        (data_root / 'pixiv' / 'artistA').mkdir(parents=True)
        for name in ['100_p0.png', '100_p1.png', '900.jpg']:
            _make_image(data_root / 'pixiv' / 'artistA' / name)
        config = {'root_dir': str(data_root), 'sort_by': 'time_name'}

        def covers(cfg):
            albums = self._plugin(cfg).list_albums()['albums']
            return {a['path']: a['cover'] for a in albums}

        # 先按普通相册扫一遍并落盘索引，再切到 Pixiv 排序（目录 mtime 未变）
        self.assertEqual(covers({'root_dir': str(data_root)})['pixiv/artistA'],
                         'pixiv/artistA/100_p0.png')
        self.assertEqual(covers(config)['pixiv/artistA'], 'pixiv/artistA/900.jpg')

    def test_album_cache_version(self):
        plugin = self._plugin({'root_dir': str(self.root)})
        plugin.list_albums()
        self.assertEqual(plugin._album_cache.get('version'),
                         self.module.ImageViewerPlugin._ALBUM_CACHE_VERSION)

    def test_old_album_cache_version_invalidated(self):
        """带旧封面规则（version 3：自然序第一张）的索引缓存必须作废重扫。"""
        data_root = Path(self._tmp.name) / 'data_stale'
        _make_image(data_root / 'pixiv' / 'artistA' / '100_p0.png')
        _make_image(data_root / 'pixiv' / 'artistA' / '900.jpg')
        cache_file = data_root / '.cache' / 'albums_index.json'
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            'version': 3,
            'dirs': {'pixiv/artistA': {
                'path': 'pixiv/artistA', 'name': 'artistA', 'depth': 2, 'parent': 'pixiv',
                'direct_count': 2, 'direct_cover': 'pixiv/artistA/100_p0.png',
                'direct_mtime': 1.0, 'mtime': 1.0,
                'has_children': False, 'children': [],
            }},
        }), encoding='utf-8')
        albums = self._plugin({'root_dir': str(data_root), 'sort_by': 'time_name'}).list_albums()['albums']
        self.assertEqual(next(a for a in albums if a['path'] == 'pixiv/artistA')['cover'],
                         'pixiv/artistA/900.jpg')

    # ---------- 作者视图二次排序设置 ----------

    def test_album_sort_defaults(self):
        s = self._plugin({'root_dir': str(self.root)}).get_settings('')
        self.assertEqual(s['album_sort_by'], 'mtime')
        self.assertEqual(s['album_sort_order'], 'desc')

    def test_album_sort_settings_persist_globally(self):
        """设置页保存的二次排序必须落库并在"重启"（新实例）后读回。"""
        plugin = self._plugin({'root_dir': str(self.root)})
        result = plugin.save_folder_settings('', {
            'album_sort_by': 'count',
            'album_sort_order': 'asc',
        })
        self.assertTrue(result.get('success'))
        self.assertEqual(plugin.get_settings('')['album_sort_by'], 'count')
        # 新实例 = 重启后重新加载，仍能读到（历史缺陷：全局保存写插件级键但读不回来）
        reopened = self._plugin({'root_dir': str(self.root)})
        s = reopened.get_settings('')
        self.assertEqual(s['album_sort_by'], 'count')
        self.assertEqual(s['album_sort_order'], 'asc')

    def test_album_sort_is_not_stored_per_folder(self):
        """二次排序是全局偏好，不应出现在文件夹级设置里。"""
        plugin = self._plugin({'root_dir': str(self.root)})
        plugin.save_folder_settings('pixiv', {'sort_by': 'time_name'})
        folders = plugin._settings_store.get('image-viewer').get('folders', {})
        self.assertEqual(folders.get('pixiv'), {'sort_by': 'time_name'})

    def test_settings_schema_declares_album_sort(self):
        schema = {f['key']: f for f in self.module.ImageViewerPlugin.settings_schema}
        self.assertEqual(schema['album_sort_by']['default'], 'mtime')
        self.assertEqual(schema['album_sort_order']['default'], 'desc')
        values = {o['value'] for o in schema['album_sort_by']['options']}
        self.assertEqual(values, {'mtime', 'name', 'count'})


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
