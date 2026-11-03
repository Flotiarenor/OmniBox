"""edge-tts 合成必须带超时：卡住的流不能把请求线程一起挂住。

原来的实现是 `asyncio.run(_edge_stream(...))`，库里对端把连接挂住时 `async for`
会永远等下去 —— 请求线程被占住，前端只能一直转圈，连错误都没有。

现在的判据是"这么久没有收到新的数据块"（`STREAM_IDLE_TIMEOUT_SECONDS`），
由 `asyncio.wait_for` 包住**单次读取**；超时抛 `SynthesisTimeout`，由引擎的降级链
换成下一档或如实报错。用例断言的是这一行为：

  * 正常流仍然合成成功（超时包装对长文本透明，时间戳锚点不变）；
  * 流卡住时抛可识别错误，且在远小于"卡住时长"的时间内返回；
  * 走到请求层（`tts_speak`）时返回 `error` 而不是挂住，也没有写出半个缓存文件；
  * 超时不原地重试三次 —— 判定用耗时（重试会让等待翻约九倍），不看 mock 次数。

`edge_tts` 换成假模块是**测试环境限制**（真实合成要连网络）：假模块只提供
`Communicate` 与可控的 `stream()`，被替换的是第三方网络客户端，不是被测代码。
"""

import asyncio
import importlib.machinery
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'document-reader'
PLUGIN_BACKEND = PLUGIN_DIR / 'backend'

from shell.backend.plugin_utils import load_sibling
from shell.backend.settings_store import SettingsStore
from tools.check_plugins import _load_backend_class

tts = load_sibling(str(PLUGIN_BACKEND / 'tts_engine.py'), 'tts_engine',
                   'document_reader_tts_timeout_test')
edge = load_sibling(str(PLUGIN_BACKEND / 'tts_edge.py'), 'tts_edge',
                    'document_reader_tts_timeout_test')

#: 测试里把空闲超时压到这么短：判据是"没有进展"，压短不影响被测语义。
TEST_IDLE_TIMEOUT = 0.2


def _fake_edge_module(communicate_cls):
    """一个能被 `import edge_tts` 取到的假模块（带 `__spec__`，`available()` 才认）。"""
    module = types.ModuleType('edge_tts')
    module.Communicate = communicate_cls
    module.__spec__ = importlib.machinery.ModuleSpec('edge_tts', loader=None)
    return module


class _NormalCommunicate:
    """正常流：两个音频块 + 一个词边界，用来证明超时包装不改变结果与时间轴。"""

    def __init__(self, text, voice, rate=None, boundary=None):
        self.text = text
        self.state = {'chunk_audio_bytes': 0}

    async def stream(self):
        yield {'type': 'audio', 'data': b'\x01' * 5}
        self.state['chunk_audio_bytes'] = 10
        yield {'type': 'audio', 'data': b'\x02' * 5}
        yield {'type': 'WordBoundary', 'text': '第一', 'offset': 100000,
               'duration': 20000}


class _StallingCommunicate:
    """卡住的流：第一块之后就再没有数据（真实场景是连接被挂住）。"""

    stall_seconds = 30.0

    def __init__(self, text, voice, rate=None, boundary=None):
        self.text = text
        self.state = {'chunk_audio_bytes': 0}

    async def stream(self):
        yield {'type': 'audio', 'data': b'\x01'}
        await asyncio.sleep(type(self).stall_seconds)
        yield {'type': 'audio', 'data': b'\x02'}


class EdgeTimeoutTests(unittest.TestCase):
    def test_normal_stream_is_synthesized_with_marks(self):
        """正常流照旧合成：音频字节与字级时间戳（锚点校正后）都不变。"""
        fake = _fake_edge_module(_NormalCommunicate)
        with mock.patch.dict(sys.modules, {'edge_tts': fake}), \
                mock.patch.object(edge, 'STREAM_IDLE_TIMEOUT_SECONDS', TEST_IDLE_TIMEOUT):
            audio, ext, marks = edge.synthesize('今天下雨了。', {'voice': 'v', 'rate': 0})

        self.assertEqual(audio, b'\x01' * 5 + b'\x02' * 5)
        self.assertEqual(ext, '.mp3')
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]['text'], '第一')
        # offset 100000（100ns 单位）= 10ms；锚点为 0，因此相对音频起点即 10ms
        self.assertEqual(marks[0]['startMs'], 10.0)
        self.assertEqual(marks[0]['durationMs'], 2.0)

    def test_stalled_stream_raises_identifiable_timeout(self):
        """卡住的流：抛可识别错误，耗时远小于卡住时长（不无限等）。"""
        fake = _fake_edge_module(_StallingCommunicate)
        started = time.time()
        with mock.patch.dict(sys.modules, {'edge_tts': fake}), \
                mock.patch.object(edge, 'STREAM_IDLE_TIMEOUT_SECONDS', TEST_IDLE_TIMEOUT), \
                self.assertRaises(edge.SynthesisTimeout) as ctx:
            edge.synthesize('今天下雨了。', {'voice': 'v', 'rate': 0})
        elapsed = time.time() - started

        self.assertIn('超时', str(ctx.exception))
        self.assertIs(ctx.exception.retryable, False,
                      '超时不该在同一引擎上重试')
        self.assertLess(elapsed, 5.0,
                        f'超时没有生效，等了 {elapsed:.1f} 秒（卡住时长为 '
                        f'{_StallingCommunicate.stall_seconds:g} 秒）')

    def test_engine_reports_timeout_without_retrying_in_place(self):
        """引擎层：固定引擎超时后立刻报错，而不是原地重试三次。

        `_attempt_engine` 默认对任何异常重试 `ENGINE_ATTEMPTS` 次（3 次，间隔 0.4 与
        0.8 秒）。超时若也重试，一次卡流要等约 1.8 秒 —— 这里用耗时区分两种行为。
        """
        speech = tts.SpeechEngine({'edge': edge.synthesize},
                                  {'edge': edge.available},
                                  lambda engine_name, params: None)
        fake = _fake_edge_module(_StallingCommunicate)
        started = time.time()
        with mock.patch.dict(sys.modules, {'edge_tts': fake}), \
                mock.patch.object(edge, 'STREAM_IDLE_TIMEOUT_SECONDS', TEST_IDLE_TIMEOUT), \
                self.assertRaises(RuntimeError) as ctx:
            speech.synthesize('今天下雨了。', {'mode': 'edge', 'voice': 'v', 'rate': 0})
        elapsed = time.time() - started

        self.assertIn('超时', str(ctx.exception))
        self.assertLess(elapsed, 1.0,
                        f'超时被原地重试了：耗时 {elapsed:.2f} 秒（单次约 '
                        f'{TEST_IDLE_TIMEOUT:g} 秒，三次重试约 1.8 秒）')


class PluginRequestTimeoutTests(unittest.TestCase):
    """请求层：`tts_speak` 在超时后返回 error，且没有写出半个缓存文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        cls = _load_backend_class(PLUGIN_DIR, 'backend/main.py', 'DocumentReaderPlugin')
        self.plugin = cls({'name': 'document-reader', 'dependencies': []},
                          {'directories': {'data_root': str(self.tmp)}})
        self.plugin._settings_store = SettingsStore(str(self.tmp / 'settings'))
        self.plugin.update_setting('tts_engine', 'edge')
        self.plugin.update_setting('tts_voice', 'zh-CN-XiaoxiaoNeural')
        self.cache_dir = self.tmp / 'tts-cache'
        self.plugin.tts._cache.cache_dir = lambda: self.cache_dir
        # 插件入口加载的是自己那份 tts_edge 模块；超时要压在它上面才生效
        self.edge_module = sys.modules['document_reader_tts_edge']

    def test_tts_speak_returns_error_on_timeout(self):
        fake = _fake_edge_module(_StallingCommunicate)
        started = time.time()
        with mock.patch.dict(sys.modules, {'edge_tts': fake}), \
                mock.patch.object(self.edge_module, 'STREAM_IDLE_TIMEOUT_SECONDS',
                                  TEST_IDLE_TIMEOUT):
            result = self.plugin.tts.tts_speak('今天下雨了。', 'e0decafe', 0)
        elapsed = time.time() - started

        self.assertIn('error', result, result)
        self.assertIn('超时', result['error'])
        self.assertNotIn('url', result)
        self.assertLess(elapsed, 1.0,
                        f'请求在超时后没有及时返回：{elapsed:.2f} 秒')
        self.assertEqual(list(self.cache_dir.iterdir()), [],
                         '合成失败时不该留下缓存文件')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
