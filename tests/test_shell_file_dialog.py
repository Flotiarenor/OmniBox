"""系统文件对话框：桌面窗口的有无决定成败，而不是"请求走了哪条路"。

回归背景：桌面模式下 Flask 与 pywebview 在同一个进程里，前端 `bridge` 可能落到 HTTP
shim，于是设置页点「选择图片…」走的是 `/api/system_pick_path`。file_server 曾经把它
当成"浏览器访问模式"直接返回失败，桌面用户因此收到
"浏览器访问模式没有系统文件对话框"。真正的前提是**有没有桌面窗口**。

运行：
    python -m unittest tests.test_shell_file_dialog -v
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend import file_dialog
from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import create_app
from shell.backend.paths import get_config_dir, get_plugins_config_dir
from shell.backend.plugin_manager import collect_protected_paths


def _fake_webview(result=None, has_window=True, raises=False):
    """一个够用的 webview 替身，并记录 create_file_dialog 收到的参数。"""
    module = types.ModuleType('webview')
    # 与真实模块同值（webview/__init__.py 的 FileDialog：OPEN=10 / FOLDER=20）
    module.FileDialog = types.SimpleNamespace(OPEN=10, FOLDER=20)
    calls = []

    class _Window:
        def create_file_dialog(self, dialog_type, allow_multiple=False, file_types=()):
            calls.append({
                'dialog_type': dialog_type,
                'allow_multiple': allow_multiple,
                'file_types': tuple(file_types),
            })
            if raises:
                raise RuntimeError('对话框炸了')
            return result

    module.windows = [_Window()] if has_window else []
    return module, calls


class _StubPluginManager:
    """create_app 需要的最小插件管理器。"""

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
        return None

    def get_plugin_dir(self, name):
        return None

    def get_protected_paths(self):
        return collect_protected_paths({}, get_plugins_config_dir())


class PickPathTest(unittest.TestCase):
    def _pick(self, kind='image', **kwargs):
        module, calls = _fake_webview(**kwargs)
        with mock.patch.dict(sys.modules, {'webview': module}):
            return file_dialog.pick_path(kind), calls

    def test_file_dialog_returns_path(self):
        result, calls = self._pick(result=('D:\\wall.jpg',))
        self.assertTrue(result['success'])
        self.assertEqual(result['path'], 'D:\\wall.jpg')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['dialog_type'], 10)          # FileDialog.OPEN
        self.assertFalse(calls[0]['allow_multiple'])
        # 文件模式必须带上图片过滤器，否则用户在一个混杂目录里要自己找图
        self.assertIn('图片', calls[0]['file_types'][0])

    def test_directory_dialog_has_no_file_types(self):
        result, calls = self._pick(kind='directory', result=('D:\\Pictures',))
        self.assertEqual(result['path'], 'D:\\Pictures')
        self.assertEqual(calls[0]['dialog_type'], 20)          # FileDialog.FOLDER
        self.assertEqual(calls[0]['file_types'], ())

    def test_cancel_is_not_a_failure(self):
        """用户取消与"这个环境选不了"必须能区分：前者静默，后者要提示。"""
        result, _ = self._pick(result=None)
        self.assertTrue(result['success'])
        self.assertEqual(result['path'], '')

    def test_without_window_reports_reason(self):
        result, calls = self._pick(has_window=False)
        self.assertFalse(result['success'])
        self.assertIn('桌面窗口', result['error'])
        self.assertEqual(calls, [])

    def test_dialog_exception_is_reported(self):
        result, _ = self._pick(raises=True)
        self.assertFalse(result['success'])
        self.assertIn('失败', result['error'])

    def test_string_result_is_not_truncated(self):
        """平台实现返回字符串而不是元组时，不能把路径的第一个字符当成结果。

        真出现这种情况时，界面会拿 `D` 当目录去配置，报的却是"目录不存在" ——
        现象与原因完全对不上。
        """
        result, _ = self._pick(result='D:\\wall.jpg')
        self.assertEqual(result['path'], 'D:\\wall.jpg')

    def test_empty_string_result_is_cancel(self):
        result, _ = self._pick(result='')
        self.assertTrue(result['success'])
        self.assertEqual(result['path'], '')

    def test_concurrent_dialog_is_refused(self):
        """对话框阻塞到用户作答：并发调用只服务一个，不叠出第二个模态窗口。"""
        module, calls = _fake_webview(result=('D:\\wall.jpg',))
        acquired = file_dialog._dialog_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with mock.patch.dict(sys.modules, {'webview': module}):
                result = file_dialog.pick_path('image')
        finally:
            file_dialog._dialog_lock.release()
        self.assertFalse(result['success'])
        self.assertIn('已有一个', result['error'])
        self.assertEqual(calls, [], '并发调用不该真的去开对话框')

    def test_lock_is_released_after_a_call(self):
        """一次正常调用之后锁必须归还，否则后续调用全部被拒。"""
        self._pick(result=('D:\\wall.jpg',))
        self.assertTrue(file_dialog._dialog_lock.acquire(blocking=False))
        file_dialog._dialog_lock.release()


class PickPathHttpRouteTest(unittest.TestCase):
    """HTTP 路径与桌面 js_api 注册的是同一个实现，因此这里也必须能弹。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        data_root = root / 'data'
        data_root.mkdir()
        self.photo = root / 'wall.jpg'
        self.photo.write_bytes(b'JPEGDATA')
        config = {
            'server': {'host': '127.0.0.1', 'port': 18080},
            'directories': {'data_root': str(data_root)},
        }
        app = create_app(config, _StubPluginManager())
        self.client = app.test_client()
        self.headers = {TOKEN_HEADER: get_or_create_token(get_config_dir())}

    def test_api_route_returns_picked_path(self):
        module, calls = _fake_webview(result=(str(self.photo),))
        with mock.patch.dict(sys.modules, {'webview': module}):
            resp = self.client.post('/api/system_pick_path', headers=self.headers,
                                    json={'args': ['image']})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['result']['path'], str(self.photo))
        self.assertEqual(len(calls), 1, 'HTTP 路径必须真的调用系统对话框')

    def test_api_route_survives_without_window(self):
        module, _ = _fake_webview(has_window=False)
        with mock.patch.dict(sys.modules, {'webview': module}):
            resp = self.client.post('/api/system_pick_path', headers=self.headers,
                                    json={'args': ['image']})
        self.assertEqual(resp.status_code, 200)
        result = resp.get_json()['result']
        self.assertFalse(result['success'])
        self.assertIn('桌面窗口', result['error'])


if __name__ == '__main__':
    unittest.main()
