"""应用内检查更新的后端自检：版本解析、形态判定、检查分支、下载与校验、注册守卫。

为什么这些断言值得留着：

  * `parse_version` / `install_form` 是全部后续判断的坐标原点 —— 版本比较错一位就会把
    "已是最新"报成"有新版"，形态判错就会给便携版一个会搬走数据的升级按钮。
  * 检查的分支全是失败路径（限流、401、404、JSON 形状变化、超时、连不上）。它们各自
    对应一句不同的界面文案，混成一个"检查失败"等于让用户无从下手；而**不抛异常**是
    硬要求，否则 `api_proxy` 会把它变成 500。
  * 下载的判据是"校验通过了才算数"：摘要不符、超上限、可疑重定向、取消，都必须留下
    一个空的 `updates/`（没有 `.part`、没有半成品），否则下次启动会有人把它当成品。

全程零外网：GitHub API 与资产都由本地假 HTTP 服务扮演，下载上限与超时由
`UpdateTransport` 调小。

运行：
    python -m unittest tests.test_update_check -v
"""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend import update_check
from shell.backend.file_server import _ADMIN_ONLY_API
from tests.test_main_startup import _load_main, _StubManager

#: 假安装器 / 假 zip 的字节内容：只求"字节一致"，不求像真的安装包。
SETUP_BODY = b'cubby-setup-payload-' * 8
ZIP_BODY = b'cubby-zip-payload-' * 4

#: 检查与下载用的短超时：用例不该等 15 秒。
SHORT_TIMEOUT: Tuple[float, float] = (0.3, 0.3)


# ===== 假 HTTP 服务 =====

def _write(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    """写响应体；客户端已经放弃（超时用例）时安静收场。"""
    try:
        handler.wfile.write(body)
        handler.wfile.flush()
    except OSError:
        handler.close_connection = True


def _json_route(payload: Any, *, status: int = 200,
                headers: Optional[Dict[str, str]] = None) -> Callable:
    body = json.dumps(payload).encode('utf-8')

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        _write(handler, body)

    return respond


def _raw_route(body: bytes, *, status: int = 200, content_length: Optional[int] = None,
               headers: Optional[Dict[str, str]] = None) -> Callable:
    declared = len(body) if content_length is None else content_length

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/octet-stream')
        handler.send_header('Content-Length', str(declared))
        for name, value in (headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        if body:
            _write(handler, body)

    return respond


def _stream_route(chunks: List[bytes],
                  gate: Optional[threading.Event] = None) -> Callable:
    """无 Content-Length 的流式响应：连接关闭即正文结束（HTTP/1.0 语义）。

    `gate` 用来把"响应进行到一半"固定住：第一块写完就等它，用例据此在下载途中取消
    或发起第二次 `start_download`。读到下一块时 worker 才会检查取消标记，所以至少
    要有两块。
    """

    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/octet-stream')
        handler.send_header('Connection', 'close')
        handler.end_headers()
        try:
            for chunk in chunks:
                handler.wfile.write(chunk)
                handler.wfile.flush()
                if gate is not None:
                    gate.wait(10.0)
        except OSError:
            return
        handler.close_connection = True

    return respond


def _redirect_route(location: str, *, status: int = 302) -> Callable:
    def respond(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        handler.send_header('Location', location)
        handler.send_header('Content-Length', '0')
        handler.end_headers()

    return respond


def _slow_json_route(payload: Any, delay: float) -> Callable:
    """先睡一会儿再回话：用来触发读超时（连接是通的，只是不回）。"""

    def respond(handler: BaseHTTPRequestHandler) -> None:
        time.sleep(delay)
        body = json.dumps(payload).encode('utf-8')
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        _write(handler, body)

    return respond


class _Handler(BaseHTTPRequestHandler):
    """按路径表分派的假服务。

    `routes` 与 `seen` 挂在类上：handler 实例是每个请求一个新的，跨请求的状态只能
    放在类上；每个用例在 `setUp` 里清空它们。
    """

    routes: ClassVar[Dict[str, Callable]] = {}
    seen: ClassVar[List[Dict[str, Any]]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split('?')[0]
        type(self).seen.append({'path': path, 'headers': dict(self.headers)})
        route = type(self).routes.get(path)
        if route is None:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        route(self)


class _FakeHTTPServer(ThreadingHTTPServer):
    """客户端提前断开（超时 / 取消）是这些用例的预期行为，不必打整栈。"""

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class _FakeServer:
    """监听 `127.0.0.1` 随机端口的假服务。"""

    def __init__(self) -> None:
        self.httpd = _FakeHTTPServer(('127.0.0.1', 0), _Handler)
        # 默认 0.5 秒的轮询间隔会让每个用例的 `shutdown()` 多等半秒，几十个用例叠加起来
        # 就是十几秒；这里只影响"多久看一眼停止标志"。
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _free_port() -> int:
    """拿一个"没人监听"的端口：连上去应当是 ConnectionError。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _transport(server: _FakeServer, *, timeouts: Tuple[float, float] = SHORT_TIMEOUT,
               max_bytes: int = update_check.MAX_UPDATE_BYTES,
               allowed_schemes: Tuple[str, ...] = ('http', 'https'),
               allowed_hosts: Tuple[str, ...] = ('127.0.0.1',),
               api_base: Optional[str] = None) -> update_check.UpdateTransport:
    return update_check.UpdateTransport(
        api_base=api_base or server.base,
        api_timeout=timeouts,
        download_timeout=timeouts,
        max_bytes=max_bytes,
        allowed_schemes=allowed_schemes,
        allowed_hosts=allowed_hosts,
    )


def _sha_line(body: bytes, name: str) -> bytes:
    return f'{hashlib.sha256(body).hexdigest()} {name}\n'.encode('utf-8')


def _setup_name(version: str = '1.5.0') -> str:
    return f'Cubby-Setup-{version}.exe'


def _release_payload(base: str, *, tag: str = 'v1.5.0', setup_body: bytes = SETUP_BODY,
                     zip_body: bytes = ZIP_BODY, setup_size: Optional[int] = None,
                     include_setup: bool = True, include_zip: bool = True,
                     include_setup_sha: bool = True,
                     html_url: str = '') -> Dict[str, Any]:
    """一份形状正常的发布信息；各开关用来造"缺资产 / 缺 .sha256 / 尺寸超限"。"""
    version = tag.removeprefix('v')
    setup = _setup_name(version)
    assets: List[Dict[str, Any]] = []
    if include_setup:
        assets.append({
            'name': setup,
            'size': len(setup_body) if setup_size is None else setup_size,
            'browser_download_url': f'{base}/dl/{setup}',
        })
    if include_zip:
        assets.append({
            'name': update_check.ZIP_ASSET_NAME,
            'size': len(zip_body),
            'browser_download_url': f'{base}/dl/{update_check.ZIP_ASSET_NAME}',
        })
    if include_setup_sha:
        assets.append({
            'name': setup + '.sha256',
            'size': len(_sha_line(setup_body, setup)),
            'browser_download_url': f'{base}/dl/{setup}.sha256',
        })
        assets.append({
            'name': update_check.ZIP_ASSET_NAME + '.sha256',
            'size': len(_sha_line(zip_body, update_check.ZIP_ASSET_NAME)),
            'browser_download_url': f'{base}/dl/{update_check.ZIP_ASSET_NAME}.sha256',
        })
    return {
        'tag_name': tag,
        'html_url': html_url or f'https://github.com/Flotiarenor/Cubby/releases/tag/{tag}',
        'assets': assets,
    }


def _header(entry: Dict[str, Any], name: str) -> str:
    for key, value in entry['headers'].items():
        if key.lower() == name.lower():
            return str(value)
    return ''


# ===== 纯函数 =====


class ParseVersionTests(unittest.TestCase):
    def test_accepts_three_segment_versions(self):
        self.assertEqual(update_check.parse_version('1.2.3'), (1, 2, 3))
        self.assertEqual(update_check.parse_version('v1.2.3'), (1, 2, 3))
        self.assertEqual(update_check.parse_version(' 1.2.3 '), (1, 2, 3))
        self.assertEqual(update_check.parse_version('0.0.0'), (0, 0, 0))

    def test_strips_only_one_leading_v(self):
        """两个 `v` 不是合法 tag：宁可判不合法，也不要猜一个版本出来比较。"""
        self.assertIsNone(update_check.parse_version('vv1.2.3'))

    def test_rejects_other_shapes(self):
        for value in ('1.2', '1.2.3.4', '1.2.3-beta', 'nightly', 'dev', '', 'v', 'x.y.z'):
            with self.subTest(value=value):
                self.assertIsNone(update_check.parse_version(value))

    def test_rejects_non_strings(self):
        for value in (None, 123, 1.23, ['1.2.3'], {'version': '1.2.3'}, True):
            with self.subTest(value=value):
                self.assertIsNone(update_check.parse_version(value))


class InstallFormTests(unittest.TestCase):
    def test_non_windows_is_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                update_check.install_form(frozen=True, portable=False,
                                          install_dir=Path(tmp), platform='linux'),
                'platform')

    def test_unfrozen_is_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                update_check.install_form(frozen=False, portable=True,
                                          install_dir=Path(tmp), platform='win32'),
                'dev')

    def test_portable_marker_wins_over_uninstaller(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / update_check.UNINSTALLER_NAME).write_text('', encoding='utf-8')
            self.assertEqual(
                update_check.install_form(frozen=True, portable=True,
                                          install_dir=Path(tmp), platform='win32'),
                'portable')

    def test_uninstaller_file_means_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / update_check.UNINSTALLER_NAME).write_text('', encoding='utf-8')
            self.assertEqual(
                update_check.install_form(frozen=True, portable=False,
                                          install_dir=Path(tmp), platform='win32'),
                'installer')

    def test_uninstaller_directory_is_not_installer(self):
        """判据要的是"安装器写过这个文件"；同名目录说明不了，也不能当程序启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / update_check.UNINSTALLER_NAME).mkdir()
            self.assertEqual(
                update_check.install_form(frozen=True, portable=False,
                                          install_dir=Path(tmp), platform='win32'),
                'zip')

    def test_empty_dir_is_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                update_check.install_form(frozen=True, portable=False,
                                          install_dir=Path(tmp), platform='win32'),
                'zip')


class ParseSha256Tests(unittest.TestCase):
    def test_parses_digest_and_name(self):
        line = _sha_line(b'data', 'Cubby-Setup-1.5.0.exe')
        digest, name = update_check.parse_sha256(line.decode('utf-8'))
        self.assertEqual(digest, hashlib.sha256(b'data').hexdigest())
        self.assertEqual(name, 'Cubby-Setup-1.5.0.exe')

    def test_accepts_binary_marker_and_upper_case(self):
        digest = hashlib.sha256(b'data').hexdigest().upper()
        parsed = update_check.parse_sha256(f'{digest} *Cubby.zip\n')
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[1], 'Cubby.zip')

    def test_rejects_other_shapes(self):
        for text in ('', '   ', 'not-a-digest Cubby.zip', 'a' * 63 + ' Cubby.zip',
                     'a' * 64, 'a' * 64 + ' Cubby.zip\nextra line'):
            with self.subTest(text=text[:24]):
                self.assertIsNone(update_check.parse_sha256(text))


# ===== 检查 =====


class _ServerTest(unittest.TestCase):
    """起一个假服务 + 一个临时用户数据根；每个用例都从干净状态开始。"""

    def setUp(self) -> None:
        _Handler.routes = {}
        _Handler.seen = []
        self.server = _FakeServer()
        self.addCleanup(self.server.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.updates = self.home / update_check.UPDATE_DIRNAME
        # 数据根必须隔离：`_cleanup_stale_parts()` 在服务构造时就会 glob `updates/`。
        patch = mock.patch.object(update_check, 'get_user_data_dir', lambda: self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def serve_release(self, payload: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        body = payload if payload is not None else _release_payload(self.server.base, **kwargs)
        _Handler.routes[update_check.RELEASES_PATH] = _json_route(body)

    def wait_settled(self, timeout: float = 5.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.service.state()['job']
            if job['state'] not in ('downloading', 'verifying'):
                return job
            time.sleep(0.01)
        self.fail(f'下载没有在 {timeout} 秒内结束: {self.service.state()["job"]}')
        raise AssertionError('不可达：self.fail 不返回')

    def served_paths(self) -> List[str]:
        return [entry['path'] for entry in _Handler.seen]


class CheckTests(_ServerTest):
    def setUp(self) -> None:
        super().setUp()
        self.form = 'installer'
        patch = mock.patch.object(update_check, 'current_form', lambda: self.form)
        patch.start()
        self.addCleanup(patch.stop)
        self.service = update_check.UpdateService(_transport(self.server))
        self.serve_release()

    def test_update_available(self):
        result = self.service.check('1.4.0')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['code'], 'update_available')
        self.assertEqual(result['current'], '1.4.0')
        self.assertEqual(result['latest'], '1.5.0')
        self.assertEqual(result['form'], 'installer')
        self.assertTrue(result['download_allowed'])
        self.assertEqual(result['asset'], {'name': 'Cubby-Setup-1.5.0.exe', 'size': len(SETUP_BODY)})
        self.assertEqual(result['retry_after_seconds'], None)
        self.assertEqual(result['message'], '')
        self.assertFalse(result['cached'])
        self.assertIsInstance(result['checked_at'], int)
        # 信封不许带下载地址：那会让前端有机会把 URL 改成任意值再回传
        self.assertNotIn('/dl/', json.dumps(result))

    def test_same_version_is_up_to_date(self):
        self.serve_release(tag='v1.4.0')
        result = self.service.check('1.4.0')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['code'], 'up_to_date')
        self.assertIsNone(result['asset'])

    def test_remote_lower_does_not_offer_a_downgrade(self):
        self.serve_release(tag='v1.3.9')
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'up_to_date')
        self.assertIsNone(result['asset'])

    def test_portable_form_only_hints(self):
        self.form = 'portable'
        result = self.service.check('1.4.0')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['code'], 'update_available')
        self.assertEqual(result['form'], 'portable')
        self.assertFalse(result['download_allowed'])
        # 便携形态要手动解压，仍然要告诉用户该解压哪个包
        self.assertEqual(result['asset']['name'], update_check.ZIP_ASSET_NAME)

    def test_dev_form_is_disabled_without_any_request(self):
        self.form = 'dev'
        result = self.service.check('1.4.0')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'disabled')
        self.assertEqual(_Handler.seen, [], '开发模式不该发出任何请求')

    def test_missing_installer_asset_is_no_asset(self):
        self.serve_release(include_setup=False)
        result = self.service.check('1.4.0')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'no_asset')
        self.assertIsNone(result['asset'])
        self.assertFalse(result['download_allowed'])

    def test_missing_sha256_asset_is_no_asset(self):
        """没有 .sha256 就无法证明文件完整，等同"没有资产"。"""
        self.serve_release(include_setup_sha=False)
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'no_asset')
        self.assertFalse([p for p in self.served_paths() if p.startswith('/dl/')])

    def test_rate_limit_by_403_with_zero_remaining(self):
        reset = int(time.time()) + 120
        _Handler.routes[update_check.RELEASES_PATH] = _json_route(
            {'message': 'API rate limit exceeded'},
            status=403,
            headers={'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(reset)},
        )
        result = self.service.check('1.4.0')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'rate_limited')
        self.assertIsInstance(result['retry_after_seconds'], int)
        self.assertGreater(result['retry_after_seconds'], 0)

    def test_rate_limit_by_429_prefers_retry_after(self):
        _Handler.routes[update_check.RELEASES_PATH] = _json_route(
            {'message': 'slow down'}, status=429, headers={'Retry-After': '37'},
        )
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'rate_limited')
        self.assertEqual(result['retry_after_seconds'], 37)

    def test_401_is_forbidden(self):
        _Handler.routes[update_check.RELEASES_PATH] = _json_route(
            {'message': 'Bad credentials'}, status=401)
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'forbidden')

    def test_404_is_not_found(self):
        _Handler.routes.pop(update_check.RELEASES_PATH)
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'not_found')
        self.assertEqual(result['html_url'], update_check.RELEASES_PAGE_URL)

    def test_json_shape_changes_are_bad_response_without_raising(self):
        cases = [
            {},
            {'tag_name': 123, 'assets': []},
            {'tag_name': 'v1.5.0', 'assets': {}},
            {'tag_name': None, 'assets': []},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                # 换一份新服务状态，避免 60 秒缓存把第二次请求吞掉
                self.service = update_check.UpdateService(_transport(self.server))
                _Handler.routes[update_check.RELEASES_PATH] = _json_route(payload)
                result = self.service.check('1.4.0')
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'], 'bad_response')

    def test_non_json_body_is_bad_response(self):
        _Handler.routes[update_check.RELEASES_PATH] = _raw_route(b'<html>oops</html>')
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'bad_response')

    def test_unparseable_tag_is_bad_version(self):
        self.serve_release(tag='nightly')
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'bad_version')
        self.assertIsNone(result['latest'])

    def test_unparseable_current_version_is_bad_version(self):
        result = self.service.check('dev')
        self.assertEqual(result['code'], 'bad_version')
        self.assertEqual(_Handler.seen, [])

    def test_connection_error_is_network(self):
        port = _free_port()
        service = update_check.UpdateService(
            _transport(self.server, api_base=f'http://127.0.0.1:{port}'))
        result = service.check('1.4.0')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'network')
        self.assertEqual(result['latest'], None)

    def test_read_timeout_is_network(self):
        _Handler.routes[update_check.RELEASES_PATH] = _slow_json_route({}, 0.8)
        service = update_check.UpdateService(
            _transport(self.server, timeouts=(0.5, 0.1)))
        result = service.check('1.4.0')
        self.assertEqual(result['code'], 'network')

    def test_result_is_reused_for_sixty_seconds(self):
        first = self.service.check('1.4.0')
        second = self.service.check('1.4.0')
        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'], '第二次请求应当复用缓存结果')
        self.assertEqual(second['code'], first['code'])
        self.assertEqual(len(_Handler.seen), 1, '60 秒内不该发第二次请求')

    def test_request_path_and_headers(self):
        self.service.check('1.4.0')
        self.assertEqual(len(_Handler.seen), 1)
        entry = _Handler.seen[0]
        self.assertEqual(entry['path'], update_check.RELEASES_PATH)
        self.assertEqual(
            _header(entry, 'User-Agent'),
            'Cubby/1.4.0 (+https://github.com/Flotiarenor/Cubby)',
        )
        self.assertIn('github', _header(entry, 'Accept'))

    def test_user_agent_cannot_be_injected_through_the_version_string(self):
        """版本串来自前端：带换行会让请求头构造失败，必须先收敛。"""
        result = self.service.check('1.4.0\r\nX-Evil: 1')
        # 版本本身不可解析 → bad_version，且没有任何请求发出
        self.assertEqual(result['code'], 'bad_version')
        self.assertEqual(_Handler.seen, [])


# ===== 下载 =====


class DownloadTests(_ServerTest):
    def setUp(self) -> None:
        super().setUp()
        self.form = 'installer'
        patch = mock.patch.object(update_check, 'current_form', lambda: self.form)
        patch.start()
        self.addCleanup(patch.stop)
        self.service = update_check.UpdateService(_transport(self.server))

    def setUpDownload(self, *, body: bytes = SETUP_BODY,
                      digest_body: Optional[bytes] = None,
                      gate: Optional[threading.Event] = None,
                      max_bytes: int = update_check.MAX_UPDATE_BYTES,
                      setup_size: Optional[int] = None) -> None:
        if max_bytes != update_check.MAX_UPDATE_BYTES:
            self.service = update_check.UpdateService(
                _transport(self.server, max_bytes=max_bytes))
        self.serve_release(setup_size=setup_size)
        self.serve_asset(_setup_name(), body, digest_body=digest_body, gate=gate)

    def serve_asset(self, asset: str, body: bytes, *, digest_body: Optional[bytes] = None,
                    gate: Optional[threading.Event] = None) -> None:
        if gate is None:
            _Handler.routes[f'/dl/{asset}'] = _raw_route(body)
        else:
            half = max(1, len(body) // 2)
            _Handler.routes[f'/dl/{asset}'] = _stream_route([body[:half], body[half:]], gate)
        _Handler.routes[f'/dl/{asset}.sha256'] = _raw_route(
            digest_body if digest_body is not None else _sha_line(body, asset))

    def start(self, version: str = '1.5.0', asset: Optional[str] = None) -> Dict[str, Any]:
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'update_available', result)
        target = asset or (result['asset'] or {}).get('name', '')
        return self.service.start_download(version, target)

    def test_success_leaves_only_the_verified_file(self):
        self.setUpDownload()
        started = self.start()
        self.assertTrue(started['started'], started)
        self.assertEqual(started['version'], '1.5.0')
        self.assertEqual(started['asset'], _setup_name())
        self.assertEqual(started['total'], len(SETUP_BODY))

        job = self.wait_settled()
        self.assertEqual(job['state'], 'ready')
        self.assertEqual(job['code'], '')
        self.assertEqual(job['percent'], 100)
        final = self.updates / _setup_name()
        self.assertEqual(job['path'], str(final))
        self.assertEqual(final.read_bytes(), SETUP_BODY)
        self.assertEqual([p.name for p in self.updates.iterdir()], [_setup_name()])
        # .sha256 是单独一次 GET
        self.assertIn(f'/dl/{_setup_name()}.sha256', self.served_paths())

    def test_second_start_after_ready_reports_the_existing_file(self):
        self.setUpDownload()
        self.start()
        self.wait_settled()
        again = self.start()
        self.assertEqual(again, {'started': False, 'ready': True,
                                 'path': str(self.updates / _setup_name())})

    def test_digest_mismatch_deletes_the_download(self):
        body = SETUP_BODY
        wrong = b'0' * 64 + b' ' + _setup_name().encode('utf-8') + b'\n'
        self.setUpDownload(body=body, digest_body=wrong)
        started = self.start()
        self.assertTrue(started['started'], started)
        job = self.wait_settled()
        self.assertEqual(job['state'], 'error')
        self.assertEqual(job['code'], 'digest_mismatch')
        self.assertEqual(list(self.updates.iterdir()), [], '摘要不符不能留下半成品')

    def test_bad_sha256_shape_is_bad_response(self):
        self.setUpDownload(digest_body=b'not-a-digest\n')
        self.start()
        job = self.wait_settled()
        self.assertEqual(job['code'], 'bad_response')
        self.assertEqual(list(self.updates.iterdir()), [])

    def test_bad_sha256_filename_is_bad_response(self):
        body = SETUP_BODY
        digest_body = _sha_line(body, 'something-else.exe')
        self.setUpDownload(body=body, digest_body=digest_body)
        self.start()
        job = self.wait_settled()
        self.assertEqual(job['code'], 'bad_response')
        self.assertEqual(list(self.updates.iterdir()), [])

    def test_asset_size_over_limit_never_requests_the_asset(self):
        self.serve_release(setup_size=4096)
        self.service = update_check.UpdateService(_transport(self.server, max_bytes=1024))
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'update_available')
        started = self.service.start_download('1.5.0', _setup_name())
        self.assertEqual(started, {'started': False, 'error': 'too_large'})
        self.assertFalse([p for p in self.served_paths() if p.startswith('/dl/')],
                         '预检就该拦住，不该发起资产请求')

    def test_content_length_over_limit_aborts(self):
        self.serve_release()
        _Handler.routes[f'/dl/{_setup_name()}'] = _raw_route(b'', content_length=4096)
        _Handler.routes[f'/dl/{_setup_name()}.sha256'] = _raw_route(
            _sha_line(SETUP_BODY, _setup_name()))
        self.service = update_check.UpdateService(_transport(self.server, max_bytes=1024))
        self.start()
        job = self.wait_settled()
        self.assertEqual(job['code'], 'too_large')
        self.assertEqual(list(self.updates.iterdir()), [])

    def test_streaming_over_limit_aborts(self):
        # 尺寸预检用的是 assets[].size，这里把它设小，让"流式超限"这条分支真的被走到
        self.setUpDownload(body=b'x' * 4096, max_bytes=1024, setup_size=512)
        self.start()
        job = self.wait_settled()
        self.assertEqual(job['code'], 'too_large')
        self.assertEqual(list(self.updates.iterdir()), [])

    def test_redirect_to_disallowed_host_is_rejected(self):
        self.serve_release()
        _Handler.routes[f'/dl/{_setup_name()}'] = _redirect_route('http://evil.example.com/x.exe')
        _Handler.routes[f'/dl/{_setup_name()}.sha256'] = _raw_route(
            _sha_line(SETUP_BODY, _setup_name()))
        self.start()
        job = self.wait_settled()
        self.assertEqual(job['state'], 'error')
        self.assertEqual(job['code'], 'bad_redirect')
        self.assertEqual(list(self.updates.iterdir()), [])
        self.assertFalse([p for p in self.served_paths() if 'evil' in p],
                         '被拒的重定向不该真的被请求')

    def test_redirect_to_file_scheme_is_rejected(self):
        """自动跟随会对 file:// 抛 InvalidSchema（不是 ConnectionError），因此必须手动循环。"""
        self.serve_release()
        _Handler.routes[f'/dl/{_setup_name()}'] = _redirect_route('file:///C:/Windows/notepad.exe')
        _Handler.routes[f'/dl/{_setup_name()}.sha256'] = _raw_route(
            _sha_line(SETUP_BODY, _setup_name()))
        self.start()
        job = self.wait_settled()
        self.assertEqual(job['code'], 'bad_redirect')

    def test_redirect_to_allowed_host_is_followed(self):
        body = SETUP_BODY
        self.serve_release()
        _Handler.routes[f'/dl/{_setup_name()}'] = _redirect_route(f'{self.server.base}/cdn/setup.exe')
        _Handler.routes['/cdn/setup.exe'] = _raw_route(body)
        _Handler.routes[f'/dl/{_setup_name()}.sha256'] = _raw_route(
            _sha_line(body, _setup_name()))
        self.start()
        job = self.wait_settled()
        self.assertEqual(job['state'], 'ready', job)
        self.assertEqual((self.updates / _setup_name()).read_bytes(), body)

    def test_cancel_leaves_an_empty_directory(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.setUpDownload(body=b'z' * 512, gate=gate)
        started = self.start()
        self.assertTrue(started['started'], started)
        cancelled = self.service.cancel()
        self.assertTrue(cancelled['cancelled'], cancelled)
        gate.set()
        job = self.wait_settled()
        self.assertEqual(job['state'], 'cancelled')
        self.assertEqual(list(self.updates.iterdir()), [], '取消后不能留下半成品')
        self.assertEqual(self.service.cancel(), {'cancelled': False, 'running': False})

    def test_second_start_while_running_reports_running(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.setUpDownload(body=b'w' * 512, gate=gate)
        self.assertTrue(self.start()['started'])
        again = self.service.start_download('1.5.0', _setup_name())
        self.assertEqual(again, {'started': False, 'running': True})
        gate.set()
        job = self.wait_settled()
        self.assertEqual(job['state'], 'ready', job)

    def test_portable_form_is_not_allowed(self):
        self.form = 'portable'
        self.serve_release()
        result = self.service.check('1.4.0')
        self.assertEqual(result['code'], 'update_available')
        target = (result['asset'] or {}).get('name', '')
        self.assertEqual(self.service.start_download('1.5.0', target),
                         {'started': False, 'error': 'not_allowed'})

    def test_version_or_asset_not_matching_the_cache_is_stale(self):
        self.setUpDownload()
        self.service.check('1.4.0')
        self.assertEqual(self.service.start_download('1.4.0', _setup_name()),
                         {'started': False, 'error': 'stale'})
        self.assertEqual(self.service.start_download('1.5.0', 'other.exe'),
                         {'started': False, 'error': 'stale'})
        self.assertFalse([p for p in self.served_paths() if p.startswith('/dl/')])

    def test_without_a_check_result_there_is_no_result(self):
        self.setUpDownload()
        self.assertEqual(self.service.start_download('1.5.0', _setup_name()),
                         {'started': False, 'error': 'no_result'})

    def test_state_offers_download_for_installer_form(self):
        state = self.service.state()
        self.assertEqual(state['form'], 'installer')
        self.assertTrue(state['enabled'])
        self.assertTrue(state['download_allowed'])
        self.assertEqual(state['job']['state'], 'idle')
        self.assertEqual(state['job']['percent'], 0)

    def test_state_has_no_download_for_portable_form(self):
        self.form = 'portable'
        state = self.service.state()
        self.assertTrue(state['enabled'])
        self.assertFalse(state['download_allowed'])
        self.assertIn('数据在程序目录', state['reason'])

    def test_state_disables_development_form(self):
        self.form = 'dev'
        state = self.service.state()
        self.assertFalse(state['enabled'])
        self.assertFalse(state['download_allowed'])
        self.assertIn('开发模式', state['reason'])


class OpenUpdatesDirTests(_ServerTest):
    def test_creates_the_directory_before_opening(self):
        service = update_check.UpdateService(_transport(self.server))
        opened: List[str] = []
        with mock.patch.object(update_check.os, 'startfile', lambda path: opened.append(path),
                               create=True):
            result = service.open_updates_dir()
        self.assertEqual(result, {'success': True})
        self.assertTrue(self.updates.is_dir())
        self.assertEqual(opened, [str(self.updates)])

    def test_reports_failure_instead_of_raising(self):
        service = update_check.UpdateService(_transport(self.server))
        with mock.patch.object(update_check.os, 'startfile',
                               side_effect=OSError('没有文件管理器'), create=True):
            result = service.open_updates_dir()
        self.assertFalse(result['success'])
        self.assertIn('没有文件管理器', result['error'])


# ===== 注册与守卫 =====


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(update_check.reset_for_tests)

    def test_service_is_a_singleton_and_reset_drops_it(self):
        """两条通道都通过 `service()` 取实例：它必须是同一个，测试钩子要能换掉它。"""
        first = update_check.service()
        self.assertIs(update_check.service(), first)
        self.assertIs(update_check.api_methods()['system_check_update'].__self__, first)
        update_check.reset_for_tests()
        self.assertIsNot(update_check.service(), first)

    def test_every_update_method_is_admin_only(self):
        names = set(update_check.api_methods())
        self.assertEqual(names, {
            'system_check_update',
            'system_start_update_download',
            'system_get_update_state',
            'system_cancel_update_download',
            'system_open_update_dir',
        })
        self.assertLessEqual(names, _ADMIN_ONLY_API,
                             '这些端点会联网、写数据根、将来会启动安装器，必须只给管理员')

    def test_desktop_channel_registers_the_same_methods(self):
        """桌面模式只拿到 `shell_methods` 这一张表：漏注册就是"点了没反应"。"""
        main = _load_main()
        captured: Dict[str, Any] = {}

        def fake_run(config: Any, manager: Any, api: Any, host: str, port: int) -> None:
            captured['api'] = api

        config = {'server': {'host': '127.0.0.1', 'port': 18080},
                  'directories': {'data_root': str(PROJECT_ROOT / 'data')}}
        with mock.patch.object(main, 'ShellInfo', lambda *a, **k: mock.Mock()), \
                mock.patch.object(main, 'ShellPrefs', lambda *a, **k: mock.Mock()), \
                mock.patch.object(main, '_run_desktop_window', fake_run):
            main._run_app(config, _StubManager())

        api = captured['api']
        for name in update_check.api_methods():
            self.assertTrue(callable(getattr(api, name, None)),
                            f'桌面 js_api 缺少 {name}')


if __name__ == '__main__':
    unittest.main()
