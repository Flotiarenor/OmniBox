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

    def get_settings_panels(self):
        return []

    def save_settings_panel(self, *args, **kwargs):
        return None

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


if __name__ == '__main__':
    unittest.main()
