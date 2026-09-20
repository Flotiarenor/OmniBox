"""`wait_for_server` 的启动探活回归：不得经环境代理访问本机 Flask。

对应问题：requests 默认读取 `HTTP_PROXY` / `HTTPS_PROXY`。环境里存在代理时，
探活请求被交给代理（实测 `ProxyError`），Flask 已监听也会被判定为启动超时。

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


if __name__ == '__main__':
    unittest.main()
