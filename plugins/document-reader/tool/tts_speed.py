#!/usr/bin/env python3
"""朗读测速器：量「合成一句要多久」与「念完一句要多久」，判断连续朗读跟不跟得上。

为什么需要它
------------
阅读器的朗读是「一句一合成」：前端把正文切成句子，逐句请求音频，而且**每句开始播放时
才去要下一句**（`reader-tts.js` 的 `_prefetchNext`）。于是有一条硬指标 —— 合成下一句
所花的时间必须小于**上一句**的音频长度，否则上一句已经念完、下一句还没生成好，用户听到
的就是断断续续的等待。

这条指标在界面上看不出来（看起来只是「偶尔卡一下」），单元测试也量不到：合成耗时取决于
本机 CPU、到 edge 端点的链路、以及自建 TTS 服务的排队情况，只有真实引擎跑一遍才知道。

用法
----
    venv/Scripts/python.exe plugins/document-reader/tool/tts_speed.py
    venv/Scripts/python.exe plugins/document-reader/tool/tts_speed.py --sentences 20
    venv/Scripts/python.exe plugins/document-reader/tool/tts_speed.py --engine system
    venv/Scripts/python.exe plugins/document-reader/tool/tts_speed.py \
        --document "D:\\文档\\某书.txt" --chapter 3
    venv/Scripts/python.exe plugins/document-reader/tool/tts_speed.py --json bench.json
    venv/Scripts/python.exe plugins/document-reader/tool/tts_speed.py --selftest

不加参数时读插件**当前保存的朗读设置**（引擎、音色、语速、端点），用内置样本文本测前
30 句。`--engine` 一类的参数只是在内存里覆盖设置，不会写回插件。

退出码：0 = 连续朗读跟得上（无卡顿）；1 = 有卡顿或稳态落后；2 = 无法测量（配置/引擎/文本）。

测量口径
--------
* **只读**：只读插件设置与文档，不写缓存、不改设置。合成直接走引擎
  （`tts_engine.synthesize`），**不经过** `tts_speak` 的按内容缓存 —— 走缓存的话第二次
  跑全是 0 秒，量不到任何东西。
* **音频时长**由音频字节现算（WAV 的 `fmt `/`data` 块、MP3 的帧头、Ogg 的 granule），
  不依赖第三方库；解析不出来时退回引擎给的字级时间戳（只有 edge 有），再不行就把这一句
  标成「未知」并从时间轴里剔除。
* **时间轴按前端的队列行为推算**：请求在第「当前句 − 预取深度」句开始播放的那一刻发出，
  一句能容忍的合成耗时是**上一句及更早几句**的音频长度，不是这一句的。短句（四五个字）
  的音频只有零点几秒，正是卡顿最容易出现的地方 —— 平均速度够快也救不回来，所以报告里
  既有稳态的实时系数，也有逐句的卡顿计数。深度取朗读设置里的 `tts_prefetch`（默认 5），
  与界面上真正在跑的策略一致。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'document-reader'
BACKEND_DIR = PLUGIN_DIR / 'backend'
MANIFEST_FILE = PLUGIN_DIR / 'manifest.json'

# 默认测这么多句：一句一次合成，联网引擎 30 句已经要一两分钟
DEFAULT_SENTENCES = 30
# 多长的停顿算「用户听得出来」：HTTP 抖动与浏览器调度本来就有几十毫秒
STALL_THRESHOLD_S = 0.1
# 预取深度对照：前端实际用的是设置项 tts_prefetch，这里把常见几档都算一遍，
# 用来回答「加深预取能不能救」「当前这一档是不是白开着」
DEPTH_LADDER: Tuple[int, ...] = (1, 2, 3, 5, 8)

# 内置样本文本：不来自任何一本书，只为覆盖朗读里真实交替出现的两种节奏 ——
# 四五个字的短句（音频撑不住合成耗时）与一两百字的长句（合成耗时与等待都更长）。
SAMPLE_TEXT = (
    '朗读测速用的样本文本，不来自任何一本书。'
    '短句先来一句。'
    '再来一句稍长一点的，好让合成与播放之间的余量露出本色。'
    '叙述总是一口气铺开：前因、场景、人物心里那点没说出口的盘算，都要在同一个句子里交代完，'
    '直到读的人以为这口气永远不会停，才在最后一个句号处收住。'
    '「对话要单独成行。」她说，「因为它常常比叙述更碎，一句只有半行。」'
    '标点密的时候，切出来的句子很短；这时候上一句的音频撑不住下一句的合成耗时，等待就出现了。'
    '长句反过来给了合成更多时间，代价是一旦等起来，等的时间也一并变长。'
    '最后一段留一点空白，让换段处的停顿也进统计。'
)


# ============================================================
# 音频时长（纯标准库：三个引擎的产出格式都在这里）
# ============================================================


def probe_duration(data: bytes, ext: str) -> Tuple[Optional[float], str]:
    """音频字节 -> (秒数, 来源)，来源取 `wav` / `mp3` / `ogg` / `none`。

    系统音色给 `.wav`，edge 与多数端点给 `.mp3`，少数端点回 `.ogg`。后缀不认或解析失败
    时按**文件头**再试一轮：端点的 `Content-Type` 不一定靠谱，引擎那边也可能猜错后缀
    （见 `openai_synthesize` 的注释），不能因为后缀对不上就判「算不出时长」。
    """
    if not data:
        return None, 'none'
    suffix = (ext or '').strip().lower()
    parsers: Dict[str, Callable[[bytes], Optional[float]]] = {
        '.wav': _wav_seconds,
        '.mp3': _mp3_seconds,
        '.ogg': _ogg_seconds,
        '.opus': _ogg_seconds,
    }
    direct = parsers.get(suffix)
    if direct is not None:
        seconds = direct(data)
        if seconds:
            return seconds, suffix.lstrip('.')
    for name, parser in (('wav', _wav_seconds), ('mp3', _mp3_seconds), ('ogg', _ogg_seconds)):
        if name == suffix.lstrip('.'):
            continue
        seconds = parser(data)
        if seconds:
            return seconds, name
    return None, 'none'


def _wav_seconds(data: bytes) -> Optional[float]:
    """RIFF/WAVE：`data` 块字节数除以 `fmt ` 里的字节率（不依赖 `wave` 模块）。"""
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        return None
    byte_rate = 0
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], 'little')
        body = pos + 8
        if chunk_id == b'fmt ' and size >= 12 and body + 12 <= len(data):
            byte_rate = int.from_bytes(data[body + 8:body + 12], 'little')
        elif chunk_id == b'data':
            if byte_rate <= 0:
                return None
            available = min(size, len(data) - body)
            return available / byte_rate if available > 0 else None
        pos = body + size + (size & 1)      # 块按偶数字节对齐
    return None


# MPEG 音频帧头里的比特率、采样率与每帧采样数。layer 用 1/2/3（Layer I/II/III），
# 版本分两组：MPEG1 与 MPEG2/2.5 —— 两张表的比特率与 Layer III 的采样数都不一样。
_MP3_KBPS: Dict[Tuple[int, int], Tuple[int, ...]] = {
    (1, 1): (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (2, 3): (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_RATES: Dict[int, Tuple[int, int, int]] = {
    3: (44100, 48000, 32000),       # MPEG1
    2: (22050, 24000, 16000),       # MPEG2
    0: (11025, 12000, 8000),        # MPEG2.5
}
_MP3_SAMPLES: Dict[Tuple[int, int], int] = {
    (1, 1): 384, (1, 2): 1152, (1, 3): 1152,
    (2, 1): 384, (2, 2): 1152, (2, 3): 576,
}


def _mp3_seconds(data: bytes) -> Optional[float]:
    """MPEG 音频：逐帧累加「每帧采样数 / 采样率」。

    逐帧走一遍而不是拿文件大小估：TTS 的音频是 VBR 与短音频的混合，按大小除比特率在
    短句上能差出几十毫秒，而卡顿判定的阈值就是几十毫秒。一小时的音频也才九万帧。
    """
    pos = 0
    if data[:3] == b'ID3' and len(data) >= 10:
        tag_size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) \
            | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        pos = 10 + tag_size + (10 if data[5] & 0x10 else 0)
    total = 0.0
    frames = 0
    limit = len(data) - 4
    while pos <= limit:
        header = int.from_bytes(data[pos:pos + 4], 'big')
        if header & 0xFFE00000 != 0xFFE00000:      # 不是同步字：往后挪一个字节再找帧头
            pos += 1
            continue
        version = (header >> 19) & 0x3
        layer_bits = (header >> 17) & 0x3
        kbps_index = (header >> 12) & 0xF
        rate_index = (header >> 10) & 0x3
        padding = (header >> 9) & 0x1
        if version == 1 or layer_bits == 0 or kbps_index in (0, 15) or rate_index == 3:
            pos += 1
            continue
        group = 1 if version == 3 else 2
        layer = 4 - layer_bits
        kbps = _MP3_KBPS[(group, layer)][kbps_index - 1]
        rate = _MP3_RATES[version][rate_index]
        samples = _MP3_SAMPLES[(group, layer)]
        total += samples / rate
        frames += 1
        frame_bytes = samples * kbps * 125 // rate + padding   # samples/8 * bitrate / rate
        pos += max(1, frame_bytes)
    return total if frames else None


def _ogg_seconds(data: bytes) -> Optional[float]:
    """Ogg：读最后一个完整页面的 granule（采样计数），除以采样率。

    采样率从首个页面的编码头取：Opus 的 granule 恒按 48 kHz 计，Vorbis 用自己的采样率。
    """
    sample_rate = 0
    last_granule: Optional[int] = None
    pos = 0
    while True:
        start = data.find(b'OggS', pos)
        if start < 0 or start + 27 > len(data):
            break
        granule = int.from_bytes(data[start + 6:start + 14], 'little')
        segments = data[start + 26]
        table_end = start + 27 + segments
        if table_end > len(data):
            break
        if not sample_rate:
            payload = data[table_end:table_end + 16]
            if payload.startswith(b'OpusHead'):
                sample_rate = 48000
            elif payload.startswith(b'\x01vorbis') and len(payload) >= 16:
                sample_rate = int.from_bytes(payload[12:16], 'little')
        if granule != 0xFFFFFFFFFFFFFFFF:          # 全 1 是「本页没有结束帧」的哨兵
            last_granule = granule
        pos = table_end + sum(data[start + 27:table_end])
        if pos <= start:
            break
    if last_granule is None or sample_rate <= 0:
        return None
    return last_granule / sample_rate


def duration_from_marks(marks: Sequence[dict]) -> Tuple[Optional[float], str]:
    """用引擎给的逐字时间戳兜底（只有 edge 有）：末字的偏移 + 时长。

    这个值略短于音频本身（句尾静音不在其中），所以只在音频字节解析不出来时拿来兜底。
    """
    best = 0.0
    for mark in marks or []:
        try:
            end = float(mark.get('startMs') or 0.0) + float(mark.get('durationMs') or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        best = max(best, end)
    return (best / 1000.0, 'marks') if best > 0 else (None, 'none')


# ============================================================
# 播放队列模型（口径与 reader-tts.js 一致）
# ============================================================


def simulate(durations: Sequence[float], synth_seconds: Sequence[float],
             depth: int = 1, stall_threshold: float = STALL_THRESHOLD_S) -> Dict[str, Any]:
    """按前端的预取行为推算「听一遍」的时间轴。

    `depth` 是预取深度：第 i 句的请求在**第 i-depth 句开始播放**的那一刻发出，音频在第 i
    句需要它之前就绪就不卡。界面上的深度来自设置项 `tts_prefetch`；深度大于 1 时假定多个
    请求并发（真实实现里它们互相独立，见 `reader-tts.js` 的 `_prefetchAt`）。

    返回的 `start_s[i]` 是第 i 句开始出声的时刻，`gaps_s[i]` 是它比「上一句念完」晚了多久。
    """
    count = len(durations)
    if count != len(synth_seconds):
        raise ValueError(f'时长与合成耗时数量不一致: {count} != {len(synth_seconds)}')
    if count == 0:
        return {'first_audio_s': 0.0, 'audio_s': 0.0, 'wall_s': 0.0, 'start_s': [],
                'gaps_s': [], 'stall_count': 0, 'stall_s': 0.0,
                'max_stall_s': 0.0, 'max_stall_index': None}
    depth = max(1, int(depth))

    ready = [0.0] * count
    start = [0.0] * count
    gaps = [0.0] * count
    # 关键顺序：第 i 句的请求在 start[i-depth] 发出，而 start 只能边推进边算 ——
    # 先整体算 ready 再算 start，就等于假定所有请求都在 0 时刻发出，等待会被算小。
    for index in range(count):
        issued_at = 0.0 if index < depth else start[index - depth]
        ready[index] = issued_at + synth_seconds[index]
        if index == 0:
            start[0] = ready[0]
            continue
        ideal = start[index - 1] + durations[index - 1]
        start[index] = max(ideal, ready[index])
        gaps[index] = start[index] - ideal

    stalled = [(index, gap) for index, gap in enumerate(gaps) if gap > stall_threshold]
    max_gap = max(gaps) if gaps else 0.0
    return {
        'first_audio_s': start[0],
        'audio_s': sum(durations),
        'wall_s': start[-1] + durations[-1],
        'start_s': start,
        'gaps_s': gaps,
        'stall_count': len(stalled),
        'stall_s': sum(gap for _index, gap in stalled),
        'max_stall_s': max_gap,
        'max_stall_index': gaps.index(max_gap) if max_gap > 0 else None,
    }


def realtime_factor(durations: Sequence[float], synth_seconds: Sequence[float]) -> float:
    """整体实时系数：合成总耗时 / 音频总长。大于 1 就是稳态也跟不上。"""
    audio = sum(durations)
    return (sum(synth_seconds) / audio) if audio > 0 else 0.0


def judge(rtf: float, stall_count: int) -> Tuple[str, str]:
    """结论与依据。稳态落后与局部卡顿是两回事，处理办法也不同。"""
    if rtf > 1.0:
        return '跟不上', f'稳态合成速度只有播放速度的 {1.0 / rtf:.2f} 倍'
    if stall_count:
        return '局部卡顿', '平均速度够，但短句的音频撑不住下一句的合成耗时'
    if rtf <= 0:
        return '跟得上', '合成几乎不占时间'
    return '跟得上', f'合成速度是播放速度的 {1.0 / rtf:.2f} 倍'


# ============================================================
# 文本来源
# ============================================================


class _HtmlText(HTMLParser):
    """把章节 HTML 变成纯文本：块级标签处断行，其余内容原样拼起来。

    朗读拿到的本来也是渲染后 DOM 的 `textContent`（见 `reader-tts.js` 的 `_split`），
    因此这里的口径要与之一致：只去掉排版用的空白，不增删正文里的字。
    """

    _BREAK_TAGS = frozenset({
        'p', 'div', 'br', 'li', 'tr', 'td', 'th', 'blockquote', 'pre', 'hr', 'table',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'figcaption',
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []

    @property
    def text(self) -> str:
        return ''.join(self._parts)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._BREAK_TAGS:
            self._parts.append('\n')

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BREAK_TAGS:
            self._parts.append('\n')

    def handle_data(self, data: str) -> None:
        self._parts.append(data)


def html_to_text(fragment: str) -> str:
    """章节 HTML -> 纯文本（空格压缩、空行去掉，保留段落断行）。"""
    parser = _HtmlText()
    parser.feed(fragment or '')
    parser.close()
    lines = [re.sub(r'[ \t\r\f\v]+', ' ', line).strip() for line in parser.text.split('\n')]
    return '\n'.join(line for line in lines if line)


# ============================================================
# 插件模块与设置
# ============================================================

_BACKEND: Optional[Dict[str, Any]] = None


def _load_module(file_path: Path, name: str):
    """按文件路径加载一个模块（插件后端由运行时 importlib 加载，这里走同一套手法）。"""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f'无法加载模块: {file_path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def load_backend() -> Dict[str, Any]:
    """加载本插件后端里测速要用到的几个模块（只加载一次，不构造插件实例）。

    不构造 `DocumentReaderPlugin`：它的 `__init__` 会读文档缓存与书签，测速不该碰这些
    状态。要的只有三样 —— 引擎（`synthesize`）、参数整理（`TtsMixin._tts_params`）、
    设置项默认值（`settings_schema`）。
    """
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from shell.backend.plugin_utils import load_sibling

    tts_module = load_sibling(str(BACKEND_DIR / 'tts.py'), 'tts', 'document_reader_speed')
    main_module = _load_module(BACKEND_DIR / 'main.py', 'document_reader_speed_main')
    schema = {item['key']: item.get('default')
              for item in main_module.DocumentReaderPlugin.settings_schema if item.get('key')}
    _BACKEND = {
        # tts.py 把引擎模块加载成 `_tts`（见那里的 `load_sibling`），与插件运行期是同一份
        'engine': tts_module._tts,
        'params_of': tts_module.TtsMixin._tts_params,
        # 预取深度的夹取与兜底在插件里，测速不重复实现一遍（改规则时只改一处）
        'prefetch_of': tts_module.TtsMixin._tts_prefetch,
        'schema': schema,
        'parser': load_sibling(str(BACKEND_DIR / 'parser.py'), 'parser', 'document_reader_speed'),
        'documents': load_sibling(str(BACKEND_DIR / 'documents.py'), 'documents', 'document_reader_speed'),
    }
    return _BACKEND


def plugin_name() -> str:
    """插件名（manifest 的 `name`；设置文件名就是它）。"""
    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 'document-reader'
    name = str(data.get('name') or '').strip()
    return name or 'document-reader'


def settings_path() -> Path:
    """插件设置的落点：`<config>/plugins/<插件名>.json`（与 SettingsStore 同一规则）。"""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from shell.backend.paths import get_plugins_config_dir
    return get_plugins_config_dir() / f'{plugin_name()}.json'


def read_settings(path: Path) -> Dict[str, Any]:
    """读一份插件设置；文件不存在或坏了都返回空字典（此时全用 schema 默认值）。"""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class _SettingsView:
    """把一份设置字典伪装成插件实例，好让 `TtsMixin._tts_params` 直接复用。

    复用而不是重写一遍参数整理：那里有一条不能丢的规则 —— 引擎选 `openai` 时，edge 的
    音色名（`zh-CN-XiaoxiaoNeural`）要换成端点的通用名（`alloy`），否则端点会静默换成
    它自己的第一个说话人。测速若绕开这条规则，测的就不是用户听到的那个声音。
    """

    def __init__(self, values: Dict[str, Any]) -> None:
        self._values = values

    def setting(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def _raw_settings(self) -> Dict[str, Any]:
        return dict(self._values)


def resolve_params(backend: Dict[str, Any], stored: Dict[str, Any],
                   overrides: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """合并「schema 默认值 → 已保存设置 → 命令行覆盖」，再交给插件的参数整理。"""
    merged = {**backend['schema'], **stored}
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    params = backend['params_of'](_SettingsView(merged))
    # 预取深度不是引擎参数，但时间轴要用它：按插件同一套夹取规则算，测的就是界面的策略
    params['prefetch'] = backend['prefetch_of'](_SettingsView(merged))
    return params, merged


# ============================================================
# 测量
# ============================================================


def measure(texts: Sequence[str], params: Dict[str, Any],
            synthesize: Callable[[str, Dict[str, Any]], Dict[str, Any]],
            progress: Optional[Callable[[Dict[str, Any], int], None]] = None
            ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """逐句合成并计时；返回 (每句记录, 错误信息)。

    计时只包住 `synthesize` 本身（与前端一次 `tts_speak` 请求的等待对应），不含切句与
    音频时长解析 —— 后两者是毫秒级，混进来会让数字失去可复现性。
    """
    rows: List[Dict[str, Any]] = []
    for index, text in enumerate(texts):
        started = time.perf_counter()
        try:
            result = synthesize(text, params)
        except Exception as exc:
            return rows, f'第 {index + 1} 句合成失败：{exc}'
        elapsed = time.perf_counter() - started
        audio = result.get('audio') or b''
        seconds, source = probe_duration(audio, str(result.get('ext') or ''))
        if seconds is None:
            seconds, source = duration_from_marks(result.get('marks') or [])
        row = {
            'index': index,
            'text': text,
            'chars': len(text),
            'audio_s': seconds,
            'duration_source': source,
            'synth_s': elapsed,
            'engine': str(result.get('engine') or ''),
            'bytes': len(audio),
        }
        rows.append(row)
        if progress is not None:
            progress(row, len(texts))
    return rows, None


# ============================================================
# 报告
# ============================================================


def _width(text: str) -> int:
    """显示宽度：中文与全角标点占两列，表格才对得齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    """按显示宽度补空格（CJK 占两列）。"""
    fill = ' ' * max(0, width - _width(text))
    return fill + text if right else text + fill


def _seconds(value: Optional[float]) -> str:
    return '—' if value is None else f'{value:.2f}'


def render_report(summary: Dict[str, Any], rows: Sequence[Dict[str, Any]],
                  quiet: bool = False) -> List[str]:
    """把测量结果排成给人看的几段。返回行列表，便于用例断言。"""
    lines: List[str] = []
    lines.append('朗读测速 · document-reader')
    lines.append('')
    lines.append(f'  设置      {summary["settings_path"]}')
    lines.append(f'  引擎      {summary["engine_label"]}')
    lines.append(f'  音色      {summary["voice_line"]}')
    if summary.get('endpoint_line'):
        lines.append(f'  端点      {summary["endpoint_line"]}')
    lines.append(f'  文本      {summary["text_source"]} · {summary["sentence_count"]} 句 / '
                 f'{summary["char_count"]} 字')
    lines.append(f'  预取深度  {summary["depth"]} 句（{summary.get("depth_source", "时间轴按它算")}）')
    if summary.get('mixed_engines'):
        lines.append('  注意      本轮用到了多个引擎（自动模式降级），耗时不代表单一引擎')
    if summary.get('unknown_duration'):
        lines.append(f'  注意      另有 {summary["unknown_duration"]} 句算不出音频时长，已从时间轴里剔除')
    lines.append('')

    if not quiet:
        header = (f'  {_pad("#", 4, True)} {_pad("字数", 5, True)} {_pad("音频s", 8, True)} '
                  f'{_pad("合成s", 8, True)} {_pad("RTF", 7, True)} {_pad("等待s", 7, True)}')
        lines.append(header)
        for row in rows:
            audio = row['audio_s']
            synth = row['synth_s']
            rtf = (synth / audio) if audio else None
            wait = row.get('wait_s')
            lines.append(
                f'  {_pad(str(row["index"] + 1), 4, True)} {_pad(str(row["chars"]), 5, True)} '
                f'{_pad(_seconds(audio), 8, True)} {_pad(f"{synth:.2f}", 8, True)} '
                f'{_pad("—" if rtf is None else f"{rtf:.2f}", 7, True)} '
                f'{_pad("—" if wait is None else f"{wait:.2f}", 7, True)}')
        lines.append('')

    sim = summary['sim']
    lines.append('汇总')
    lines.append(f'  音频总长    {_fmt_clock(sim["audio_s"])}')
    lines.append(f'  合成总耗时  {_fmt_clock(summary["synth_s"])}')
    lines.append(f'  实时系数    {summary["rtf"]:.2f}（合成 1 分钟音频需要 '
                 f'{summary["rtf"]:.2f} 分钟）')
    lines.append(f'  听一遍      {_fmt_clock(sim["wall_s"])}（首句合成等待 '
                 f'{_fmt_clock(sim["first_audio_s"])}）')
    if sim['stall_count']:
        lines.append(f'  卡顿        {sim["stall_count"]} 次 · 累计 {_fmt_clock(sim["stall_s"])}'
                     f' · 最长 {_fmt_clock(sim["max_stall_s"])}'
                     f'（第 {sim["max_stall_index"] + 1} 句前）')
    else:
        lines.append(f'  卡顿        0 次（阈值 {STALL_THRESHOLD_S:.2f}s）')
    lines.append(f'  结论        {summary["verdict"]}：{summary["reason"]}')
    lines.append('')

    ladder = summary.get('depths') or {}
    if ladder:
        lines.append('预取深度对照（同一批耗时，换成不同深度重排时间轴）')
        for depth, item in sorted(ladder.items()):
            if item['stall_count']:
                detail = f'{item["stall_count"]} 次 · 累计 {_fmt_clock(item["stall_s"])}'
            else:
                detail = '0 次'
            mark = '  ← 当前' if depth == summary['depth'] else ''
            lines.append(f'  深度 {_pad(str(depth), 2, True)}  {detail}{mark}')
        lines.append('')

    advice = summary.get('advice') or []
    if advice:
        lines.append('建议')
        lines.extend(f'  * {item}' for item in advice)
        lines.append('')
    return lines


def _fmt_clock(seconds: float) -> str:
    """秒数 -> `12.34s`（超过一分钟再补 `（1.2 分钟）`）。"""
    if seconds < 60:
        return f'{seconds:.2f}s'
    return f'{seconds:.2f}s（{seconds / 60:.1f} 分钟）'


def build_advice(rtf: float, sim: Dict[str, Any], depths: Dict[str, Dict[str, Any]],
                 depth: int) -> List[str]:
    """按结论给可操作的建议；数字全部来自本轮测量，不写「大概」「可能」。"""
    advice: List[str] = []
    count = len(sim.get('start_s') or [])
    if rtf > 1.0:
        better = [(name, item) for name, item in sorted(depths.items())
                  if item['stall_count'] < sim['stall_count']]
        if better:
            best = min(better, key=lambda pair: (pair[1]['stall_count'], pair[1]['stall_s']))
            if best[1]['stall_count'] == 0:
                advice.append(f'本轮 {count} 句里，预取深度提到 {best[0]} 就没有卡顿了'
                              f'（界面目前是 {depth}）；稳态仍落后 {rtf - 1.0:.2f}，长文会继续累积延迟')
            else:
                advice.append(f'本轮 {count} 句里，预取深度提到 {best[0]} 可把卡顿从 '
                              f'{sim["stall_count"]} 次压到 {best[1]["stall_count"]} 次；'
                              f'稳态仍落后，听完一章要多等 {sim["wall_s"] - sim["audio_s"]:.0f} 秒')
        advice.append('稳态落后靠预取消不掉：把语速降下来（设置项 tts_rate，音色不变），'
                      '或换成合成更快的引擎/端点后再测一遍')
        advice.append(f'按当前速度听 1 小时音频要多等 {60 * (rtf - 1.0):.0f} 分钟')
    elif sim['stall_count']:
        faster = [(name, item) for name, item in sorted(depths.items())
                  if name > depth and item['stall_count'] == 0]
        if faster:
            advice.append(f'本轮 {count} 句里，把预取深度提到 {faster[0][0]} 就没有卡顿了'
                          f'（界面的深度是 {depth}）')
        advice.append('平均速度够、只在短句处等待：把切句的句子加长（短句的音频撑不住一次合成）'
                      '或加深预取，都能吸收这部分抖动')
    else:
        margin = max(0.0, 1.0 - rtf)
        speed = f'{1.0 / rtf:.2f}' if rtf > 0 else '—'
        advice.append(f'本轮 {count} 句无卡顿，余量 {margin:.2f}：合成比播放快 {speed} 倍')
        if rtf > 0.9:
            advice.append('余量不足一成本，语速再往上拉或换更慢的端点就会出现卡顿，改设置后重测一次')
    return advice


# ============================================================
# 命令行
# ============================================================


def _force_utf8() -> None:
    """控制台按 UTF-8 输出：正文与路径含中文，Windows 默认代码页会直接抛编码错误。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except (ValueError, OSError):
                pass


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='朗读测速：量合成速度能不能跟上播放速度（只读设置，不写缓存）')
    parser.add_argument('--engine', choices=('auto', 'edge', 'openai', 'system'),
                        help='覆盖朗读引擎（默认读插件设置里的 tts_engine）')
    parser.add_argument('--voice', help='覆盖音色名（tts_voice）')
    parser.add_argument('--rate', type=int, help='覆盖语速百分比（tts_rate，界面默认 100）')
    parser.add_argument('--order', help='覆盖自动模式的引擎顺序，如 edge,system（tts_order）')
    parser.add_argument('--base-url', dest='base_url', help='覆盖 OpenAI 兼容端点（tts_base_url）')
    parser.add_argument('--api-key', dest='api_key', help='覆盖端点 API Key（tts_api_key）')
    parser.add_argument('--model', help='覆盖端点模型名（tts_model）')
    parser.add_argument('--settings', type=Path, help='改用指定的设置文件（默认读插件设置）')
    parser.add_argument('--text', help='直接给要测的正文')
    parser.add_argument('--text-file', dest='text_file', type=Path, help='从文本文件读正文（自动判编码）')
    parser.add_argument('--document', type=Path, help='从书里取一章正文（.txt / .md / .epub）')
    parser.add_argument('--chapter', type=int, default=0, help='配合 --document：章序号，从 0 开始（默认 0）')
    parser.add_argument('--encoding', default='auto', help='配合 --document：文本编码（默认 auto）')
    parser.add_argument('--sentences', type=int, default=DEFAULT_SENTENCES,
                        help=f'最多测多少句（默认 {DEFAULT_SENTENCES}）')
    parser.add_argument('--depth', type=int,
                        help='覆盖预取深度（默认取朗读设置里的 tts_prefetch，'
                             '即界面上真正在跑的策略）')
    parser.add_argument('--json', dest='json_out', type=Path, help='把结果写成 JSON（存基线用）')
    parser.add_argument('--quiet', action='store_true', help='不打逐句表与进度，只打汇总')
    parser.add_argument('--selftest', action='store_true', help='只跑内部自检，不合成任何音频')
    return parser.parse_args(list(argv) if argv is not None else None)


def pick_text(args: argparse.Namespace, backend: Dict[str, Any]) -> Tuple[str, str]:
    """按命令行选正文，返回 (正文, 来源说明)。"""
    sources = [name for name, value in
               (('--text', args.text), ('--text-file', args.text_file), ('--document', args.document))
               if value]
    if len(sources) > 1:
        raise SystemExit(f'{"、".join(sources)} 只能用其中一个')
    if args.text:
        return str(args.text), '命令行文本（--text）'
    if args.text_file:
        text = backend['parser'].TxtParser.read_full_content(str(args.text_file), 'auto')
        return text, f'文本文件 {args.text_file.name}'
    if args.document:
        return _document_chapter(args, backend)
    return SAMPLE_TEXT, '内置样本文本（不来自任何一本书）'


def _document_chapter(args: argparse.Namespace, backend: Dict[str, Any]) -> Tuple[str, str]:
    """从书里取一章正文：txt 走偏移切片（与阅读器同一实现），md/epub 走白名单 HTML 再取文本。"""
    path = args.document
    if not path.is_file():
        raise SystemExit(f'文档不存在: {path}')
    kind = {'.txt': 'txt', '.md': 'md', '.markdown': 'md', '.epub': 'epub'}.get(path.suffix.lower())
    if not kind:
        raise SystemExit(f'只支持 .txt / .md / .epub: {path.suffix}')
    index = max(0, int(args.chapter))

    if kind == 'txt':
        chapters, offsets = backend['parser'].TxtParser.parse_txt(str(path), args.encoding)
        if not offsets:
            raise SystemExit(f'解析不出任何章节: {path}')
        if index >= len(offsets):
            raise SystemExit(f'章序号越界：这本书有 {len(offsets)} 章，--chapter {index} 超出范围')
        content = backend['parser'].TxtParser.read_full_content(str(path), args.encoding)
        start, end = offsets[index]
        title = str(chapters[index].get('title') or f'第 {index + 1} 章')
        return content[int(start):int(end)], f'{path.name} 第 {index + 1} 章《{title}》'

    document = backend['documents'].open_document(str(path), kind, encoding=args.encoding)
    chapters = document.chapters()
    if index >= len(chapters):
        raise SystemExit(f'章序号越界：这本书有 {len(chapters)} 章，--chapter {index} 超出范围')
    html = document.chapter_html(index)
    title = str(chapters[index].get('title') or f'第 {index + 1} 章')
    return html_to_text(html), f'{path.name} 第 {index + 1} 章《{title}》'


def _progress_printer(row: Dict[str, Any], total: int) -> None:
    """逐句进度走 stderr：stdout 留给报告，方便重定向到文件。"""
    audio = _seconds(row['audio_s'])
    print(f'  [{row["index"] + 1:>3}/{total}] {row["chars"]:>4} 字 · 音频 {audio}s · '
          f'合成 {row["synth_s"]:.2f}s', file=sys.stderr, flush=True)


def _engine_label(engine: Dict[str, Any], params: Dict[str, Any]) -> str:
    data = engine.status(params)
    labels = {item['name']: item['label'] for item in data.get('engines') or []}
    mode = params['mode']
    if mode in labels:
        return f'{mode}（{labels[mode]}）'
    available = [item['name'] for item in data.get('engines') or [] if item.get('available')]
    return f'auto → 可用 {", ".join(available) or "（无）"}（顺序 {", ".join(params["order"])}）'


def _voice_line(params: Dict[str, Any]) -> str:
    return f'{params["voice"] or "（引擎默认）"} · 语速 {int(params["rate"]):+d}%'


def _endpoint_line(params: Dict[str, Any]) -> str:
    """端点一行：只在端点这一档（或自动模式）才有意义，系统音色与它无关。"""
    mode = str(params.get('mode') or 'auto')
    if mode not in ('auto', 'openai'):
        return ''
    base = str(params.get('base_url') or '').strip()
    if not base:
        return '（未配置）'
    model = str(params.get('model') or '').strip()
    return f'{base} · 模型 {model}' if model else base


def run(args: argparse.Namespace) -> int:
    backend = load_backend()
    engine = backend['engine']
    path = args.settings or settings_path()
    stored = read_settings(path)
    overrides = {
        'tts_engine': args.engine,
        'tts_voice': args.voice,
        'tts_rate': args.rate,
        'tts_order': args.order,
        'tts_base_url': args.base_url,
        'tts_api_key': args.api_key,
        'tts_model': args.model,
    }
    params, _merged = resolve_params(backend, stored, overrides)

    mode = params['mode']
    if mode in engine.ENGINES:
        if not engine.engine_available(mode, params):
            print(f'引擎 {mode} 当前不可用（未安装 / 未配置端点 / 平台不支持），换个引擎再测：'
                  f'--engine system', file=sys.stderr)
            return 2
    elif not engine.available_engines(params):
        print('没有任何可用的朗读引擎：装 edge-tts、配置端点，或用 --engine system', file=sys.stderr)
        return 2

    text, source = pick_text(args, backend)
    pieces = engine.split_text(text, 240)
    pieces = [piece for piece in pieces if piece.strip()]
    limit = max(1, int(args.sentences))
    if len(pieces) > limit:
        pieces = pieces[:limit]
    if not pieces:
        print('没切出任何句子：换一段正文，或换 --chapter 再测', file=sys.stderr)
        return 2

    if not args.quiet:
        print(f'开始测速：{source} · {len(pieces)} 句（引擎 {mode}）', file=sys.stderr)
    progress = None if args.quiet else _progress_printer
    rows, error = measure(pieces, params, engine.synthesize, progress)
    if error:
        print(f'{error}\n已完成的 {len(rows)} 句结果不作结论', file=sys.stderr)
        return 2

    usable = [row for row in rows if row['audio_s']]
    if not usable:
        print('一句都没能算出音频时长（音频格式不认识？），无法判断速度', file=sys.stderr)
        return 2
    durations = [float(row['audio_s']) for row in usable]
    synth_seconds = [float(row['synth_s']) for row in usable]

    # 深度默认取朗读设置里的值：测出来的时间轴必须是界面上真正在跑的那一套
    depth = int(args.depth) if args.depth is not None else int(params['prefetch'])
    depth = max(1, depth)
    sim = simulate(durations, synth_seconds, depth)
    depths = {value: simulate(durations, synth_seconds, value)
              for value in sorted(set(DEPTH_LADDER) | {depth})}
    rtf = realtime_factor(durations, synth_seconds)
    verdict, reason = judge(rtf, sim['stall_count'])

    # 逐句表里的「等待」按本轮深度算出来（与 sim 的时间轴同源）
    wait_by_index = {row['index']: gap for row, gap in zip(usable, sim['gaps_s'], strict=True)}
    for row in rows:
        row['wait_s'] = wait_by_index.get(row['index'])

    used = sorted({row['engine'] for row in usable if row['engine']})
    summary: Dict[str, Any] = {
        'settings_path': str(path),
        'engine_mode': mode,
        'engine_label': _engine_label(engine, params),
        'engines_used': used,
        'mixed_engines': len(used) > 1,
        'voice': params['voice'],
        'voice_line': _voice_line(params),
        'endpoint_line': _endpoint_line(params),
        'rate': params['rate'],
        'text_source': source,
        'sentence_count': len(usable),
        'char_count': sum(row['chars'] for row in usable),
        'synth_s': sum(synth_seconds),
        'depth': depth,
        'depth_source': ('--depth 覆盖' if args.depth is not None
                         else '朗读设置的 tts_prefetch，时间轴按它算'),
        'rtf': rtf,
        'sim': sim,
        'depths': depths,
        'verdict': verdict,
        'reason': reason,
        'unknown_duration': len(rows) - len(usable),
    }
    summary['advice'] = build_advice(rtf, sim, depths, depth)

    for line in render_report(summary, rows, quiet=args.quiet):
        print(line)

    if args.json_out:
        payload = {
            'tool': 'tts_speed',
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'settings_path': str(path),
            # params 里有 api_key，写进文件等于把凭据抄一份出来 —— 只留会影响到速度的键
            'params': {key: params[key] for key in ('mode', 'voice', 'rate', 'order',
                                                    'base_url', 'model')},
            'text_source': source,
            'rows': rows,
            'summary': {key: value for key, value in summary.items() if key != 'advice'},
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\n已写入 {args.json_out}')

    return 0 if (verdict == '跟得上' and rtf <= 1.0) else 1


# ============================================================
# 自检（不联网、不出声：只验算法）
# ============================================================


def _fake_mp3(frames: int) -> bytes:
    """`frames` 个 128 kbps / 44.1 kHz / MPEG1 Layer III 的静音帧（每帧 417 字节）。"""
    header = bytes((0xFF, 0xFB, 0x90, 0x00))
    return b''.join(header + b'\x00' * 413 for _ in range(frames))


def _fake_wav(seconds: float, rate: int = 44100, channels: int = 1, bits: int = 16) -> bytes:
    """一段静音 WAV（RIFF 头 + fmt + data）。"""
    byte_rate = rate * channels * bits // 8
    frames = int(byte_rate * seconds)
    payload = b'\x00' * (frames + frames % 2)
    header = (
        b'RIFF' + (36 + len(payload)).to_bytes(4, 'little') + b'WAVE'
        + b'fmt ' + (16).to_bytes(4, 'little')
        + (1).to_bytes(2, 'little') + channels.to_bytes(2, 'little')
        + rate.to_bytes(4, 'little') + byte_rate.to_bytes(4, 'little')
        + (channels * bits // 8).to_bytes(2, 'little') + bits.to_bytes(2, 'little')
        + b'data' + len(payload).to_bytes(4, 'little')
    )
    return header + payload


def _fake_ogg(seconds: float) -> bytes:
    """一个 Opus 的 Ogg 流：首页带 OpusHead，末页带 granule（48 kHz 计数）。"""
    def page(granule: int, payload: bytes, header_type: int = 0) -> bytes:
        segment = bytes([len(payload)])
        head = (b'OggS' + bytes([0, header_type]) + granule.to_bytes(8, 'little')
                + (1).to_bytes(4, 'little') + (0).to_bytes(4, 'little')
                + (0).to_bytes(4, 'little') + bytes([1]))
        return head + segment + payload

    ident = b'OpusHead' + bytes([1, 1]) + (312).to_bytes(2, 'little') \
        + (48000).to_bytes(4, 'little') + bytes([0, 0, 0])
    return (page(0, ident, header_type=2)
            + page(int(seconds * 48000), b'\x00' * 20, header_type=4))


def selftest() -> int:
    """内部自检：时长解析与队列模型的固定答案。返回 0 表示全部通过。"""
    failures: List[str] = []

    def check(name: str, got: Any, want: Any) -> None:
        if got != want:
            failures.append(f'{name}: 期望 {want!r}，实际 {got!r}')

    check('WAV 1.0s', probe_duration(_fake_wav(1.0), '.wav')[0], 1.0)
    frames = 40
    check('MP3 40 帧', round(probe_duration(_fake_mp3(frames), '.mp3')[0] or 0, 4),
          round(frames * 1152 / 44100, 4))
    check('MP3 带 ID3v2 头', round(probe_duration(b'ID3\x04\x00\x00\x00\x00\x00\x00'
                                                 + _fake_mp3(frames), '.mp3')[0] or 0, 4),
          round(frames * 1152 / 44100, 4))
    check('OGG 1.5s', probe_duration(_fake_ogg(1.5), '.ogg')[0], 1.5)
    check('后缀不对按文件头认', probe_duration(_fake_wav(0.5), '.mp3')[0], 0.5)

    fast = simulate([1.0, 1.0, 1.0], [0.2, 0.2, 0.2], 1)
    check('快速合成无卡顿', fast['stall_count'], 0)
    check('快速合成音频总长', round(fast['audio_s'], 4), 3.0)
    check('第一句的合成本身也是等待', round(fast['wall_s'], 4), 3.2)
    slow = simulate([1.0, 1.0, 1.0], [0.2, 1.5, 0.2], 1)
    check('上一句撑不住就要等', slow['stall_count'], 1)
    check('等待只落在后一句', [round(g, 4) for g in slow['gaps_s']], [0.0, 0.5, 0.0])
    # 同样一批耗时：第 3 句合成很慢，深度 1 会卡，深度 2 在上一句开播前就发出请求，正好吸收
    slow_tail = [0.2, 0.5, 1.5]
    check('深度 1 卡在最后一句', simulate([1.0, 1.0, 1.0], slow_tail, 1)['stall_count'], 1)
    check('深度 2 吸收等待', simulate([1.0, 1.0, 1.0], slow_tail, 2)['stall_count'], 0)
    check('RTF 大于 1', round(realtime_factor([1.0, 1.0], [1.5, 1.5]), 4), 1.5)
    check('稳态落后结论', judge(1.5, 0)[0], '跟不上')
    check('局部卡顿结论', judge(0.6, 2)[0], '局部卡顿')
    check('无卡顿且有余量', judge(0.6, 0)[0], '跟得上')

    backend = load_backend()
    pieces = backend['engine'].split_text(SAMPLE_TEXT, 240)
    check('样本文本可切句且无损', ''.join(pieces), SAMPLE_TEXT)
    check('样本文本句子数', len(pieces) > 6, True)

    if failures:
        print('自检失败：', file=sys.stderr)
        for item in failures:
            print(f'  - {item}', file=sys.stderr)
        return 1
    print('自检通过：音频时长解析、播放队列模型、RTF 与结论判定、样本文本切句')
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8()
    args = _parse_args(argv)
    if args.selftest:
        return selftest()
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
