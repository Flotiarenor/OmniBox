"""文件/缩略图路由的越界与状态码语义测试（docs/code-review.md §4.3）。

两个历史问题：
  1. `/thumbs` 的 `except Exception: abort(400)` 会把 `abort(403)` 抛出的
     HTTPException 一起吞掉 —— 越界访问返回 400 而不是 403（同文件另一处
     写法是对的，会用 e.code 还原）；
  2. 修好之后不能把正常路径一起改坏，所以这里同时断言正常读取仍可用。

运行：
    python -m unittest tests.test_file_server_paths -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import create_app
from shell.backend.paths import get_config_dir


class _StubPluginManager:
    _instances = {}

    def get_api_methods(self):
        return {}

    def get_frontend_manifests(self):
        return []

    def get_plugin_extensions(self):
        return {}

    def get_settings_panels(self):
        return []

    def save_settings_panel(self, *args, **kwargs):
        return None

    def get_plugin_instance(self, name):
        return None


class FilePathRouteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_root = Path(self._tmp.name) / 'data'
        self.data_root.mkdir()
        self.thumbs_dir = self.data_root / '.cache' / 'thumbs'
        self.thumbs_dir.mkdir(parents=True)
        # 根目录外的"机密"文件：任何请求都不该读到它
        self.outside = Path(self._tmp.name) / 'secret.txt'
        self.outside.write_text('TOP-SECRET', encoding='utf-8')

        config = {
            'server': {'host': '127.0.0.1', 'port': 18080},
            'directories': {'data_root': str(self.data_root)},
        }
        app = create_app(config, _StubPluginManager())
        self.client = app.test_client()
        self.token = get_or_create_token(get_config_dir())
        self.headers = {TOKEN_HEADER: self.token}

    def _get(self, url, headers=None):
        """统一发请求并登记关闭：文件响应带真实文件句柄，不关会刷 ResourceWarning。"""
        resp = self.client.get(url, headers=self.headers if headers is None else headers)
        self.addCleanup(resp.close)
        return resp

    def test_thumbs_traversal_is_403_not_400(self):
        """越界访问必须 403：403 被吞成 400 会掩盖真实的拒绝原因。"""
        resp = self._get('/thumbs/..\\..\\secret.txt')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('TOP-SECRET', resp.get_data(as_text=True))

    def test_thumbs_missing_file_is_404(self):
        resp = self._get('/thumbs/nope.jpg')
        self.assertEqual(resp.status_code, 404)

    def test_thumbs_normal_file_is_served(self):
        target = self.thumbs_dir / 'ok.jpg'
        target.write_bytes(b'JPEGDATA')
        resp = self._get('/thumbs/ok.jpg')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'JPEGDATA')

    def test_media_traversal_is_403(self):
        resp = self._get('/file?path=..\\..\\secret.txt')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('TOP-SECRET', resp.get_data(as_text=True))

    def test_media_normal_file_is_served(self):
        target = self.data_root / 'movie.mp4'
        target.write_bytes(b'MP4DATA')
        resp = self._get('/file?path=movie.mp4')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'MP4DATA')

    def test_absolute_path_outside_roots_is_403(self):
        resp = self._get(f'/file?path={self.outside}')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('TOP-SECRET', resp.get_data(as_text=True))

    def test_thumbs_requires_token(self):
        """越界之外的底线：缩略图属于数据路由，无令牌必须 401。"""
        resp = self._get('/thumbs/ok.jpg', headers={})
        self.assertEqual(resp.status_code, 401)


if __name__ == '__main__':
    unittest.main()
