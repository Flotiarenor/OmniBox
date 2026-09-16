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
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_FILE_NAME, TOKEN_HEADER, get_or_create_token, get_token_file
from shell.backend.file_server import create_app
from shell.backend.paths import get_config_dir, get_plugins_config_dir
from shell.backend.plugin_manager import collect_protected_paths

# 越界载荷必须用当前平台的分隔符：POSIX 上反斜杠是合法文件名字符，
# `..\..\secret.txt` 只是个普通文件名（结果是 404 而非穿越），断言 403 会假失败。
_TRAVERSAL = '..\\..\\secret.txt' if os.name == 'nt' else '../../secret.txt'


class _StubInstance:
    """最小插件实例：只实现文件/缩略图路由会调用的成员。

    存在的意义是能自由设定 `get_file_roots()` / `thumb_dir` / `get_protected_paths()`
    —— 也就是审计 §1.1 那条链路的可控输入：插件把根设成"包含 .config 的目录"而不是
    默认数据根，或者反过来申报受保护路径。
    """

    def __init__(self, file_roots, thumb_dir=None, protected=(), data_root=None):
        self._file_roots = [Path(root) for root in file_roots]
        self._data_root = Path(data_root) if data_root is not None else self._file_roots[0]
        self._thumb_dir = Path(thumb_dir) if thumb_dir is not None else None
        self._protected = [Path(path) for path in protected]

    def get_file_roots(self):
        return list(self._file_roots)

    def get_data_root(self):
        return self._data_root

    def get_protected_paths(self):
        return list(self._protected)

    @property
    def thumb_dir(self):
        return self._thumb_dir or (self._file_roots[0] / '.cache' / 'thumbs')

    def get_thumb_data(self, rel_path):
        return None

    def ensure_thumb(self, rel_path):
        return None


class _StubPluginManager:
    def __init__(self):
        self._instances: dict = {}
        self._plugin_dirs: dict = {}

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
        return self._instances.get(name)

    def get_plugin_dir(self, name):
        return self._plugin_dirs.get(name)

    def get_protected_paths(self):
        """与真实 PluginManager 共用同一份聚合 + 边界校验实现。

        刻意不在这里写 `return []`：否则"申报越界必须被忽略"这条用例会因为桩
        放松了校验而静默变绿，测的就不是真实行为了。
        """
        return collect_protected_paths(self._instances, get_plugins_config_dir())


class _FileRouteFixture:
    """文件路由用例的公共夹具。

    刻意**不**继承 unittest.TestCase：否则它会被当成一个用例类收集，而子类
    再继承它时父类的用例会被重复执行一遍（结果看起来全绿，实际跑了两遍）。
    """
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
        self.manager = _StubPluginManager()
        app = create_app(config, self.manager)
        self.client = app.test_client()
        self.token = get_or_create_token(get_config_dir())
        self.headers = {TOKEN_HEADER: self.token}

    def _get(self, url, headers=None):
        """统一发请求并登记关闭：文件响应带真实文件句柄，不关会刷 ResourceWarning。"""
        resp = self.client.get(url, headers=self.headers if headers is None else headers)
        self.addCleanup(resp.close)
        return resp

    def _rogue_root_containing_credentials(self, thumb_dir=None):
        """注册一个根目录 = <config_dir> 的插件，返回它的名字。

        开发模式下 <config_dir> 与默认 <data_root>（./data）是兄弟目录，
        所以 `../.config` 这种根一旦被接受，凭据文件就落在合法范围内。
        """
        self.manager._instances['rogue'] = _StubInstance([get_config_dir()], thumb_dir=thumb_dir)
        return 'rogue'

    def _token_path(self) -> Path:
        return get_token_file(get_config_dir())


class FilePathRouteTests(_FileRouteFixture, unittest.TestCase):
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


class ProtectedCredentialFileTests(_FileRouteFixture, unittest.TestCase):
    """壳自己的凭据文件不得从任何数据路由流出去。

    攻击链（审计 §1.1 的改根链路 + 兄弟目录布局）：插件把文件根设成包含
    <config_dir> 的目录 → `/file` 的「是否在允许根之内」检查通过 → 读走
    `auth_token.txt`。它是长期进程外凭据，读走等于把一次前端 XSS 或一次
    局域网泄露自举成持久令牌，所以这条判定必须独立于插件提供的根。
    """

    def test_token_file_is_403_even_inside_a_plugin_root(self):
        """绝对路径形态：根合法也不再放行。"""
        plugin = self._rogue_root_containing_credentials()
        token_path = self._token_path()
        self.assertTrue(token_path.exists(), '前置条件：令牌文件已生成')
        resp = self._get(f'/file?path={quote(str(token_path))}&plugin={plugin}')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(self.token, resp.get_data(as_text=True))

    def test_token_file_is_403_as_relative_path(self):
        """相对路径形态：roots[0] 就是 config 目录时，"裸文件名"也必须被拒。"""
        plugin = self._rogue_root_containing_credentials()
        resp = self._get(f'/file?path={TOKEN_FILE_NAME}&plugin={plugin}')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(self.token, resp.get_data(as_text=True))

    def test_thumbs_never_serves_token_file(self):
        """缩略图路由走的是插件自己的 thumb_dir：它指到 config 目录时同样要拒。"""
        plugin = self._rogue_root_containing_credentials(thumb_dir=get_config_dir())
        resp = self._get(f'/thumbs/{TOKEN_FILE_NAME}?plugin={plugin}')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(self.token, resp.get_data(as_text=True))

    @unittest.skipUnless(os.name == 'nt', 'NTFS 大小写不敏感，此用例只在 Windows 上有意义')
    def test_token_file_case_variants_are_403(self):
        """NTFS 上 AUTH_TOKEN.TXT 与 auth_token.txt 是同一个文件，比对必须归一化。"""
        plugin = self._rogue_root_containing_credentials()
        upper = str(self._token_path()).upper()
        resp = self._get(f'/file?path={quote(upper)}&plugin={plugin}')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(self.token, resp.get_data(as_text=True))

    def test_normal_files_in_a_rogue_root_still_serve(self):
        """只保护凭据：同一个根里的普通文件不受影响（不搞系统路径黑名单）。"""
        plugin = self._rogue_root_containing_credentials()
        config_dir = get_config_dir()
        ordinary = config_dir / 'ordinary.txt'
        ordinary.write_text('ORDINARY', encoding='utf-8')
        self.addCleanup(lambda: ordinary.unlink(missing_ok=True))
        resp = self._get(f'/file?path={quote(str(ordinary))}&plugin={plugin}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'ORDINARY')

    def test_extended_prefix_root_does_not_bypass_protection(self):
        r"""`\\?\` 扩展前缀形态必须同样被拒（曾是一个完整绕过）。

        `resolve()` 在 Windows 上保留 `\\?\` 前缀，所以"根用扩展形式申报 +
        请求也用扩展形式"能让纯词法比较既不相等也不互相包含 —— 实测过同一文件
        扩展形式返回 200 + 令牌明文。归一化在 protected_paths 里统一做，
        这里端到端锁住。
        """
        ext_root = '\\\\?\\' + str(get_config_dir())
        self.manager._instances['ext'] = _StubInstance([ext_root])
        token_path = self._token_path()
        self.assertTrue(token_path.exists(), '前置条件：令牌文件已生成')
        ext_target = '\\\\?\\' + str(token_path)
        resp = self._get(f'/file?path={quote(ext_target)}&plugin=ext')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(self.token, resp.get_data(as_text=True))
        # 对照组：同一个扩展根下的普通文件仍然可读，归一化不能把正常访问一起挡掉
        ordinary = get_config_dir() / 'ordinary-ext.txt'
        ordinary.write_text('ORDINARY', encoding='utf-8')
        self.addCleanup(lambda: ordinary.unlink(missing_ok=True))
        ext_ordinary = '\\\\?\\' + str(ordinary)
        resp = self._get(f'/file?path={quote(ext_ordinary)}&plugin=ext')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'ORDINARY')

    def test_thumbs_plugin_bytes_cannot_return_protected_file(self):
        """`get_thumb_data()` 直接返回字节的分支也必须先过受保护判定。

        原实现把这个判定放在散文件回退分支里，于是"插件自己读文件并返回字节"
        那条路整条绕过了防护 —— 判定必须早于任何插件代码执行。
        """

        class _ByteThumbInstance(_StubInstance):
            def get_thumb_data(self, rel_path):
                return b'LEAKED-TOKEN', 'image/jpeg'

        self.manager._instances['bytes'] = _ByteThumbInstance(
            [get_config_dir()], thumb_dir=get_config_dir())
        resp = self._get(f'/thumbs/{TOKEN_FILE_NAME}?plugin=bytes')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('LEAKED-TOKEN', resp.get_data(as_text=True))

    def test_plugin_frontend_route_refuses_protected_file(self):
        """插件前端路由免令牌，但同样不能成为受保护文件的出口。"""
        plugin_dir = Path(self._tmp.name) / 'plugins' / 'frontend-plugin'
        frontend = plugin_dir / 'frontend'
        frontend.mkdir(parents=True)
        secret = frontend / 'credentials.json'
        secret.write_text('{"refresh_token": "SUPER-SECRET"}', encoding='utf-8')
        self.manager._plugin_dirs['frontend-plugin'] = plugin_dir
        self.manager._instances['frontend-plugin'] = _StubInstance(
            [plugin_dir], protected=[secret], data_root=plugin_dir)

        resp = self._get('/plugins/frontend-plugin/frontend/credentials.json', headers={})
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('SUPER-SECRET', resp.get_data(as_text=True))
        # 对照组：同一个前端目录里的普通资源仍然免令牌可取
        (frontend / 'app.js').write_text('// app', encoding='utf-8')
        resp = self._get('/plugins/frontend-plugin/frontend/app.js', headers={})
        self.assertEqual(resp.status_code, 200)


class PluginDeclaredProtectionTests(_FileRouteFixture, unittest.TestCase):
    """插件"申报"受保护路径、Shell 执行：即设置项声明 `"secret": True` 的落地链路。

    与上面一组的区别：上一组验证壳**自己**的凭据文件不可端出（硬编码，不可协商），
    这一组验证**插件申报**的路径同样生效，且申报越界会被忽略而不是被信任。
    """

    def _plugin_declaring(self, protected, file_roots=None, data_root=None):
        """注册一个申报了 protected 的插件，返回它的名字。"""
        self.manager._instances['declaring'] = _StubInstance(
            file_roots if file_roots is not None else [self.data_root],
            protected=protected, data_root=data_root)
        return 'declaring'

    def test_declared_file_is_403(self):
        """插件的设置文件（refresh_token 就存在那里）落在自己的根内时也必须 403。"""
        secret = self.data_root / 'plugin-settings.json'
        secret.write_text('{"refresh_token": "SUPER-SECRET"}', encoding='utf-8')
        plugin = self._plugin_declaring([secret])
        resp = self._get(f'/file?path={quote(str(secret))}&plugin={plugin}')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('SUPER-SECRET', resp.get_data(as_text=True))

    def test_declared_directory_protects_everything_under_it(self):
        """申报目录 = 该目录及其下全部内容都受保护。"""
        vault = self.data_root / 'vault'
        vault.mkdir()
        nested = vault / 'nested' / 'token.json'
        nested.parent.mkdir()
        nested.write_text('SUPER-SECRET', encoding='utf-8')
        plugin = self._plugin_declaring([vault])
        resp = self._get(f'/file?path={quote(str(nested))}&plugin={plugin}')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('SUPER-SECRET', resp.get_data(as_text=True))

    def test_out_of_bounds_declaration_is_ignored(self):
        """申报自己数据根与配置目录之外的路径 → 被忽略（防误用），文件照常提供。

        否则一个写错的申报（例如声明了盘符根）就能把整个文件服务钉死。
        这里刻意让"文件根"比"数据根"宽：该文件本来就在允许范围内，所以它仍然是
        200 只可能是因为申报被忽略了。
        """
        outside = self.outside  # <tmp>/secret.txt：在文件根内，但不在插件数据根内
        plugin = self._plugin_declaring(
            [outside], file_roots=[Path(self._tmp.name)], data_root=self.data_root)
        resp = self._get(f'/file?path={quote(str(outside))}&plugin={plugin}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'TOP-SECRET')


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
