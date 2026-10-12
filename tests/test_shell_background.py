"""壳的背景图库：导入 / 列举 / 删除，以及 `/file` 的壳级图片根。

背景图在选中时被复制进 `<config_dir>/backgrounds`，因此要守住的是：
  1. 复制与删除只发生在图片库目录里 —— 删除端点不能成为"删除本机任意文件"的入口；
  2. `/file` 无 plugin 时只多放行这一个目录，且仍然只放行图片；
  3. 插件请求不受影响：壳的壁纸库不会顺带变成所有插件的允许根。

运行：
    python -m unittest tests.test_shell_background -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import create_app
from shell.backend.paths import get_config_dir, get_plugins_config_dir
from shell.backend.plugin_manager import collect_protected_paths
from shell.backend.shell_info import ShellInfo, _safe_image_name, background_library_dir


class _StubInstance:
    """最小插件实例：文件路由只用这几个成员。"""

    def __init__(self, root):
        self._root = Path(root)

    def get_file_roots(self):
        return [self._root]

    def get_data_root(self):
        return self._root

    def get_protected_paths(self):
        return []

    @property
    def thumb_dir(self):
        return self._root / '.cache' / 'thumbs'

    def get_thumb_data(self, rel_path):
        return None

    def ensure_thumb(self, rel_path):
        return None

    def ensure_file(self, path):
        return


class _StubPluginManager:
    def __init__(self):
        self.instances = {}

    def get_api_methods(self):
        return {}

    def freshness_api_methods(self):
        return {}

    def get_frontend_manifests(self):
        return []

    def get_plugin_extensions(self):
        return {}

    def get_plugin_status(self):
        return {'loaded': [], 'failures': []}

    def get_plugin_instance(self, name):
        return self.instances.get(name)

    def get_plugin_dir(self, name):
        return None

    def get_protected_paths(self):
        """与真实 PluginManager 共用同一份聚合 + 边界校验实现。

        写死 `return []` 会让"插件申报的受保护路径必须生效"这类用例静默变绿，
        也会在每条文件请求上留一条 warning。
        """
        return collect_protected_paths(self.instances, get_plugins_config_dir())


class ShellBackgroundLibraryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.data_root = root / 'data'
        self.data_root.mkdir()
        self.gallery = root / 'gallery'
        self.gallery.mkdir()
        self.photo = self.gallery / 'wall.jpg'
        self.photo.write_bytes(b'JPEGDATA')
        (self.gallery / 'note.txt').write_text('not an image', encoding='utf-8')
        self.outside = root / 'outside.jpg'
        self.outside.write_bytes(b'OUTSIDE')
        # 配置目录同样要隔离：图片库就在它下面，不隔离就会往开发机的真实 .config 里
        # 复制图片。
        self.config_dir = root / 'config'
        self.config_dir.mkdir()
        self.library = background_library_dir(str(self.config_dir))

        self.config = {
            'server': {'host': '127.0.0.1', 'port': 18080},
            'directories': {'data_root': str(self.data_root)},
        }
        self.manager = _StubPluginManager()
        app = create_app(self.config, self.manager)
        self.client = app.test_client()
        self.headers = {TOKEN_HEADER: get_or_create_token(get_config_dir())}
        self.shell_info = ShellInfo(self.config, str(self.config_dir))
        # 请求期的 `get_config_dir` 同样指向本用例的配置目录：`/file` 每次请求都要
        # 拼出图片库路径，不隔离就会去读开发机的真实配置。鉴权不受影响 —— 主体表在
        # `create_app` 时就用真实配置目录建好了。
        patcher = mock.patch('shell.backend.file_server.get_config_dir',
                             return_value=self.config_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get(self, url):
        resp = self.client.get(url, headers=self.headers)
        self.addCleanup(resp.close)
        return resp

    def _import(self, source=None):
        result = self.shell_info.import_background_image(str(source or self.photo))
        self.assertTrue(result['success'], result)
        return result['image']

    # ===== 导入 =====
    def test_import_copies_into_library(self):
        image = self._import()
        self.assertEqual(image['name'], 'wall.jpg')
        self.assertEqual(image['path'], str(self.library / 'wall.jpg'))
        self.assertEqual(Path(image['path']).read_bytes(), b'JPEGDATA')
        # 原图留在原处：库里是副本，删除库内条目不影响用户自己的文件
        self.assertEqual(self.photo.read_bytes(), b'JPEGDATA')

    def test_import_rejects_non_image(self):
        result = self.shell_info.import_background_image(str(self.gallery / 'note.txt'))
        self.assertFalse(result['success'])
        self.assertIn('不是图片', result['error'])
        self.assertFalse(self.library.exists())

    def test_import_rejects_missing_file_and_relative_path(self):
        missing = self.shell_info.import_background_image(str(self.gallery / 'nope.jpg'))
        relative = self.shell_info.import_background_image('wall.jpg')
        malformed = self.shell_info.import_background_image(123)
        self.assertIn('不存在', missing['error'])
        self.assertIn('绝对路径', relative['error'])
        self.assertFalse(malformed['success'])

    def test_import_keeps_same_name_apart(self):
        """同名图片不覆盖也不复用：第二张落成 `wall-2.jpg`。"""
        other_dir = Path(self._tmp.name) / 'other'
        other_dir.mkdir()
        other = other_dir / 'wall.jpg'
        other.write_bytes(b'OTHER')
        first = self._import()
        second = self._import(other)
        self.assertEqual(first['name'], 'wall.jpg')
        self.assertEqual(second['name'], 'wall-2.jpg')
        self.assertEqual(Path(second['path']).read_bytes(), b'OTHER')

    def test_safe_image_name_strips_reserved_chars(self):
        """库内文件名不含路径分隔符与 Windows 保留字符。

        `Path('a:b?c.jpg')` 在两种平台上都建不出文件，因此直接验证收敛函数，
        并只断言"不含保留字符"，不断言平台各自会怎么切分盘符。
        """
        cleaned = _safe_image_name('a:b?c.jpg')
        self.assertNotRegex(cleaned, r'[<>:"/\\|?*\x00-\x1f]')
        self.assertTrue(cleaned.endswith('.jpg'))
        # 目录部分不带进库内文件名（`/` 在 Windows 上同样是分隔符，两种平台一致）
        self.assertEqual(_safe_image_name('dir/wall.JPG'), 'wall.jpg')
        # 只剩点号的名字会退化成默认名，而不是产生空文件名
        self.assertEqual(_safe_image_name('...'), 'background')

    def test_import_rejects_oversized_image(self):
        with mock.patch('shell.backend.shell_info.MAX_BACKGROUND_IMAGE_BYTES', 4):
            result = self.shell_info.import_background_image(str(self.photo))
        self.assertFalse(result['success'])
        self.assertIn('超过', result['error'])

    def test_import_stops_at_library_limit(self):
        with mock.patch('shell.backend.shell_info.MAX_BACKGROUND_IMAGES', 1):
            self._import()
            result = self.shell_info.import_background_image(str(self.photo))
        self.assertFalse(result['success'])
        self.assertIn('最多', result['error'])

    # ===== 列举 =====
    def test_list_is_empty_without_library(self):
        self.assertEqual(self.shell_info.list_background_images(), [])

    def test_list_only_images_newest_first(self):
        first = self._import()
        second_photo = Path(self._tmp.name) / 'newer.png'
        second_photo.write_bytes(b'PNGDATA')
        second = self._import(second_photo)
        # 手工再放一个非图片文件：列表只列图片，与 `/file` 的放行集合一致
        (self.library / 'note.txt').write_text('not an image', encoding='utf-8')
        os.utime(self.library / first['name'], (1000, 1000))
        os.utime(self.library / second['name'], (2000, 2000))
        images = self.shell_info.list_background_images()
        self.assertEqual([item['name'] for item in images], [second['name'], first['name']])
        self.assertEqual(images[0]['path'], str(self.library / second['name']))

    # ===== 删除 =====
    def test_delete_removes_library_entry(self):
        image = self._import()
        result = self.shell_info.delete_background_image(image['path'])
        self.assertTrue(result['success'], result)
        self.assertFalse(Path(image['path']).exists())
        self.assertEqual(self.shell_info.list_background_images(), [])

    def test_delete_accepts_bare_name(self):
        image = self._import()
        self.assertTrue(self.shell_info.delete_background_image(image['name'])['success'])
        self.assertFalse(Path(image['path']).exists())

    def test_delete_refuses_paths_outside_library(self):
        """删除端点不能成为"删除本机任意文件"的入口。"""
        token_file = self.config_dir / 'auth_token.txt'
        token_file.write_text('x' * 43, encoding='utf-8')
        image = self._import()
        targets = (
            str(self.outside),                              # 库外的普通文件
            str(self.library / '..' / 'auth_token.txt'),    # 用 `..` 拼到库外
            str(self.library),                              # 目录本身
        )
        for target in targets:
            with self.subTest(target=target):
                result = self.shell_info.delete_background_image(target)
                self.assertFalse(result['success'])
        self.assertTrue(self.outside.exists())
        self.assertTrue(token_file.exists())
        self.assertTrue(Path(image['path']).exists())

    # ===== `/file` 的壳级允许根 =====
    def test_shell_request_reads_library_image(self):
        image = self._import()
        resp = self._get(f'/file?path={quote(image["path"])}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'JPEGDATA')

    def test_shell_request_denies_file_outside_library(self):
        """图片不再引用原路径：原图所在目录不在允许根里。"""
        self._import()
        resp = self._get(f'/file?path={quote(str(self.outside))}')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(b'OUTSIDE', resp.get_data())
        original = self._get(f'/file?path={quote(str(self.photo))}')
        self.assertEqual(original.status_code, 403)

    def test_library_serves_images_only(self):
        """图片库只用于取图片：手工放进库里的文档不能被顺带开放。"""
        self.library.mkdir(parents=True, exist_ok=True)
        (self.library / 'note.txt').write_text('secret', encoding='utf-8')
        image = self._import()
        text = self._get(f'/file?path={quote(str(self.library / "note.txt"))}')
        picture = self._get(f'/file?path={quote(image["path"])}')
        self.assertEqual(text.status_code, 403)
        self.assertNotIn(b'secret', text.get_data())
        self.assertEqual(picture.status_code, 200)

    def test_plugin_request_does_not_gain_library(self):
        """壳的壁纸库不能顺带对所有插件开放：插件请求仍只认自己的 get_file_roots()。"""
        self.manager.instances['probe'] = _StubInstance(self.data_root)
        image = self._import()
        resp = self._get(f'/file?path={quote(image["path"])}&plugin=probe')
        self.assertEqual(resp.status_code, 403)

    def test_deleted_image_is_denied(self):
        """删除后同一条请求立刻取不到内容：库根还在，但文件已不存在（404）。"""
        image = self._import()
        served = self._get(f'/file?path={quote(image["path"])}')
        self.assertEqual(served.status_code, 200)
        # Windows 上文件被打开的响应句柄占住，不关掉就删不掉（WinError 32）
        served.close()
        self.assertTrue(self.shell_info.delete_background_image(image['path'])['success'])
        gone = self._get(f'/file?path={quote(image["path"])}')
        self.assertEqual(gone.status_code, 404)
        self.assertNotIn(b'JPEGDATA', gone.get_data())


if __name__ == '__main__':
    unittest.main()
