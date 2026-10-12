"""`main.py` 的启动 / 退出路径回归。

两条与"写盘时机"有关的约定：

1. `wait_for_server` 不得经环境代理访问本机 Flask（历史上表现为"服务已监听仍判启动超时"）；
2. 「上次离开的页面」只记**安全退出** —— 桌面模式必须在 `webview.start()` **正常返回**
   之后提交，不能放进 `finally`：异常展开、崩溃与强杀都不算安全退出，那时应当保留
   上一次安全退出的值（见 `.dsh/shell-prefs-report.md` §7.5）。

运行：
    python -m unittest tests.test_main_startup -v
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.paths import get_config_dir
from shell.backend.shell_prefs import ShellPrefs


def _load_main():
    """按绝对路径加载仓库根的 main.py。

    不能直接 `import main`：其他用例会把插件 backend 目录插到 `sys.path`（其中有同名
    main.py），`unittest discover` 的导入顺序会让这里的 `import main` 命中插件模块。
    """
    spec = importlib.util.spec_from_file_location('omnibox_main_under_test',
                                                  PROJECT_ROOT / 'main.py')
    if spec is None or spec.loader is None:
        raise ImportError('无法为 main.py 创建模块加载器')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = _load_main()


class _OkResponse:
    status_code = 200


class WaitForServerTests(unittest.TestCase):
    def test_health_check_disables_env_proxy(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            return _OkResponse()

        with mock.patch.object(main.requests, 'get', fake_get):
            self.assertTrue(main.wait_for_server('127.0.0.1', 18080, timeout=1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'http://127.0.0.1:18080/health')
        self.assertEqual(calls[0][1].get('proxies'), {'http': None, 'https': None})

    def test_health_check_retries_after_request_exception(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                raise main.requests.ConnectionError('not up')
            return _OkResponse()

        with mock.patch.object(main.requests, 'get', fake_get):
            self.assertTrue(main.wait_for_server('127.0.0.1', 18080, timeout=2))
        self.assertEqual(len(calls), 2)


class _StubManager:
    """`_run_app` 用到的全部插件管理器能力（其余一律返回空）。"""

    def get_frontend_manifests(self):
        return []

    def get_plugin_extensions(self):
        return {}

    def get_plugin_status(self):
        return {'loaded': [], 'failures': []}

    def get_api_methods(self):
        return {}

    def freshness_api_methods(self):
        return {}


class DesktopExitTests(unittest.TestCase):
    """桌面窗口的退出路径：提交「上次离开的页面」的时机。"""

    def setUp(self):
        self.config = {'server': {'host': '127.0.0.1', 'port': 18080},
                       'directories': {'data_root': str(get_config_dir() / 'data')}}

    def _run(self, start_side_effect=None):
        """跑 `_run_desktop_window`，把建窗与消息循环替身掉，返回调用顺序。"""
        order = []

        def fake_start(*_args, **_kwargs):
            order.append('webview.start')
            if start_side_effect is not None:
                raise start_side_effect

        def fake_create_app(*_args, **_kwargs):
            order.append('create_app')
            return mock.Mock()

        def fake_wait(*_args, **_kwargs):
            order.append('wait_for_server')
            return True

        error = None
        with mock.patch.object(main, 'create_app', fake_create_app), \
                mock.patch.object(main, 'wait_for_server', fake_wait), \
                mock.patch.object(main.webview, 'create_window', lambda *a, **k: None), \
                mock.patch.object(main.webview, 'start', fake_start), \
                mock.patch.object(main, 'register_desktop_window', lambda: None), \
                mock.patch.object(main, '_webview_storage_dir', lambda: ''), \
                mock.patch.object(main, '_commit_last_route',
                                  lambda: order.append('commit')):
            try:
                main._run_desktop_window(self.config, _StubManager(), object(),
                                         '127.0.0.1', 18080)
            except Exception as e:      # 由调用方决定要不要接
                error = e
        return order, error

    def test_commit_runs_after_the_window_loop_returns(self):
        order, error = self._run()
        self.assertIsNone(error, f'_run_desktop_window 抛异常了: {error!r}')
        self.assertIn('commit', order, '关窗退出后没有提交「上次离开的页面」')
        self.assertLess(order.index('webview.start'), order.index('commit'),
                        '提交必须发生在 webview.start() 返回之后')

    def test_commit_is_skipped_when_the_window_loop_raises(self):
        """异常展开不算安全退出：抛异常时不得提交（保留上一次安全退出的值）。"""
        order, error = self._run(start_side_effect=RuntimeError('窗口循环失败'))
        self.assertIsInstance(error, RuntimeError)
        self.assertNotIn('commit', order,
                         '异常路径提交了路由，会覆盖上一次安全退出的值')


class CommitLastRouteTests(unittest.TestCase):
    """提交函数本身：幂等、且不吞异常时不留痕。"""

    def test_commit_is_a_noop_without_pending_values(self):
        calls = []
        with mock.patch.object(ShellPrefs, 'commit_last_route',
                               lambda self: calls.append(self) or False):
            main._commit_last_route()
        self.assertEqual(len(calls), 1)

    def test_commit_swallows_errors(self):
        with mock.patch.object(ShellPrefs, 'commit_last_route',
                               side_effect=OSError('磁盘满了')):
            main._commit_last_route()   # 不应抛出：退出路径上抛异常会盖住退出码


if __name__ == '__main__':
    unittest.main()
