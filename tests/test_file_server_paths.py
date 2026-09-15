"""文件/缩略图路由的越界与状态码语义测试（docs/code-review.md §4.3）。

两个历史问题：
  1. `/thumbs` 的 `except Exception: abort(400)` 会把 `abort(403)` 抛出的
     HTTPException 一起吞掉 —— 越界访问返回 400 而不是 403（同文件另一处
     写法是对的，会用 e.code 还原）；
  2. 修好之后不能把正常路径一起改坏，所以这里同时断言正常读取仍可用。

运行：
    python -m unittest tests.test_file_server_paths -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import create_app
from shell.backend.paths import get_config_dir

# 越界载荷必须用当前平台的分隔符：POSIX 上反斜杠是合法文件名字符，
# `..\..\secret.txt` 只是个普通文件名（结果是 404 而非穿越），断言 403 会假失败。
_TRAVERSAL = '..\\..\\secret.txt' if os.name == 'nt' else '../../secret.txt'


class _StubPluginManager:
    _instances: ClassVar[dict] = {}

    def get_api_methods(self):
        return {}

    def get_frontend_manifests(self):
        return []

    def get_plugin_extensions(self):
        return {}

    def get_plugin_status(self):
        return {'loaded': [], 'failures': []}

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
        resp = self._get(f'/thumbs/{_TRAVERSAL}')
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
        resp = self._get(f'/file?path={_TRAVERSAL}')
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


class SecurityHeaderTests(unittest.TestCase):
    """安全响应头必须在所有响应出口统一施加（含 send_file 的文件响应）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_root = Path(self._tmp.name) / 'data'
        self.data_root.mkdir()
        config = {
            'server': {'host': '127.0.0.1', 'port': 18080},
            'directories': {'data_root': str(self.data_root)},
        }
        app = create_app(config, _StubPluginManager())
        self.client = app.test_client()
        self.token = get_or_create_token(get_config_dir())
        self.headers = {TOKEN_HEADER: self.token}

    def _get(self, url, headers=None):
        resp = self.client.get(url, headers=self.headers if headers is None else headers)
        self.addCleanup(resp.close)
        return resp

    def _assert_common_headers(self, resp):
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(resp.headers.get('X-Frame-Options'), 'SAMEORIGIN')
        self.assertEqual(resp.headers.get('Referrer-Policy'), 'no-referrer')
        csp = resp.headers.get('Content-Security-Policy') or ''
        for directive in ("object-src 'none'", "base-uri 'self'", "frame-ancestors 'self'"):
            with self.subTest(directive=directive):
                self.assertIn(directive, csp)
        # 强执行档刻意不含 default-src / script-src：收紧脚本来源会打坏 7 个插件的
        # 内联脚本与 pywebview 桥接，必须先在 Report-Only 下验证过再合并。这条断言
        # 防止有人顺手把目标档的策略挪进强执行档而不做验证。
        self.assertNotIn('default-src', csp)
        self.assertNotIn('script-src', csp)
        self.assertIn('Content-Security-Policy-Report-Only', resp.headers)

    def test_headers_on_open_route(self):
        self._assert_common_headers(self._get('/health', headers={}))

    def test_headers_on_error_response(self):
        """401 也要带头：错误页同样可能被嵌进 iframe 或被嗅探。

        用 POST：`/api` 只在 POST 上注册（`serve_shell` 会把 GET 的 api/ 路径
        显式判成 404，见 file_server 里那段注释），所以令牌墙只在 POST 上可达。
        """
        resp = self.client.post('/api/nope', headers={})
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 401)
        self._assert_common_headers(resp)

    def test_headers_on_file_response(self):
        """send_file 的响应也必须带头（/file 是用户文件与外部输入的主要出口）。"""
        target = self.data_root / 'movie.mp4'
        target.write_bytes(b'MP4DATA')
        resp = self._get('/file?path=movie.mp4')
        self.assertEqual(resp.status_code, 200)
        self._assert_common_headers(resp)


if __name__ == '__main__':
    unittest.main()
