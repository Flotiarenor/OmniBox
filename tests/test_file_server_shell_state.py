"""壳首页的状态注入与状态标记页（`file_server.serve_shell` / 错误页）。

为什么这几条必须锁住：

- 壳偏好不再放 localStorage，而是由 `serve_shell` 注入 `window.__OMNIBOX_SHELL_STATE__`。
  注入点必须早于模块脚本（否则首帧会先画默认色再跳到用户主题），并且**必须转义**
  `<` 与 `&` —— 颜色值是用户输入，一个 `</script>` 会直接闭合脚本块；
- index.html 现在是动态响应：一旦被缓存，用户改过的主题与启动页会按旧值发出来，
  而这种现象与"没存住"长得一模一样；
- 状态标记页原来靠 localStorage 取主题，偏好搬家后它必须改由服务端注入，
  否则每个 401/404 页面都会闪回默认外观。

运行：
    python -m unittest tests.test_file_server_shell_state -v
"""

import json
import re
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
    """最小插件管理器：本用例只走壳自己的路由。"""

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
        return []


class ShellStateInjectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.data_root = self.dir / 'data'
        self.data_root.mkdir()
        self.config = {
            'server': {'host': '127.0.0.1', 'port': 18080},
            'directories': {'data_root': str(self.data_root)},
        }
        # `create_app` 读的是模块级的 `get_config_dir()`（真实配置目录）。注入用例
        # 只作为"落盘的偏好会被读到"的黑盒，因此刻意不动它：直接写进真实配置目录，
        # 用完删掉。测试里不隔离它，才能验到真实部署下"写盘 → 下次 GET 生效"。
        self.config_dir = get_config_dir()
        self.shell_json = self.config_dir / 'shell.json'
        self._backup = self.shell_json.read_text(encoding='utf-8') if self.shell_json.exists() else None
        if self._backup is not None:
            self.addCleanup(self._restore_shell_json)
        else:
            self.addCleanup(self._remove_shell_json)
        self.app = create_app(self.config, _StubPluginManager())
        self.client = self.app.test_client()
        self.headers = {TOKEN_HEADER: get_or_create_token(self.config_dir)}

    def _restore_shell_json(self):
        assert self._backup is not None
        self.shell_json.write_text(self._backup, encoding='utf-8')

    def _remove_shell_json(self):
        self.shell_json.unlink(missing_ok=True)

    def _write_prefs(self, prefs):
        self.shell_json.write_text(json.dumps({'prefs': prefs}, ensure_ascii=False),
                                   encoding='utf-8')

    def _injected(self, path='/', headers=None):
        """取首页响应，解出注入的 JSON（找不到注入块时直接失败）。"""
        resp = self.client.get(path, headers=headers if headers is not None else {})
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        match = re.search(
            r'<script>window\.__OMNIBOX_SHELL_STATE__=(.*?);</script>', body, re.DOTALL)
        self.assertIsNotNone(match, '首页里没有注入 __OMNIBOX_SHELL_STATE__')
        assert match is not None
        return resp, body, match

    # ===== 注入 =====

    def test_index_html_carries_shell_state_without_token(self):
        """首页是免令牌路由，注入块同样不得要求令牌（壳加载时还没有 Cookie）。"""
        _, _, match = self._injected()
        state = json.loads(match.group(1))
        self.assertIn('prefs', state)
        self.assertIn('theme', state['prefs'])

    def test_injected_script_precedes_the_module_entry(self):
        """注入必须早于模块入口：晚一步就会先画默认色再跳到用户主题。"""
        _, body, _ = self._injected()
        entry = re.search(r'<script type="module"[^>]*src="([^"]+)"', body)
        self.assertIsNotNone(entry, f'首页里找不到模块入口脚本: {body[:200]}')
        assert entry is not None
        self.assertLess(body.index('__OMNIBOX_SHELL_STATE__'), entry.start())

    def test_stored_prefs_are_injected(self):
        self._write_prefs({'theme': 'light', 'colors': {'--accent': '#8b5cf6'},
                           'startup': {'page': 'settings', 'route': '', 'fullscreen': True}})
        _, _, match = self._injected()
        prefs = json.loads(match.group(1))['prefs']
        self.assertEqual(prefs['theme'], 'light')
        self.assertEqual(prefs['colors']['--accent'], '#8b5cf6')
        self.assertEqual(prefs['startup']['page'], 'settings')
        self.assertIs(prefs['startup']['fullscreen'], True)

    def test_script_breaker_in_a_color_value_is_escaped(self):
        """颜色值是用户输入：`</script>` 必须被转义，否则整个页面结构被破坏。"""
        self._write_prefs({'colors': {'--bg-app': '</script><script>window.PWNED=1</script>'}})
        _, _, match = self._injected()
        # 注入块内部不得出现真正的结束标签：出现一次，脚本就在这里被浏览器截断
        self.assertNotIn('</script>', match.group(1))
        self.assertIn('\\u003c', match.group(1), '`<` 必须转义成 \\u003c')
        # 转义后它只是一段字符串数据，不再是可执行的脚本标签
        self.assertEqual(
            len(re.findall(r'<script>window\.__OMNIBOX_SHELL_STATE__=', match.group(0))), 1)

    def test_ampersand_is_escaped(self):
        self._write_prefs({'colors': {'--bg-app': 'a&b'}})
        _, _, match = self._injected()
        self.assertIn('\\u0026', match.group(1))

    def test_broken_shell_json_does_not_break_the_page(self):
        # 开发机上此前跑过这个用例会留下隔离文件：只比对"本次新增的那一份"
        before = set(self.config_dir.glob('shell.json.corrupt-*'))
        self.shell_json.write_text('{ 不是 JSON', encoding='utf-8')
        _, _, match = self._injected()
        prefs = json.loads(match.group(1))['prefs']
        self.assertEqual(prefs['theme'], 'dark', '损坏文件回退默认，而不是白屏')
        # 损坏文件由 SettingsStore 改名留档（文件级校验），并且只隔离一次
        added = set(self.config_dir.glob('shell.json.corrupt-*')) - before
        self.assertEqual(len(added), 1, '损坏文件必须留档一份，供人工恢复')
        for path in added:
            path.unlink(missing_ok=True)

    def test_index_is_not_cached(self):
        """index.html 是动态响应：走 304 会把旧偏好一直发下去。"""
        resp, _, _ = self._injected()
        self.assertIn('no-store', resp.headers.get('Cache-Control', ''))

    def test_spa_routes_get_the_same_injected_page(self):
        resp, _, _ = self._injected('/settings')
        self.assertIn('no-store', resp.headers.get('Cache-Control', ''))

    def test_missing_asset_is_still_a_404_with_content(self):
        """assets 下的缺失文件不能回落到首页（否则 404 会变成 200）。"""
        resp = self.client.get('/assets/does-not-exist.js')
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 404)

    # ===== 状态标记页 =====

    def test_status_page_uses_the_stored_theme(self):
        self._write_prefs({'theme': 'light', 'colors': {'--accent': '#8b5cf6'}})
        resp = self.client.get('/file?path=/nope')     # 未带令牌 → 401 标记页
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 401)
        body = resp.get_data(as_text=True)
        self.assertIn('data-theme="light"', body)
        self.assertIn('--accent:#8b5cf6', body)
        # 偏好已不在 localStorage：标记页不得再留一条会自动失效的回退读
        self.assertNotIn("localStorage.getItem('omni-theme')", body)

    def test_status_page_escapes_injected_values(self):
        self._write_prefs({'colors': {'--bg-app': '"/><script>window.PWNED=1</script>'}})
        resp = self.client.get('/file?path=/nope')
        self.addCleanup(resp.close)
        body = resp.get_data(as_text=True)
        self.assertNotIn('<script>window.PWNED', body)

    def test_status_page_defaults_to_dark(self):
        self._remove_shell_json()
        resp = self.client.get('/file?path=/nope')
        self.addCleanup(resp.close)
        self.assertIn('data-theme="dark"', resp.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
