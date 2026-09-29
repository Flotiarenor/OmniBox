"""漫画库入架门槛：目录里没有阅读器读得到的图片就不进书架。

背景：`scan_manga` 原先只看目录名（跳过隐藏目录与 `ai`），于是漫画根下任何一级
子目录都被当成一部漫画。漫画根未配置时回落全局数据根（`directories.data_root`），
那里放着插件自己的数据目录（如 `<数据根>/group-mesh`，group-mesh 插件 `on_load`
无条件创建，可以是空的），表现在书架上就是一张 0 页、无封面的空白卡片。

这里守的是新口径：**必须有一张阅读器读得到的图片**（漫画目录里直接有，或一级
章节子目录里有；更深的层级 `find_cover` / `list_pages` 都看不到，因此不算）。
"""

import shutil
import sys
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.plugin_utils import load_sibling

SCANNER_PATH = PROJECT_ROOT / 'plugins' / 'manga-library' / 'backend' / 'scanner.py'
scanner = load_sibling(str(SCANNER_PATH), 'scanner', 'manga_library_scanner_test')

# 测试数据放工作区内（沙箱/CI 下系统临时目录可能不可写，同 tests/debug_media_scan_task.py）。
# 目录名自己拼而不用 tempfile.mkdtemp：mkdtemp 建出的是 mode 0700 目录，本机沙箱下
# 连子目录都建不进去（WinError 5）。
TMP_BASE = PROJECT_ROOT / '.build' / 'manga-scan-test'


class _TempRootTestCase(unittest.TestCase):
    def setUp(self):
        TMP_BASE.mkdir(parents=True, exist_ok=True)
        self.root = TMP_BASE / f'run-{uuid.uuid4().hex}'
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _touch(path: Path, name: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_bytes(b'x')
    return target


def _folder_names(manga_dir: Path) -> list:
    return sorted(item['folder_name'] for item in scanner.scan_manga(manga_dir, []))


class ScanMangaImageGateTest(_TempRootTestCase):
    def test_empty_plugin_data_dir_is_not_listed(self):
        """空目录（group-mesh 数据目录那种）不进书架。"""
        (self.root / 'group-mesh').mkdir()
        _touch(self.root / '123456', '1.jpg')

        self.assertEqual(_folder_names(self.root), ['123456'])

    def test_single_chapter_manga_is_listed_with_cover(self):
        _touch(self.root / 'single', '1.jpg')
        _touch(self.root / 'single', '2.jpg')

        result = scanner.scan_manga(self.root, [])
        self.assertEqual([item['folder_name'] for item in result], ['single'])
        self.assertEqual(result[0]['cover_url'], 'single/1.jpg')

    def test_multi_chapter_manga_with_only_empty_first_chapter_is_listed(self):
        """第一章是空目录、第二章有图：仍然入架（入架判定看全部章节）。"""
        (self.root / 'multi' / '01').mkdir(parents=True)
        _touch(self.root / 'multi' / '02', '1.jpg')

        self.assertEqual(_folder_names(self.root), ['multi'])

    def test_folder_with_subdirs_but_no_images_is_not_listed(self):
        (self.root / 'mixed' / '01').mkdir(parents=True)
        (self.root / 'mixed' / '02').mkdir(parents=True)
        (self.root / 'mixed' / 'notes.txt').write_text('x', encoding='utf-8')

        self.assertEqual(_folder_names(self.root), [])

    def test_images_only_in_hidden_or_ai_subdirs_are_not_enough(self):
        """.cache / ai 里的图片不算：阅读器读不到（与跳过这两个目录同一条规则）。"""
        _touch(self.root / 'cached' / '.cache', '1.jpg')
        _touch(self.root / 'ai-copy' / 'ai', '1.jpg')

        self.assertEqual(_folder_names(self.root), [])

    def test_images_deeper_than_one_level_are_not_enough(self):
        """深于一级的图片阅读器读不到，按「没有图片」处理，不进书架。"""
        _touch(self.root / 'deep' / 'vol1' / 'ch1', '1.jpg')

        self.assertEqual(_folder_names(self.root), [])

    def test_hidden_dir_and_ai_dir_are_still_skipped(self):
        _touch(self.root / '.cache', '1.jpg')
        _touch(self.root / 'ai', '1.jpg')
        _touch(self.root / 'normal', '1.jpg')

        self.assertEqual(_folder_names(self.root), ['normal'])

    def test_image_extensions_are_case_insensitive_and_bounded(self):
        _touch(self.root / 'upper', 'A.JPEG')
        _touch(self.root / 'gif-only', '1.gif')

        self.assertEqual(_folder_names(self.root), ['upper'])

    def test_favorites_of_filtered_folders_stay_out(self):
        (self.root / 'ghost').mkdir()
        _touch(self.root / 'real', '1.jpg')

        result = scanner.scan_manga(self.root, ['ghost', 'real'])
        self.assertEqual([item['folder_name'] for item in result], ['real'])
        self.assertTrue(result[0]['is_fav'])

    def test_missing_root_returns_empty_list(self):
        self.assertEqual(scanner.scan_manga(self.root / 'nope', []), [])


class HasImagesUnitTest(_TempRootTestCase):
    def test_direct_images(self):
        _touch(self.root / 'comic', '1.png')
        self.assertTrue(scanner.has_images(self.root / 'comic'))

    def test_chapter_images(self):
        _touch(self.root / 'comic' / 'ch1', '1.webp')
        self.assertTrue(scanner.has_images(self.root / 'comic'))

    def test_empty_folder(self):
        (self.root / 'comic').mkdir()
        self.assertFalse(scanner.has_images(self.root / 'comic'))

    def test_missing_folder_is_treated_as_empty(self):
        self.assertFalse(scanner.has_images(self.root / 'missing'))


if __name__ == '__main__':
    unittest.main()
