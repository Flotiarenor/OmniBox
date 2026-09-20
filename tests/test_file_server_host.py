"""Host 头白名单的回归测试（防 DNS rebinding）。

对应审查项：docs/code-review.md §3.1。攻击链是"攻击者域名解析到
127.0.0.1 → 浏览器认为与 OmniBox 同源 → 自动带上令牌 Cookie 读走数据"，
所以这里断言的核心只有一条：**未列入白名单的 Host 拿不到任何数据，
有没有令牌都一样。**

运行：
    python -m unittest tests.test_file_server_host -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import _build_trusted_hosts, _local_ipv4_addresses, create_app
from shell.backend.paths import get_config_dir


class _StubPluginManager:
    """只提供 create_app 在请求期会用到的接口，避免真实加载插件。"""

    _instances: ClassVar[dict] = {}

    def get_api_methods(self):
        return {}

    def get_frontend_manifests(self):
        return []

    def get_plugin_extensions(self):
        return {}

    def get_plugin_status(self):
        return {'loaded': [], 'failures': []}

    def get_plugin_instance(self, name):
        return None

    def get_protected_paths(self):
        """本桩不注册任何插件实例，因此没有任何插件申报的受保护路径。"""
        return []


def _make_client(host: str = '127.0.0.1', trusted_hosts=None):
    server = {'host': host, 'port': 18080}
    if trusted_hosts is not None:
        server['trusted_hosts'] = trusted_hosts
    config = {
        'server': server,
        'directories': {'data_root': tempfile.gettempdir()},
    }
    app = create_app(config, _StubPluginManager())
    return app.test_client()


class TrustedHostTests(unittest.TestCase):
    """未列入白名单的 Host 必须被拒（400），且不受令牌影响。"""

    def setUp(self):
        self.client = _make_client()
        self.token = get_or_create_token(get_config_dir())

    def test_loopback_hosts_allowed(self):
        for base in ('http://127.0.0.1:18080', 'http://localhost:18080'):
            with self.subTest(base=base):
                self.assertEqual(self.client.get('/health', base_url=base).status_code, 200)

    def test_attacker_domain_rejected_on_open_endpoint(self):
        """连开放路由 /health 都不放过：不给攻击者任何同源探测面。"""
        resp = self.client.get('/health', base_url='http://evil.tld:18080')
        self.assertEqual(resp.status_code, 400)

    def test_attacker_domain_rejected_on_page_routes(self):
        for path in ('/', '/status'):
            with self.subTest(path=path):
                resp = self.client.get(path, base_url='http://evil.tld:18080')
                self.assertEqual(resp.status_code, 400)

    def test_attacker_domain_rejected_without_token(self):
        resp = self.client.post('/api/system_get_config', base_url='http://evil.tld:18080')
        self.assertIn(resp.status_code, (400, 401))

    def test_attacker_domain_rejected_even_with_valid_token(self):
        """关键断言：DNS rebinding 下浏览器会自动带上合法 Cookie，令牌救不了它。"""
        resp = self.client.post(
            '/api/system_get_config',
            base_url='http://evil.tld:18080',
            headers={TOKEN_HEADER: self.token},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('directories', resp.get_data(as_text=True))

    def test_trusted_host_with_token_reaches_api(self):
        resp = self.client.post(
            '/api/system_get_config',
            base_url='http://127.0.0.1:18080',
            headers={TOKEN_HEADER: self.token},
        )
        self.assertEqual(resp.status_code, 200)

    def test_trusted_host_without_token_is_401(self):
        resp = self.client.post('/api/system_get_config', base_url='http://127.0.0.1:18080')
        self.assertEqual(resp.status_code, 401)

    def test_lan_ip_rejected_when_bound_to_loopback(self):
        """默认只监听 127.0.0.1 时，本机网卡 IP 不应该被放行。"""
        ips = sorted(_local_ipv4_addresses())
        if not ips:
            self.skipTest('未探测到本机局域网 IPv4 地址')
        resp = self.client.get('/health', base_url=f'http://{ips[0]}:18080')
        self.assertEqual(resp.status_code, 400)

    def test_lan_ip_allowed_when_bound_to_wildcard(self):
        """host 改成 0.0.0.0 表示"就是要局域网访问"：放行本机网卡 IP。"""
        ips = sorted(_local_ipv4_addresses())
        if not ips:
            self.skipTest('未探测到本机局域网 IPv4 地址')
        client = _make_client(host='0.0.0.0')
        resp = client.get('/health', base_url=f'http://{ips[0]}:18080')
        self.assertEqual(resp.status_code, 200)

    def test_wildcard_bind_still_rejects_domains(self):
        """放行的是 IP，不是任意域名 —— 否则防 rebinding 就白做了。"""
        client = _make_client(host='0.0.0.0')
        resp = client.get('/health', base_url='http://evil.tld:18080')
        self.assertEqual(resp.status_code, 400)

    def test_explicit_trusted_hosts_entry_allowed(self):
        client = _make_client(host='127.0.0.1', trusted_hosts=['omnibox.lan'])
        resp = client.get('/health', base_url='http://omnibox.lan:18080')
        self.assertEqual(resp.status_code, 200)
        # 未在列表里的域名依然被拒
        resp = client.get('/health', base_url='http://other.lan:18080')
        self.assertEqual(resp.status_code, 400)


class TrustedHostListTests(unittest.TestCase):
    """_build_trusted_hosts 的取值规则（不涉网络，纯函数）。"""

    def test_loopback_always_present(self):
        hosts = _build_trusted_hosts({})
        self.assertIn('127.0.0.1', hosts)
        self.assertIn('localhost', hosts)

    def test_concrete_host_added(self):
        hosts = _build_trusted_hosts({'server': {'host': '192.168.31.16'}})
        self.assertIn('192.168.31.16', hosts)

    def test_wildcard_host_adds_lan_ips_not_wildcard_itself(self):
        hosts = _build_trusted_hosts({'server': {'host': '0.0.0.0'}})
        self.assertNotIn('0.0.0.0', hosts)
        self.assertTrue(set(hosts) >= {'127.0.0.1', 'localhost'})

    def test_extra_entries_are_stripped_and_non_list_ignored(self):
        hosts = _build_trusted_hosts({'server': {'trusted_hosts': ['  a.lan  ', '', None]}})
        self.assertIn('a.lan', hosts)
        self.assertNotIn('', hosts)
        self.assertEqual(_build_trusted_hosts({'server': {'trusted_hosts': 'oops'}}).count('oops'), 0)

    def test_broken_config_does_not_crash(self):
        self.assertIn('127.0.0.1', _build_trusted_hosts({'server': 'not-a-dict'}))


class PluginFrontendBootstrapTest(unittest.TestCase):
    """插件前端的**每个** HTML 页面都要拿到壳的引导脚本（Bridge / 共享组件 / 主题同步）。

    以前只有 `index.html` 走这条注入。子页面（例如"网络位置"提供方要在共享目录组件
    FolderPicker 的弹窗 iframe 里渲染的选择器）因此拿不到 `window.Bridge`，界面上报
    "PyWebView API 不可用"，而页面本身看起来毫无异常。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        plugin_dir = Path(self._tmp.name) / 'demo'
        frontend = plugin_dir / 'frontend'
        (frontend / 'sub').mkdir(parents=True)
        (frontend / 'index.html').write_text(
            '<html><head></head><body>main-page</body></html>', encoding='utf-8')
        (frontend / 'sub' / 'picker.html').write_text(
            '<html><head></head><body>picker-page</body></html>', encoding='utf-8')
        (frontend / 'plain.txt').write_text('not html', encoding='utf-8')
        # 插件目录**之外**的同名文件：子页面注入是自己 open 文件的，包含判定必须挡住它
        (Path(self._tmp.name) / 'outside.html').write_text(
            '<html><head></head><body>outside</body></html>', encoding='utf-8')

        class _Manager(_StubPluginManager):
            def get_plugin_dir(self, name):
                return plugin_dir if name == 'demo' else None

        config = {
            'server': {'host': '127.0.0.1', 'port': 18080},
            'directories': {'data_root': tempfile.gettempdir()},
        }
        self.client = create_app(config, _Manager()).test_client()

    def test_every_html_page_gets_the_bootstrap(self):
        for path, marker in (('/plugins/demo/frontend/index.html', 'main-page'),
                             ('/plugins/demo/frontend/sub/picker.html', 'picker-page')):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertIn(marker, html, '页面本身的内容不该被改动')
                self.assertIn('/shell/base.js', html, '缺少壳的 Bridge/Utils')
                self.assertIn('/shell/folder-picker.js', html, '缺少共享目录组件')
                self.assertIn("Bridge.setPrefix('demo');", html, '插件前缀必须按路由替换')

    def test_non_html_is_served_untouched(self):
        resp = self.client.get('/plugins/demo/frontend/plain.txt')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(as_text=True), 'not html')

    def test_html_outside_the_plugin_dir_is_not_served(self):
        """越界路径绝不能读到插件目录外的文件（手工 open 的那条路要自己判定）。"""
        for path in ('/plugins/demo/frontend/../../outside.html',
                     '/plugins/demo/frontend/%2e%2e/%2e%2e/outside.html'):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertNotEqual(resp.status_code, 200)
                self.assertNotIn('outside', resp.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
