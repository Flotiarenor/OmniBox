"""朗读测速工具的自检：音频时长解析、播放队列模型、参数复用与文本来源。

为什么这些断言值得留着：

  * **音频时长**是整个结论的地基。WAV 的块对齐、MP3 的 ID3v2 头与逐帧长度、Ogg 的
    granule 哨兵，任何一处算错，实时系数就会整体偏移；而偏移方向一致时，"跟得上"与
    "跟不上"的结论会一起错，界面上看不出来。
  * **播放队列模型**复刻 `reader-tts.js` 的预取行为（每句开始播才要下一句）。它决定
    "卡顿"这个数字，写错一个下标就会把等待算到前一句头上，或者把稳态落后算成零卡顿。
  * **参数复用**：测速必须用插件真实的参数整理（`TtsMixin._tts_params`），其中包含
    "edge 音色填进端点时要换成端点通用名"这条规则。工具自己抄一份就必然漂移。

全程零网络、不出声：合成一律是假的，音频字节在用例里现造。
"""

import argparse
import contextlib
import importlib.util
import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TOOL_FILE = PROJECT_ROOT / 'plugins' / 'document-reader' / 'tool' / 'tts_speed.py'


def load_tool():
    """按路径加载测速工具（它不是包，也不该被安装进环境）。"""
    spec = importlib.util.spec_from_file_location('document_reader_tts_speed_test', TOOL_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = load_tool()


# ============================================================
# 用例自带的音频夹具（与工具里的构造器各写一遍：改一边就要重新核对另一边）
# ============================================================


def make_wav(seconds: float, rate: int = 8000, channels: int = 1, bits: int = 16) -> bytes:
    """静音 WAV；`data` 块长度为奇数时补一个填充字节（RIFF 的块对齐规则）。"""
    byte_rate = rate * channels * bits // 8
    frames = int(byte_rate * seconds)
    payload = b'\x7f' * (frames + frames % 2)
    fmt = struct.pack('<HHIIHH', 1, channels, rate, byte_rate, channels * bits // 8, bits)
    body = (b'WAVE' + b'fmt ' + struct.pack('<I', len(fmt)) + fmt
            + b'data' + struct.pack('<I', len(payload)) + payload)
    return b'RIFF' + struct.pack('<I', len(body)) + body


MP3_KBPS = 128
MP3_FRAME_BYTES = 144 * MP3_KBPS * 1000 // 44100      # 417


def make_mp3(frames: int) -> bytes:
    """`frames` 个 MPEG1 Layer III / 128 kbps / 44.1 kHz 的帧（每帧 1152 个采样）。"""
    header = bytes((0xFF, 0xFB, 0x90, 0x00))
    return b''.join(header + b'\x00' * (MP3_FRAME_BYTES - 4) for _ in range(frames))


def make_ogg(seconds: float) -> bytes:
    """一个 Opus 的 Ogg 流：首页带 OpusHead，末页带 48 kHz 的 granule。"""

    def page(granule: int, payload: bytes, header_type: int) -> bytes:
        head = (b'OggS' + bytes((0, header_type)) + granule.to_bytes(8, 'little')
                + (7).to_bytes(4, 'little') + (0).to_bytes(4, 'little')
                + (0).to_bytes(4, 'little') + bytes((1,)))
        return head + bytes((len(payload),)) + payload

    ident = (b'OpusHead' + bytes((1, 1)) + (312).to_bytes(2, 'little')
             + (48000).to_bytes(4, 'little') + bytes((0, 0, 0)))
    return (page(0, ident, 2)
            + page(int(seconds * 48000), b'\x00' * 20, 4))


def write_min_epub(path: Path, chapters) -> None:
    """写一个最小合法 EPUB（mimetype + container.xml + OPF + 逐章 xhtml）。"""
    items = ''.join(f'<item id="c{index}" href="c{index}.xhtml" '
                    f'media-type="application/xhtml+xml"/>' for index in range(len(chapters)))
    spine = ''.join(f'<itemref idref="c{index}"/>' for index in range(len(chapters)))
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="bookid">urn:uuid:tts-speed-test</dc:identifier>'
        '<dc:title>测速样书</dc:title><dc:language>zh</dc:language></metadata>'
        f'<manifest>{items}</manifest><spine>{spine}</spine></package>'
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    with zipfile.ZipFile(str(path), 'w') as archive:
        archive.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip',
                         compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml', container)
        archive.writestr('OEBPS/content.opf', opf)
        for index, body in enumerate(chapters):
            archive.writestr(
                f'OEBPS/c{index}.xhtml',
                '<?xml version="1.0" encoding="utf-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>章</title></head>'
                f'<body><h1>标题{index}</h1><p>{body}</p></body></html>')


# ============================================================


class AudioDurationTests(unittest.TestCase):
    """三种格式的时长都要算准，且后缀与内容对不上时按文件头认。"""

    def test_wav_bytes_over_byte_rate(self):
        seconds, source = tool.probe_duration(make_wav(1.25), '.wav')
        self.assertEqual(source, 'wav')
        self.assertAlmostEqual(seconds, 1.25, places=4)

    def test_mp3_counts_frames(self):
        frames = 37
        seconds, source = tool.probe_duration(make_mp3(frames), '.mp3')
        self.assertEqual(source, 'mp3')
        self.assertAlmostEqual(seconds, frames * 1152 / 44100, places=4)

    def test_mp3_skips_id3v2_tag(self):
        tag = b'ID3' + bytes((4, 0, 0)) + (0).to_bytes(4, 'big')
        tagged = tag + make_mp3(10)
        self.assertAlmostEqual(tool.probe_duration(tagged, '.mp3')[0], 10 * 1152 / 44100, places=4)

    def test_ogg_uses_last_granule(self):
        seconds, source = tool.probe_duration(make_ogg(1.5), '.ogg')
        self.assertEqual(source, 'ogg')
        self.assertAlmostEqual(seconds, 1.5, places=4)

    def test_wrong_suffix_falls_back_to_magic(self):
        """端点没报 Content-Type、引擎按请求格式猜后缀时，不能因此判「算不出时长」。"""
        seconds, source = tool.probe_duration(make_wav(0.5), '.mp3')
        self.assertEqual(source, 'wav')
        self.assertAlmostEqual(seconds, 0.5, places=4)

    def test_unknown_or_empty_bytes_have_no_duration(self):
        for data, ext in ((b'', '.mp3'), (b'not audio at all', '.mp3'), (b'\x00' * 64, '.wav')):
            with self.subTest(ext=ext, size=len(data)):
                self.assertEqual(tool.probe_duration(data, ext), (None, 'none'))

    def test_marks_are_the_last_resort(self):
        """音频字节解析不出来时用字级时间戳兜底（只有 edge 有）。"""
        marks = [{'startMs': 0.0, 'durationMs': 400.0}, {'startMs': 400.0, 'durationMs': 500.0}]
        self.assertEqual(tool.duration_from_marks(marks), (0.9, 'marks'))
        self.assertEqual(tool.duration_from_marks([]), (None, 'none'))


class QueueModelTests(unittest.TestCase):
    """时间轴：请求在上一句开始播放时发出，因此允许的延迟是上一句的音频长度。"""

    def test_fast_synthesis_never_stalls_and_only_pays_the_first_wait(self):
        sim = tool.simulate([1.0, 1.0, 1.0], [0.2, 0.2, 0.2], depth=1)
        self.assertEqual(sim['stall_count'], 0)
        self.assertAlmostEqual(sim['audio_s'], 3.0)
        # 听一遍的墙钟 = 音频总长 + 第一句的合成等待（后面都赶在播放结束前就绪）
        self.assertAlmostEqual(sim['wall_s'], 3.2)
        self.assertAlmostEqual(sim['first_audio_s'], 0.2)

    def test_slow_sentence_stalls_the_next_one(self):
        sim = tool.simulate([1.0, 1.0, 1.0], [0.2, 1.5, 0.2], depth=1)
        self.assertEqual(sim['stall_count'], 1)
        self.assertEqual([round(gap, 4) for gap in sim['gaps_s']], [0.0, 0.5, 0.0])
        self.assertEqual(sim['max_stall_index'], 1)
        self.assertAlmostEqual(sim['stall_s'], 0.5)

    def test_deeper_prefetch_absorbs_a_late_sentence(self):
        durations = [1.0, 1.0, 1.0]
        synth = [0.2, 0.5, 1.5]
        self.assertEqual(tool.simulate(durations, synth, depth=1)['stall_count'], 1)
        self.assertEqual(tool.simulate(durations, synth, depth=2)['stall_count'], 0)

    def test_short_sentence_with_a_fixed_engine_overhead_always_stalls(self):
        """五六个字的句子音频只有零点几秒：合成开销固定时，卡顿与句子长度直接挂钩。

        第一句的等待不算卡顿（它在开播之前，报告里单列为「首句合成等待」），
        所以三句话只数出两次。
        """
        sim = tool.simulate([0.4, 0.5, 0.6], [1.2, 1.2, 1.2], depth=1)
        self.assertAlmostEqual(sim['first_audio_s'], 1.2)
        self.assertEqual(sim['stall_count'], 2)
        self.assertGreater(sim['stall_s'], 1.0)

    def test_mismatched_or_empty_input(self):
        with self.assertRaises(ValueError):
            tool.simulate([1.0], [0.1, 0.2])
        empty = tool.simulate([], [])
        self.assertEqual(empty['wall_s'], 0.0)
        self.assertEqual(empty['stall_count'], 0)

    def test_realtime_factor_and_verdict(self):
        self.assertAlmostEqual(tool.realtime_factor([1.0, 1.0], [1.5, 1.5]), 1.5)
        self.assertEqual(tool.realtime_factor([], []), 0.0)
        self.assertEqual(tool.judge(1.5, 0)[0], '跟不上')
        self.assertEqual(tool.judge(0.6, 2)[0], '局部卡顿')
        self.assertEqual(tool.judge(0.6, 0)[0], '跟得上')


class MeasureTests(unittest.TestCase):
    """逐句测量：只包住合成本身，音频时长走解析，失败要带上是第几句。"""

    def test_rows_carry_duration_and_synthesis_time(self):
        audio = make_wav(0.25)

        def fake(text, params):
            return {'audio': audio, 'ext': '.wav', 'marks': [], 'engine': 'fake-edge'}

        rows, error = tool.measure(['第一句话。', '第二句更长一些！'], {'mode': 'fake'}, fake)
        self.assertIsNone(error)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]['audio_s'], 0.25, places=4)
        self.assertEqual(rows[0]['chars'], 5)
        self.assertEqual(rows[1]['engine'], 'fake-edge')
        self.assertGreaterEqual(rows[0]['synth_s'], 0.0)
        self.assertEqual(rows[0]['duration_source'], 'wav')

    def test_unparsable_audio_falls_back_to_marks(self):
        def fake(text, params):
            return {'audio': b'?', 'ext': '.mp3', 'engine': 'edge',
                    'marks': [{'startMs': 0.0, 'durationMs': 900.0}]}

        rows, error = tool.measure(['一句话。'], {}, fake)
        self.assertIsNone(error)
        self.assertAlmostEqual(rows[0]['audio_s'], 0.9)
        self.assertEqual(rows[0]['duration_source'], 'marks')

    def test_engine_failure_reports_which_sentence(self):
        def boom(text, params):
            raise RuntimeError('端点拒绝连接')

        rows, error = tool.measure(['第一句。', '第二句。'], {}, boom)
        self.assertEqual(rows, [])
        self.assertIn('第 1 句', error)
        self.assertIn('端点拒绝连接', error)


class SettingsTests(unittest.TestCase):
    """参数必须经由插件的 `_tts_params` 整理 —— 包括 edge 音色换成端点通用名那条规则。"""

    @classmethod
    def setUpClass(cls):
        cls.backend = tool.load_backend()

    def test_empty_settings_use_schema_defaults(self):
        params, _merged = tool.resolve_params(self.backend, {}, {})
        self.assertEqual(params['mode'], 'auto')
        self.assertEqual(params['rate'], 100)
        self.assertEqual(params['order'], ['openai', 'edge', 'system'])
        self.assertEqual(params['voice'], 'zh-CN-XiaoxiaoNeural')

    def test_edge_voice_is_translated_for_the_endpoint(self):
        stored = {'tts_engine': 'openai', 'tts_voice': 'zh-CN-XiaoyiNeural',
                  'tts_base_url': 'http://127.0.0.1:9'}
        params, _merged = tool.resolve_params(self.backend, stored, {})
        self.assertEqual(params['voice'], 'alloy')

    def test_endpoint_voice_name_is_left_alone(self):
        stored = {'tts_engine': 'openai', 'tts_voice': 'zh-female-01',
                  'tts_base_url': 'http://127.0.0.1:9'}
        params, _merged = tool.resolve_params(self.backend, stored, {})
        self.assertEqual(params['voice'], 'zh-female-01')

    def test_command_line_overrides_stored_settings(self):
        stored = {'tts_engine': 'edge', 'tts_rate': 20}
        params, _merged = tool.resolve_params(
            self.backend, stored, {'tts_engine': 'system', 'tts_rate': 0})
        self.assertEqual(params['mode'], 'system')
        self.assertEqual(params['rate'], 0)

    def test_prefetch_depth_comes_from_settings_with_safe_fallback(self):
        """时间轴用的深度必须与界面同一套规则：读设置，配坏了落回 1。"""
        default = tool.resolve_params(self.backend, {}, {})[0]['prefetch']
        self.assertGreater(default, 1, '默认深度应当是多句预取（配置项默认值）')
        for stored, want in (
            ({}, default),
            ({'tts_prefetch': 3}, 3),
            ({'tts_prefetch': 1}, 1),
            ({'tts_prefetch': 0}, 1),
            ({'tts_prefetch': default * 100}, 1),
            ({'tts_prefetch': '五句'}, 1),
        ):
            with self.subTest(stored=stored):
                params, _merged = tool.resolve_params(self.backend, stored, {})
                self.assertEqual(params['prefetch'], want)


class TextSourceTests(unittest.TestCase):
    """正文来源：直给、文本文件、书的某一章，以及 HTML 转文本。"""

    @classmethod
    def setUpClass(cls):
        cls.backend = tool.load_backend()

    def test_html_becomes_plain_lines(self):
        html = '<h1>标题</h1><p>第一段&amp;内容。</p><p>第二段。</p>'
        self.assertEqual(tool.html_to_text(html), '标题\n第一段&内容。\n第二段。')

    def test_sample_text_is_split_losslessly(self):
        pieces = self.backend['engine'].split_text(tool.SAMPLE_TEXT, 240)
        self.assertEqual(''.join(pieces), tool.SAMPLE_TEXT)
        self.assertGreater(len(pieces), 6)
        # 样本必须同时含短句与长句：短句是卡顿最容易出现的地方
        self.assertTrue(any(len(piece) <= 10 for piece in pieces))
        self.assertTrue(any(len(piece) >= 60 for piece in pieces))

    def test_txt_chapter_is_sliced_by_offsets(self):
        # 正文行不能以「第 N 章」开头：TxtParser 的章节正则只匹配行首前缀，这样的正文行
        # 会被当成新章标题、把章节切成空的（插件本身的行为，另有它的用例覆盖；
        # 这里只求夹具不落进那个坑）。
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / 'book.txt'
            book.write_text('第一章 起点\n起点章的正文。\n\n第二章 终点\n终点章的正文。\n',
                            encoding='utf-8')
            args = argparse.Namespace(document=book, chapter=1, encoding='auto')
            text, source = tool._document_chapter(args, self.backend)
        self.assertIn('终点章的正文', text)
        self.assertNotIn('起点章的正文', text)
        self.assertIn('第 2 章', source)

    def test_chapter_index_out_of_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / 'book.txt'
            book.write_text('第一章 起点\n正文。\n', encoding='utf-8')
            args = argparse.Namespace(document=book, chapter=9, encoding='auto')
            with self.assertRaises(SystemExit):
                tool._document_chapter(args, self.backend)

    def test_markdown_chapter_goes_through_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / 'doc.md'
            doc.write_text('# 第一章\n\n正文甲。\n\n# 第二章\n\n正文乙。\n', encoding='utf-8')
            args = argparse.Namespace(document=doc, chapter=1, encoding='auto')
            text, _source = tool._document_chapter(args, self.backend)
        self.assertIn('正文乙', text)

    def test_epub_chapter_goes_through_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / 'book.epub'
            write_min_epub(book, ['第一段的正文。', '目标段的正文。'])
            args = argparse.Namespace(document=book, chapter=1, encoding='auto')
            text, source = tool._document_chapter(args, self.backend)
        self.assertIn('目标段的正文', text)
        self.assertIn('第 2 章', source)

    def test_text_file_is_read_with_encoding_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / 'sample.txt'
            plain.write_text('一段用于测速的正文。第二句。\n', encoding='gb18030')
            args = argparse.Namespace(text=None, text_file=plain, document=None)
            text, source = tool.pick_text(args, self.backend)
        self.assertIn('一段用于测速的正文', text)
        self.assertIn('sample.txt', source)


class ReportTests(unittest.TestCase):
    """报告：结论与汇总必须在，表格按显示宽度对齐（中文占两列）。"""

    def test_width_padding_handles_cjk(self):
        self.assertEqual(tool._width('中文'), 4)
        self.assertEqual(tool._pad('中文', 6), '中文  ')
        self.assertEqual(tool._pad('ab', 4, right=True), '  ab')

    def test_report_contains_verdict_and_sections(self):
        durations = [1.0, 0.4, 2.0]
        synth = [2.0, 1.5, 1.8]
        sim = tool.simulate(durations, synth, 1)
        depths = {depth: tool.simulate(durations, synth, depth) for depth in tool.DEPTH_LADDER}
        rtf = tool.realtime_factor(durations, synth)
        verdict, reason = tool.judge(rtf, sim['stall_count'])
        rows = [{'index': index, 'text': '句子', 'chars': 10, 'audio_s': audio,
                 'duration_source': 'mp3', 'synth_s': wall, 'engine': 'edge',
                 'bytes': 100, 'wait_s': gap}
                for index, (audio, wall, gap) in
                enumerate(zip(durations, synth, sim['gaps_s'], strict=True))]
        summary = {
            'settings_path': 'D:\\占位\\document-reader.json',
            'engine_label': 'edge（edge-tts）',
            'voice_line': 'zh-CN-XiaoxiaoNeural · 语速 +100%',
            'endpoint_line': '（未配置）',
            'text_source': '命令行文本（--text）',
            'sentence_count': len(rows),
            'char_count': 30,
            'synth_s': sum(synth),
            'depth': 1,
            'rtf': rtf,
            'sim': sim,
            'depths': depths,
            'verdict': verdict,
            'reason': reason,
            'advice': tool.build_advice(rtf, sim, depths, 1),
        }
        text = '\n'.join(tool.render_report(summary, rows))
        self.assertIn('朗读测速', text)
        self.assertIn('实时系数', text)
        self.assertIn('预取深度对照', text)
        self.assertIn(f'结论        {verdict}', text)
        self.assertIn('建议', text)

    def test_quiet_report_skips_the_per_sentence_table(self):
        sim = tool.simulate([1.0], [0.2], 1)
        summary = {
            'settings_path': 'p', 'engine_label': 'system', 'voice_line': 'v',
            'text_source': 's', 'sentence_count': 1, 'char_count': 5,
            'synth_s': 0.2, 'depth': 1, 'rtf': 0.2, 'sim': sim, 'depths': {1: sim},
            'verdict': '跟得上', 'reason': '合成几乎不占时间', 'advice': [],
        }
        text = '\n'.join(tool.render_report(summary, [], quiet=True))
        self.assertNotIn('音频s', text)
        self.assertIn('汇总', text)


class SelfTestEntryTests(unittest.TestCase):
    """`--selftest` 是用户的第一道自检：必须零网络、零副作用、可重复。"""

    def test_selftest_passes(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(tool.selftest(), 0)
        self.assertIn('自检通过', out.getvalue())

    def test_main_selftest_path(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(['--selftest']), 0)


if __name__ == '__main__':
    unittest.main()
