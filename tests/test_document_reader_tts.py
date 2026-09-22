"""朗读（TTS）后端自检：切句不丢字符、三档引擎分派、缓存与降级链。

为什么这些断言值得留着：

  * `split_text` 是**字级高亮的坐标系**：前端拿到的字符偏移量全部基于切出来的片段，
    只要切分过程中 strip 掉一个空格或补一个标点，读到第三句高亮就整体错位 ——
    而且错得很隐蔽（看起来只是"高亮慢半拍"）。
  * 引擎分派与降级链是"断网也能出声"的唯一保证：edge 挂了必须落到 system，
    配了 base_url 必须真的打 `/v1/audio/speech` 并带上 Bearer。
  * 缓存键里必须含引擎+音色+语速：漏了就会出现"切了音色，听到的还是上一个声音"。

全程零网络：OpenAI 兼容那一档打的是本地临时 HTTP 服务。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'document-reader' / 'backend'
if str(PLUGIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(PLUGIN_BACKEND))

from shell.backend.plugin_utils import load_sibling

tts = load_sibling(str(PLUGIN_BACKEND / 'tts_engine.py'), 'tts_engine', 'document_reader_tts_test')

# 一个合法的最小 WAV（44 字节头 + 8 个采样），让"音频字节"这件事可验证
WAV_BYTES = (
    b'RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00'
    b'\x40\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00'
)


class SplitTextTests(unittest.TestCase):
    def test_concatenation_preserves_text_exactly(self):
        """切分必须无损：拼接回来要逐字节等于原文（高亮坐标系的基础）。"""
        samples = [
            '第一句。第二句！第三句？',
            '没有标点的长句' * 40,
            '段落一。\n\n段落二，还有逗号，和、顿号。',
            'Short. 中英混排 English words. 结尾',
            '',
            '。',
            '一整段没有任何标点也没有换行但就是很长' * 30,
        ]
        for text in samples:
            with self.subTest(text=text[:20]):
                pieces = tts.split_text(text, max_chars=120)
                self.assertEqual(''.join(pieces), text)

    def test_respects_max_chars_for_unpunctuated_text(self):
        text = '啊' * 1000
        pieces = tts.split_text(text, max_chars=240)
        self.assertTrue(pieces)
        self.assertLessEqual(max(len(p) for p in pieces), 240)
        self.assertEqual(''.join(pieces), text)

    def test_splits_at_sentence_marks(self):
        pieces = tts.split_text('今天下雨了。明天会晴吗？后天再说！', max_chars=240)
        self.assertEqual(pieces, ['今天下雨了。', '明天会晴吗？', '后天再说！'])

    def test_empty_text_yields_no_pieces(self):
        self.assertEqual(tts.split_text(''), [])
        self.assertEqual(tts.split_text(None), [])


class _OpenAIHandler(BaseHTTPRequestHandler):
    """假的 OpenAI 兼容端点：只实现 /v1/audio/speech，记录收到的请求。"""

    # 类级共享：handler 实例每个请求新建一个，跨请求的状态只能挂类上
    seen: ClassVar[list] = []
    auth_required: ClassVar[str] = ''

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        payload = json.loads(self.rfile.read(length) or b'{}')
        type(self).seen.append({'path': self.path, 'payload': payload,
                                'authorization': self.headers.get('Authorization') or ''})
        if self.path != '/v1/audio/speech':
            self.send_response(404)
            self.end_headers()
            return
        if type(self).auth_required and self.headers.get('Authorization') != type(self).auth_required:
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":"bad key"}')
            return
        if payload.get('input') == '触发失败':
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":"boom"}')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('Content-Length', str(len(WAV_BYTES)))
        self.end_headers()
        self.wfile.write(WAV_BYTES)


class _FakeEndpoint:
    def __init__(self):
        _OpenAIHandler.seen = []
        _OpenAIHandler.auth_required = ''
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _OpenAIHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.server.server_address[:2]
        return f'http://{host}:{port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class EngineDispatchTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = _FakeEndpoint()
        self.addCleanup(self.endpoint.close)
        self.base_params = {'voice': 'zh-CN-XiaoxiaoNeural', 'rate': 0, 'model': ''}

    def test_openai_engine_posts_expected_payload(self):
        params = {**self.base_params, 'mode': 'openai', 'base_url': self.endpoint.base_url,
                  'api_key': 'sk-test', 'voice': 'alloy'}
        result = tts.synthesize('今天下雨了。', params)
        self.assertEqual(result['engine'], 'openai')
        self.assertEqual(result['audio'], WAV_BYTES)
        self.assertEqual(result['ext'], '.wav')
        sent = _OpenAIHandler.seen[-1]
        self.assertEqual(sent['path'], '/v1/audio/speech')
        self.assertEqual(sent['payload']['input'], '今天下雨了。')
        self.assertEqual(sent['payload']['voice'], 'alloy')
        self.assertEqual(sent['authorization'], 'Bearer sk-test')

    def test_openai_engine_without_base_url_is_unavailable(self):
        self.assertFalse(tts.engine_available('openai', {**self.base_params, 'base_url': ''}))
        with self.assertRaises(RuntimeError):
            tts.synthesize('随便', {**self.base_params, 'mode': 'openai', 'base_url': ''})

    def test_openai_engine_rejects_non_http_endpoint(self):
        """端点地址会收到 `Authorization: Bearer <api_key>`，因此只放行 http/https。

        审计项 P1-4：该地址原先不做任何校验，改它就能把 owner 配置的云 TTS 凭据发给
        任意主机；`file://` / `ftp://` 一类还会走别的 requests 适配器。改这个地址的
        权限另由设置项的 `"admin_only": True` 限定（见 tests/test_settings_admin_only.py）。
        """
        for base in ('file:///etc/passwd', 'ftp://127.0.0.1', 'http://', 'not-a-url', '//x/y'):
            with self.subTest(base=base):
                params = {**self.base_params, 'mode': 'openai', 'base_url': base,
                          'api_key': 'sk-secret'}
                with self.assertRaises(RuntimeError):
                    tts.synthesize('随便', params)
        # 正常地址（含本地端点与带路径前缀的反代）必须继续可用
        for base in ('http://127.0.0.1:8880', 'https://api.example.com', 'http://host:1/api'):
            with self.subTest(base=base):
                self.assertEqual(tts._validated_endpoint_base(base), base)

    def test_openai_engine_surfaces_http_error(self):
        params = {**self.base_params, 'mode': 'openai', 'base_url': self.endpoint.base_url}
        with self.assertRaises(RuntimeError) as ctx:
            tts.synthesize('触发失败', params)
        self.assertIn('500', str(ctx.exception))

    def test_auto_mode_falls_back_to_next_engine(self):
        """第一个引擎失败时必须落到下一个，并把最终成功的引擎名报出来。"""
        params = {**self.base_params, 'mode': 'auto', 'order': ['openai', 'system'],
                  'base_url': self.endpoint.base_url}
        with mock.patch.object(tts, '_RUNNERS', {**tts._RUNNERS}), \
                mock.patch.dict(tts._RUNNERS, {'openai': _boom}):
            if not tts.engine_available('system', params):
                self.skipTest('本机没有系统离线音色（非 Windows）')
            result = tts.synthesize('今天下雨了。', params)
        self.assertEqual(result['engine'], 'system')

    def test_auto_mode_reports_every_failure(self):
        params = {**self.base_params, 'mode': 'auto', 'order': ['openai', 'system'],
                  'base_url': ''}
        with mock.patch.dict(tts._RUNNERS, {'openai': _boom, 'system': _boom}), \
                self.assertRaises(RuntimeError) as ctx:
            tts.synthesize('随便', params)
        message = str(ctx.exception)
        self.assertIn('openai', message)
        self.assertIn('system', message)

    def test_fixed_engine_does_not_silently_switch(self):
        """用户点名了引擎，失败就要如实报错，不能偷偷换一种声音。"""
        params = {**self.base_params, 'mode': 'openai', 'base_url': self.endpoint.base_url}
        with mock.patch.dict(tts._RUNNERS, {'openai': _boom}), self.assertRaises(RuntimeError) as ctx:
            tts.synthesize('随便', params)
        self.assertIn('boom', str(ctx.exception))

    def test_transient_failure_is_retried_before_falling_back(self):
        """同一引擎要先重试：edge 是网络调用，偶发失败不该直接降级到系统音色。

        用户侧的表现为"偶尔听到系统音色"，而偶发失败重试一次通常就好。
        """
        calls = []

        def flaky(text, params):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('第一次连接被重置')
            return _fake_audio()

        params = {**self.base_params, 'mode': 'edge'}
        with mock.patch.dict(tts._RUNNERS, {'edge': flaky}), \
                mock.patch.object(tts, 'ENGINE_ATTEMPTS', 3), \
                mock.patch.object(tts, 'RETRY_DELAY_SECONDS', 0):
            result = tts.synthesize('今天下雨了。', params)
        self.assertEqual(result['engine'], 'edge')
        self.assertEqual(len(calls), 2, '应当在同一引擎上重试一次再成功')

    def test_engine_gives_up_after_all_attempts_then_falls_back(self):
        """重试次数用尽才降级：`auto` 下最终落到下一个可用引擎。"""
        calls = []

        def always_fail(text, params):
            calls.append(1)
            raise RuntimeError('一直失败')

        params = {**self.base_params, 'mode': 'auto', 'order': ['edge', 'openai'],
                  'base_url': self.endpoint.base_url}
        with mock.patch.dict(tts._RUNNERS, {'edge': always_fail}), \
                mock.patch.object(tts, 'ENGINE_ATTEMPTS', 3), \
                mock.patch.object(tts, 'RETRY_DELAY_SECONDS', 0):
            result = tts.synthesize('今天下雨了。', params)
        self.assertEqual(result['engine'], 'openai', '重试耗尽后应降级到端点')
        self.assertEqual(len(calls), 3, f'应当重试满 3 次，实际 {len(calls)} 次')

    def test_empty_text_rejected(self):
        with self.assertRaises(ValueError):
            tts.synthesize('   ', {**self.base_params, 'mode': 'auto'})

    def test_status_lists_all_engines(self):
        data = tts.status({**self.base_params, 'base_url': ''})
        names = [item['name'] for item in data['engines']]
        self.assertEqual(names, list(tts.ENGINES))
        self.assertEqual(data['order'], list(tts.ENGINES))


def _boom(text, params):
    raise RuntimeError('boom（测试用的必失败引擎）')


def _fake_audio():
    """一个可分辨的合成结果：(音频字节, 后缀, 时间戳)。"""
    return WAV_BYTES, '.wav', [{'text': '测', 'chars': 1, 'startMs': 0.0, 'durationMs': 100.0}]


class PluginSpeakDirectTests(unittest.TestCase):
    """直接构造插件实例（不经过 PluginManager），验证缓存与错误路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.endpoint = _FakeEndpoint()
        self.addCleanup(self.endpoint.close)

        import importlib.util
        spec = importlib.util.spec_from_file_location('document_reader_main_test',
                                                     PLUGIN_BACKEND / 'main.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.module = module
        self.plugin = module.DocumentReaderPlugin(
            {'name': 'document-reader', 'dependencies': []},
            {'directories': {'data_root': self.tmp.name}},
        )
        from shell.backend.settings_store import SettingsStore
        self.plugin._settings_store = SettingsStore(str(Path(self.tmp.name) / 'settings'))
        self.plugin.update_setting('tts_engine', 'openai')
        self.plugin.update_setting('tts_base_url', self.endpoint.base_url)
        self.plugin.update_setting('tts_voice', 'alloy')
        # 缓存目录的真实位置是 <config>/plugins/<插件名>/tts；测试里把它指到临时目录，
        # 否则会往开发机的 .config 里写，也会与并行跑的用例互相干扰。
        # 给实例挂同名属性即可覆盖类方法，实例销毁后自动失效，无需还原。
        self.cache_dir = Path(self.tmp.name) / 'tts-cache'
        self.plugin._tts_cache_dir = lambda: self.cache_dir

    def test_segment_splits_and_preserves(self):
        text = '第一句。第二句！第三句？'
        result = self.plugin.tts_segment(text)
        self.assertEqual(''.join(result['segments']), text)

    def test_speak_returns_playable_url_and_caches(self):
        first = self.plugin.tts_speak('今天下雨了。', 'abc123', 0)
        self.assertNotIn('error', first)
        self.assertEqual(first['engine'], 'openai')
        self.assertIn('/file?path=', first['url'])
        self.assertFalse(first['cached'])
        self.assertEqual(len(_OpenAIHandler.seen), 1)

        # 同一段文本第二次必须命中缓存：不再打端点
        second = self.plugin.tts_speak('今天下雨了。', 'abc123', 0)
        self.assertTrue(second['cached'])
        # 两次的 URL 必须逐字相同：首次合成与命中各走一条拼路径（_tts_cache_path / _tts_hit），
        # 只把其中一条 resolve 成长名时，同一份缓存会给出两个写法不同、指向同一文件的 URL
        # （Windows 上 8.3 短名尤其明显）。
        self.assertEqual(second['url'], first['url'])
        self.assertEqual(len(_OpenAIHandler.seen), 1)

    def test_cache_key_includes_voice(self):
        """换音色必须重新合成，不能拿上一个音色的音频当缓存命中。"""
        self.plugin.tts_speak('今天下雨了。', 'abc123', 0)
        self.plugin.update_setting('tts_voice', 'echo')
        result = self.plugin.tts_speak('今天下雨了。', 'abc123', 0)
        self.assertFalse(result['cached'])
        self.assertEqual(len(_OpenAIHandler.seen), 2)

    def test_empty_cached_audio_is_regenerated(self):
        """0 字节的缓存音频不算命中：写盘写了一半被中断时，它会让这段文字永远读不出声。"""
        # 缓存键现在只接受十六进制指纹（它是文件名前缀，见 test_traversal_cache_key_*），
        # 因此这里用一个合法的十六进制串而不是可读词。
        first = self.plugin.tts_speak('今天下雨了。', 'e0decafe', 0)
        self.assertFalse(first['cached'])
        path = self._cache_audio_path('e0decafe')
        self.assertTrue(path.is_file(), f'没找到缓存音频: {path}')
        path.write_bytes(b'')                     # 模拟被中断的半成品
        again = self.plugin.tts_speak('今天下雨了。', 'e0decafe', 0)
        self.assertFalse(again['cached'], '空文件被当成了缓存命中')
        self.assertGreater(len(_OpenAIHandler.seen), 1, '空缓存应当触发重新合成')
        self.assertGreater(path.stat().st_size, 0, '重新合成后应当写回非空音频')

    def test_traversal_cache_key_cannot_escape_cache_dir(self):
        """`cache_key` 是请求参数，被当成缓存文件名前缀 —— 它必须挡住路径穿越。

        历史缺口（审计项 P1-3）：`cache_key` 只做 `[:32]` 截断，`'../../../../tmp/x'`
        就能把音频与 .json 元数据写到缓存目录之外（实测 `os.path.normpath` 落在
        `/tmp/x-<stamp>.mp3`）。修复是"只接受十六进制指纹，否则回落按文本现算"，
        外加落盘前的包含判定。
        """
        outside = Path(self.tmp.name).parent / 'omnibox-tts-traversal-marker'
        evil_key = '../' * 8 + outside.name
        result = self.plugin.tts_speak('穿越测试。', evil_key, 0)
        self.assertNotIn('error', result)
        self.assertFalse(list(Path(self.tmp.name).parent.glob(outside.name + '*')),
                         '缓存文件落到了缓存目录之外')
        # 回落按文本现算：文件名是文本 md5 前缀，且仍落在缓存目录内
        names = [p.name for p in self.cache_dir.iterdir()]
        self.assertTrue(names, f'缓存目录里没有任何文件: {list(self.cache_dir.iterdir())}')
        for name in names:
            self.assertNotIn('/', name)
            self.assertNotIn('..', name)

    def test_cache_prefix_rejects_non_hex_keys(self):
        """前缀只含十六进制与连字符：任何分隔符形态都不得进入文件名。"""
        params = {'mode': 'openai', 'voice': 'alloy', 'rate': 0, 'base_url': 'http://x'}
        for key in ('../../etc/passwd', 'a/b', 'a\\b', '..', 'x' * 200, '你好'):
            with self.subTest(key=key):
                prefix = self.plugin._tts_cache_prefix('文本', key, params, str(self.cache_dir))
                self.assertRegex(prefix, r'^[0-9a-f]{32}-[0-9a-f]{8}$', prefix)

    def test_member_cannot_redirect_tts_endpoint(self):
        """改端点 = 把已配置的 api_key 交给新地址，因此需要管理员主体（审计项 P1-4）。"""
        from shell.backend.principal import ROLE_MEMBER, PrincipalContext, use_principal

        self.plugin.save_settings({'tts_api_key': 'sk-owner'})     # 无主体：owner 侧写入
        member = PrincipalContext(id='m', name='成员', role=ROLE_MEMBER, source='enrolled')
        with use_principal(member):
            denied = self.plugin.save_settings({'tts_base_url': 'http://attacker.invalid'})
        self.assertFalse(denied['success'])
        self.assertIn('管理员', denied.get('error', ''))
        self.assertEqual(self.plugin.setting('tts_base_url'), self.endpoint.base_url)
        # 未声明 admin_only 的朗读偏好不受影响
        with use_principal(member):
            self.assertTrue(self.plugin.save_settings({'tts_voice': 'echo'})['success'])

    def _cache_audio_path(self, key: str) -> Path:
        found = [p for p in self.cache_dir.iterdir()
                 if p.name.startswith(key + '-') and p.suffix == '.wav']
        self.assertEqual(len(found), 1, f'缓存里应恰好一个该键的 wav: {list(self.cache_dir.iterdir())}')
        return found[0]

    def test_cache_lives_under_config_and_can_be_cleared(self):
        """缓存位置与清理接口：位置必须落在 <config>/plugins 下，且能一键清空。

        放在 `<config>` 而不是文档根目录：用户找得到、清得掉，换文档根时缓存也不散落。
        """
        from shell.backend.paths import get_plugins_config_dir
        real_dir = self.module.DocumentReaderPlugin._tts_cache_dir(self.plugin)
        self.assertTrue(
            real_dir.is_relative_to(get_plugins_config_dir()),
            f'缓存目录不在 <config> 下: {real_dir}')

        self.plugin.tts_speak('今天下雨了。', 'clearme', 0)
        info = self.plugin.tts_cache_info()
        self.assertGreater(info['files'], 0)
        self.assertEqual(Path(info['path']), self.cache_dir)
        self.assertGreater(info['bytes'], 0)

        cleared = self.plugin.tts_clear_cache()
        self.assertTrue(cleared['success'])
        self.assertGreater(cleared['removed'], 0)
        self.assertEqual(self.plugin.tts_cache_info()['files'], 0, '清空后仍有残留文件')

    def test_clear_cache_on_fresh_install_is_not_an_error(self):
        """从没合成过时清缓存是幂等操作，不是"目录不可读"。"""
        result = self.plugin.tts_clear_cache()
        self.assertTrue(result['success'], result)
        self.assertEqual(result['removed'], 0)

    def test_plugin_class_does_not_shadow_mixin_members(self):
        """本类（MRO 最前）不得重复定义分片里的方法。

        真实事故：`main.py` 里留着一份旧的 `get_file_roots()`，本类排在 MRO 最前，
        于是分片里的新版（要包含朗读缓存目录）被静默盖掉 —— `/file` 不认缓存目录，
        合成成功的音频取回来是 403。常量与 `on_load`（自主覆写并调用基类）除外。
        """
        cls = self.module.DocumentReaderPlugin
        mixin_members: set = set()
        for base in cls.__mro__[1:]:
            if base.__name__ == 'PluginBase':
                break
            mixin_members |= set(base.__dict__)

        allowed = {
            # 常量：分片与入口各留一份，值必须一致
            'CACHE_DIR_NAME', 'CACHE_FILE', 'CACHE_VERSION', 'PROGRESS_FILE',
            'LEGACY_CACHE_DIR', 'LEGACY_CACHE_FILE', 'LEGACY_PROGRESS_FILE',
            # 生命周期的自主覆写（内部调用 super()）
            'on_load',
        }
        shadowed = sorted(
            name for name in mixin_members
            if name in cls.__dict__ and not name.startswith('__') and name not in allowed)
        self.assertEqual(shadowed, [], f'本类重复定义了分片成员，会静默覆盖分片: {shadowed}')

        # 常量也要真的同值，否则"两份"会漂移
        for name in allowed & mixin_members:
            if name == 'on_load':
                continue
            own = cls.__dict__[name]
            for base in cls.__mro__[1:]:
                if base.__name__ == 'PluginBase':
                    break
                if name in base.__dict__:
                    self.assertEqual(own, base.__dict__[name],
                                     f'{name} 在入口与分片里取值不同，会以入口为准')
                    break

    def test_speak_reports_engine_failure_as_error(self):
        self.plugin.update_setting('tts_base_url', '')  # 端点没了，且只准用 openai
        result = self.plugin.tts_speak('今天下雨了。', 'k', 0)
        self.assertIn('error', result)
        self.assertNotIn('url', result)

    def test_speak_rejects_empty_and_oversized_text(self):
        self.assertIn('error', self.plugin.tts_speak('   ', 'k', 0))
        self.assertIn('error', self.plugin.tts_speak('啊' * (tts.MAX_CHARS + 1), 'k', 0))

    def test_status_exposes_config(self):
        data = self.plugin.tts_status()
        self.assertEqual(data['mode'], 'openai')
        self.assertEqual(data['voice'], 'alloy')
        self.assertEqual(len(data['engines']), 3)

    def test_voices_api_shape(self):
        """音色列表接口：能问到端点就给真实列表，问不到就标 fallback（前端有兜底表）。"""
        result = self.plugin.tts_voices()
        self.assertIn('source', result)
        self.assertIn('voices', result)
        self.assertIn(result['source'], ('edge', 'fallback'))
        if result['source'] == 'edge':
            self.assertTrue(result['voices'], '标了 edge 就必须有音色')
            for voice in result['voices']:
                self.assertTrue(voice['name'].lower().startswith('zh'), voice)
                self.assertIn('locale', voice)
        else:
            self.assertEqual(result['voices'], [])

    def test_reader_preferences_are_declared_and_persist(self):
        """阅读偏好改走后端设置：键必须声明，且能写入读回（跨设备持久化的基础）。"""
        keys = {item['key'] for item in self.plugin.settings_schema}
        for key in ('reader_font_size', 'reader_line_height', 'reader_letter_spacing',
                    'reader_theme', 'reader_bg_color', 'reader_text_color', 'reader_mode'):
            self.assertIn(key, keys, f'{key} 没有在 settings_schema 里声明')

        saved = self.plugin.save_settings({'reader_font_size': 22, 'reader_theme': 'sepia'})
        self.assertTrue(saved.get('success'), saved)
        settings = self.plugin.get_settings()
        self.assertEqual(settings['reader_font_size'], 22)
        self.assertEqual(settings['reader_theme'], 'sepia')

    # ===== 前端调用形态（transport 层回归） =====
    #
    # 真实事故：前端写 `Bridge.call('get_settings', '')`（抄了 image-viewer 的写法，
    # 那边覆写成 `get_settings(self, rel_path='')` 所以接得住），本插件没覆写，
    # 基类签名是 `get_settings(self)` → `POST /api/document-reader__get_settings`
    # 直接 500（"takes 1 positional argument but 2 were given"）。
    # 这类错误绕过了所有前端静态检查，只有"按前端真实形态调一遍"才拦得住。

    def test_get_settings_accepts_frontend_call_shapes(self):
        schema = {item['key'] for item in self.plugin.settings_schema}
        # 无参（前端正确写法）
        plain = self.plugin.get_settings()
        self.assertTrue(schema.issubset(set(plain)))
        # 多传了一个参数：抛错就是 500，这种形态必须在后端被挡住
        with self.assertRaises(TypeError):
            self.plugin.get_settings('')

    def test_registered_api_methods_do_not_require_extra_args(self):
        """注册表里的每个方法都必须能"只带必要参数"调用（前端就是这么发的）。"""
        api = self.plugin.register_api()
        # 这些方法按设计需要参数（文档 id、文本、书签位置…），各自另有用例覆盖
        needs_args = {
            'document_get_chapters', 'document_get_content', 'document_cover',
            'document_update_progress', 'document_open_external', 'save_settings',
            'marks_add', 'marks_remove', 'tts_segment', 'tts_speak',
        }
        for name, method in api.items():
            if name in needs_args:
                continue
            try:
                method()
            except TypeError as exc:
                self.fail(f'注册的 {name}() 不接受无参调用：{exc}')


class CachePruneTests(unittest.TestCase):
    def test_prune_removes_oldest_pairs(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('document_reader_prune_test',
                                                     PLUGIN_BACKEND / 'main.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / 'tts'
            cache_dir.mkdir()
            for index in range(5):
                audio = cache_dir / f'k{index}.mp3'
                audio.write_bytes(b'x' * 100)
                (cache_dir / f'k{index}.json').write_text('{}', encoding='utf-8')
                os.utime(audio, (1000 + index, 1000 + index))
            plugin = module.DocumentReaderPlugin(
                {'name': 'document-reader'}, {'directories': {'data_root': tmp}})
            plugin._tts_prune(str(cache_dir), keep_bytes=250)
            left = sorted(p.name for p in cache_dir.iterdir())
            # 只剩最新的两对（音频 + 时间戳），旧的都是成对删掉的
            self.assertEqual(left, ['k3.json', 'k3.mp3', 'k4.json', 'k4.mp3'])


if __name__ == '__main__':
    unittest.main()
