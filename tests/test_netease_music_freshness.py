"""netease-music 远端来源的缓存与新鲜期：地址缓存有效期、登录态失效、状态投影。

三件事各自独立的判据：
  * 播放地址是短期外链，缓存条目带写入时刻，超过 `URL_CACHE_TTL_SECONDS` 重新解析；
  * 登录态变化（另一个账号）让远端缓存整体失效，重复的"已登录"不重复丢弃；
  * 新鲜期按引擎的 `fresh_until` / `ttl_remaining` 给出，旧引擎缺 `fresh_until` 时折算。

运行：
    python -m unittest tests.test_netease_music_freshness -v
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BACKEND_DIR = PROJECT_ROOT / 'plugins' / 'netease-music' / 'backend'
REMOTE_CACHE_MODULE = 'netease_music_remote_cache'


def _load(name: str):
    path = BACKEND_DIR / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'{name}_far_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin_class():
    return _load('main').NeteaseMusicPlugin


class _StubRunner:
    def run(self, cmd, timeout=30, env=None):
        return {'success': True, 'stdout': '', 'stderr': '', 'returncode': 0}


class _StubParser:
    def json(self, text):
        return {}


class _StubFakeMpv:
    """假 mpv：记录被调用次数，返回固定地址。"""

    def __init__(self, url='https://fresh.example/song.mp3'):
        self.url = url
        self.calls = 0

    def prepare(self):
        self.calls += 1

    def environment(self):
        return {}

    def wait_url(self, timeout=15):
        return self.url


class UrlCacheTtlTests(unittest.TestCase):
    """地址缓存的有效期：新鲜条目直接用，过期条目重新解析。"""

    def _api(self):
        module = _load('netease_music_api')
        api = module.NeteaseMusicAPI.__new__(module.NeteaseMusicAPI)
        api._url_cache = {}
        api._url_cache_size = 4
        api._runner = _StubRunner()
        api._parser = _StubParser()
        api._fake_mpv = _StubFakeMpv()
        return api

    def test_ttl_is_bounded(self):
        api = self._api()
        self.assertGreater(api.URL_CACHE_TTL_SECONDS, 0)
        self.assertLessEqual(api.URL_CACHE_TTL_SECONDS, 600.0)

    def test_fresh_entry_is_reused_without_resolving(self):
        api = self._api()
        api._url_cache['sid'] = ('https://cached.example/song.mp3', time.time())

        url = api.resolve_song_url('sid', 'orig')

        self.assertEqual(url, 'https://cached.example/song.mp3')
        self.assertEqual(api._fake_mpv.calls, 0, '新鲜地址不应再起一次解析')

    def test_expired_entry_is_resolved_again(self):
        api = self._api()
        api._url_cache['sid'] = ('https://stale.example/song.mp3',
                                 time.time() - api.URL_CACHE_TTL_SECONDS - 1)

        url = api.resolve_song_url('sid', 'orig')

        self.assertEqual(url, 'https://fresh.example/song.mp3')
        self.assertEqual(api._fake_mpv.calls, 1)
        stored, stored_at = api._url_cache['sid']
        self.assertEqual(stored, 'https://fresh.example/song.mp3')
        self.assertGreater(stored_at, time.time() - 5)

    def test_clear_url_cache_reports_dropped_entries(self):
        api = self._api()
        api._url_cache['a'] = ('https://a.example/song.mp3', time.time())
        api._url_cache['b'] = ('https://b.example/song.mp3', time.time())

        self.assertEqual(api.clear_url_cache(), 2)
        self.assertEqual(api._url_cache, {})


class _ApiStub:
    """数据接口替身：登录判定、登录与地址缓存丢弃。"""

    def __init__(self, *, logged_in=True, login_message='qr://login'):
        self.logged_in = logged_in
        self.login_message = login_message
        self.clears = 0

    def clear_url_cache(self):
        self.clears += 1
        return 3

    def check_login(self):
        return self.logged_in

    def login(self, background=True):
        return self.login_message

    def _run_command(self, cmd, timeout=30, env=None):
        return {'success': True, 'stdout': '1.2.3', 'stderr': ''}


class _PluginFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_root = Path(self._tmp.name)
        self.plugin = _plugin_class()(
            {'name': 'netease-music'}, {'directories': {'data_root': str(self.data_root)}})
        self.api = _ApiStub()
        self.plugin._api = self.api


class IdentityInvalidationTests(_PluginFixture):
    """登录态变化作废远端缓存：只在"变成已登录"那一次丢弃。"""

    def test_check_login_invalidates_once_per_transition(self):
        self.assertTrue(self.plugin.check_login()['success'])
        self.assertEqual(self.api.clears, 1)
        self.assertTrue(self.plugin.check_login()['success'])
        self.assertEqual(self.api.clears, 1, '同一登录态重复查询不应重复丢弃')

    def test_logged_out_then_in_again_invalidates_again(self):
        self.assertTrue(self.plugin.check_login()['success'])
        self.api.logged_in = False
        self.assertFalse(self.plugin.check_login()['success'])
        self.api.logged_in = True
        self.assertTrue(self.plugin.check_login()['success'])
        self.assertEqual(self.api.clears, 2)

    def test_login_invalidates_remote_cache(self):
        result = self.plugin.login(background=False)

        self.assertTrue(result['success'])
        self.assertEqual(self.api.clears, 1)
        # 登录流程已作废一次，随后的登录态查询不再重复
        self.plugin.check_login()
        self.assertEqual(self.api.clears, 1)

    def test_invalidate_reports_reason(self):
        result = self.plugin.remote_cache.invalidate('identity')

        self.assertEqual(result['reason'], 'identity')
        self.assertEqual(result['cleared'], {'url_cache': 3})

    def test_invalidate_passes_reason_to_hook(self):
        """失效原因要真的到插件钩子上（引擎按 `ttl` / `verify` / `identity` 区分路径）。"""
        seen = []

        def record(reason=''):
            seen.append(reason)
            return {'url_cache': 0}

        self.plugin._invalidate_caches = record
        self.plugin.remote_cache.invalidate('identity')

        self.assertEqual(seen, ['identity'])

    def test_failed_check_login_does_not_invalidate(self):
        self.plugin._api = type('_Boom', (), {
            'check_login': lambda self: (_ for _ in ()).throw(RuntimeError('boom'))})()

        result = self.plugin.check_login()

        self.assertFalse(result['success'])
        self.assertIn('boom', result['error'])


class RemoteFreshnessStateTests(_PluginFixture):
    """新鲜期投影：模式、TTL 与到期时刻；不假设 version_stale。"""

    def test_state_reports_ttl_and_fresh_until(self):
        from shell.backend.freshness import engine_for

        engine = engine_for(self.plugin)
        self.assertIsNotNone(engine)
        engine.verify()   # 远端来源：立刻丢弃并重落时间戳

        state = self.plugin.remote_cache.state()

        self.assertEqual(state['mode'], 'remote')
        self.assertAlmostEqual(state['ttl_seconds'], self.plugin.NETEASE_TTL_SECONDS)
        self.assertGreater(state['ttl_remaining'], 0.0)
        self.assertAlmostEqual(state['fresh_until'],
                               time.time() + state['ttl_remaining'], delta=5.0)
        self.assertNotIn('version_stale', state)

    def test_state_falls_back_when_engine_has_no_fresh_until(self):
        module = sys.modules[REMOTE_CACHE_MODULE]

        class _LegacyEngine:
            def state(self):
                return {'mode': 'remote', 'ttl_seconds': 1800.0,
                        'ttl_remaining': 900.0, 'last_sync_at': 1000.0}

        original = module.engine_for
        legacy = _LegacyEngine()

        def fake_engine(_plugin):
            return legacy

        module.engine_for = fake_engine
        try:
            state = self.plugin.remote_cache.state()
        finally:
            module.engine_for = original

        self.assertEqual(state['ttl_remaining'], 900.0)
        self.assertEqual(state['fresh_until'], 2800.0, '缺 fresh_until 时按上次同步 + TTL 折算')

    def test_get_status_carries_freshness(self):
        self.plugin.remote_cache.invalidate()

        status = self.plugin.get_status()

        self.assertTrue(status['ncm_cli_available'])
        self.assertEqual(status['freshness']['mode'], 'remote')
        self.assertIn('fresh_until', status['freshness'])
        self.assertIn('ttl_remaining', status['freshness'])
        self.assertNotIn('version_stale', status['freshness'])
        json.dumps(status, ensure_ascii=False)


class TtlSemanticsTests(_PluginFixture):
    """两条时间窗不是同一个数：远端新鲜期半小时，播放地址有效期十分钟。"""

    def test_freshness_ttl_is_the_spec_window(self):
        self.assertEqual(self.plugin.freshness_spec()['ttl_seconds'],
                         self.plugin.NETEASE_TTL_SECONDS)

    def test_url_cache_ttl_is_not_the_freshness_window(self):
        api = _load('netease_music_api').NeteaseMusicAPI
        self.assertLessEqual(api.URL_CACHE_TTL_SECONDS, self.plugin.NETEASE_TTL_SECONDS,
                             '播放地址的有效期不能长于远端数据的新鲜期')
        self.assertLess(api.URL_CACHE_TTL_SECONDS, self.plugin.NETEASE_TTL_SECONDS,
                        '两个 TTL 不是同一个数，注释与常量要能对上')


class RemoteCacheFallbackTests(_PluginFixture):
    """引擎忙/异常时绕过引擎直接丢缓存，但新鲜期必须跟着重新起算。"""

    def test_busy_engine_fallback_still_restarts_the_freshness_window(self):
        from shell.backend.freshness import engine_for

        engine = engine_for(self.plugin)
        self.assertIsNotNone(engine)
        # 真实场景：另一趟同步/校验正在跑，远端那一趟被单飞挡掉
        engine.sync = lambda *args, **kwargs: {'action': 'skip', 'reason': 'busy'}

        before = time.time()
        result = self.plugin.remote_cache.invalidate('identity')

        self.assertEqual(result['reason'], 'identity')
        self.assertEqual(result['cleared'], {'url_cache': 3}, '兜底也要真的丢缓存')
        self.assertEqual(result['engine'], 'busy')

        state = self.plugin.remote_cache.state()
        self.assertGreater(state['ttl_remaining'], 0.0, '新鲜期从这次失效重新起算')
        self.assertGreaterEqual(state['fresh_until'],
                                before + self.plugin.NETEASE_TTL_SECONDS - 5,
                                'fresh_until 不能还停在失效之前的旧时刻')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
