"""edge-tts 这一档：流式合成、字级时间戳与中文音色清单。

`edge_tts` 是动态依赖（已登记在 HIDDEN_IMPORTS），没装时 `available()` 返回 False，
降级链自然落到下一档。本模块不 import 兄弟模块；入口把它的三个函数交给
`SpeechEngine` 与设置面板。

**超时**：合成是流式网络读，没有超时上限时，对端把连接挂住会让请求线程无限等下去
（表现是"点朗读后一直转圈"，连错误都没有）。判据取"这么久没有收到新的数据块"，
而不是总时长 —— 长文本合成得久是正常现象，网络卡住的表现是再也没有下一块。
超时抛 `SynthesisTimeout`（`retryable = False`：等满一轮超时已经是这一次的上限，
在同一个引擎上再重试只是把请求拖长数倍），由引擎的降级链换成下一档或如实报错。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_VOICE = 'zh-CN-XiaoxiaoNeural'

#: 流空闲超时（秒）：这么久没有新的数据块就判超时。
STREAM_IDLE_TIMEOUT_SECONDS = 20.0


class SynthesisTimeout(RuntimeError):
    """合成超时（可识别：调用方据此提示"网络太慢"而不是"引擎坏了"）。

    `retryable` 由引擎的 `_attempt_engine` 读取：False 表示原地重试不会变好。
    """

    retryable = False


def available(params: Dict[str, Any]) -> bool:
    """装了 edge_tts 就能用；网络问题在合成时才会暴露。"""
    return importlib.util.find_spec('edge_tts') is not None


async def _stream_chunks(communicate, timeout: float):
    """按块读 `communicate.stream()`，每块最多等 `timeout` 秒。

    `asyncio.wait_for` 只包**单次读取**：它取消的是"这一次等数据"，整段合成不设
    总时长上限。超时抛 `SynthesisTimeout`，因此调用方拿到的是可识别的错误而不是
    永远挂起的 await。
    """
    iterator = communicate.stream().__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout)
        except StopAsyncIteration:
            return
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise SynthesisTimeout(
                f'edge-tts 合成超时（{timeout:g} 秒内没有收到新的音频数据）') from exc
        yield chunk


async def _edge_stream(text: str, voice: str, rate: str) -> Tuple[bytes, List[dict]]:
    """流式取音频与字级时间戳。

    时间戳的 `offset` 是"从合成开始算"的 100ns 计数，**包含网络等待**：
    库里那句 `state["chunk_audio_bytes"]` 正好是"这一帧之前已经产出了多少字节音频"，
    拿它当锚点就能把"网络延迟"从时间轴上减掉（实测首帧前约有几百毫秒的等待，
    不校正的话高亮会整体滞后，听起来就像高亮跟不上）。
    """
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, rate=rate, boundary='WordBoundary')
    audio = bytearray()
    marks: List[dict] = []
    anchor: Optional[int] = None
    async for chunk in _stream_chunks(communicate, STREAM_IDLE_TIMEOUT_SECONDS):
        if chunk['type'] == 'audio':
            if anchor is None:
                anchor = communicate.state['chunk_audio_bytes']
            audio += chunk.get('data') or b''
        elif chunk['type'] == 'WordBoundary':
            if anchor is None:
                anchor = communicate.state['chunk_audio_bytes']
            offset = int(chunk.get('offset') or 0)
            marks.append({
                'text': chunk.get('text') or '',
                'chars': len(chunk.get('text') or ''),
                # 相对音频起点的时间轴：offset 减去首帧音频到位时的字节数
                'startMs': round((offset - anchor) / 10000.0, 1),
                'durationMs': round(int(chunk.get('duration') or 0) / 10000.0, 1),
            })
    marks.sort(key=lambda m: m['startMs'])
    return bytes(audio), marks


def synthesize(text: str, params: Dict[str, Any]) -> Tuple[bytes, str, List[dict]]:
    import edge_tts  # noqa: F401 - 明确报错来源：没装就是没装

    voice = str(params.get('voice') or DEFAULT_VOICE)
    rate = f"{int(params.get('rate') or 0):+d}%"
    audio, marks = asyncio.run(_edge_stream(text, voice, rate))
    if not audio:
        raise RuntimeError('edge-tts 没有返回音频')
    return audio, '.mp3', marks


# 音色名的可读说明：edge 端点只给 ShortName / Gender / 人格标签，
# 中文语种名与"适合什么"得自己补，否则列表里全是 zh-CN-XxxNeural 没法挑。
_VOICE_NOTES = {
    'zh-CN-XiaoxiaoNeural': '晓晓 · 女 · 通用最自然',
    'zh-CN-XiaoyiNeural': '晓伊 · 女 · 年轻活泼',
    'zh-CN-YunxiNeural': '云希 · 男 · 叙述节奏快',
    'zh-CN-YunyangNeural': '云扬 · 男 · 播报腔最稳',
    'zh-CN-YunjianNeural': '云健 · 男 · 解说腔',
    'zh-CN-YunxiaNeural': '云夏 · 男童',
    'zh-CN-liaoning-XiaobeiNeural': '晓北 · 东北话',
    'zh-CN-shaanxi-XiaoniNeural': '晓妮 · 陕西话',
    'zh-HK-HiuGaaiNeural': '曉佳 · 粤语',
    'zh-HK-HiuMaanNeural': '曉曼 · 粤语',
    'zh-HK-WanLungNeural': '雲龍 · 粤语',
    'zh-TW-HsiaoChenNeural': '曉臻 · 台湾国语',
    'zh-TW-HsiaoYuNeural': '曉雨 · 台湾国语',
    'zh-TW-YunJheNeural': '雲哲 · 台湾国语',
}


def voices() -> List[Dict[str, Any]]:
    """列出 edge 端点上**真实可用**的中文音色。

    为什么必须问端点而不是写死一份表：微软会调整音色池（实测有些音色已经下线，
    写死的列表点一下就报"找不到音色"）。这里失败时返回空列表 —— 前端退回内置的
    常用几个，不会因为断网就没得选。
    """
    try:
        import edge_tts
    except ImportError:
        return []

    async def fetch() -> List[Dict[str, Any]]:
        listing = await edge_tts.list_voices()
        result = []
        for voice in listing:
            short = voice.get('ShortName') or ''
            if not short.lower().startswith('zh'):
                continue
            locale = voice.get('Locale') or ''
            gender = voice.get('Gender') or ''
            result.append({
                'name': short,
                'locale': locale,
                'gender': '女' if gender == 'Female' else ('男' if gender == 'Male' else gender),
                'note': _VOICE_NOTES.get(short, ''),
            })
        # 常用音色排在前面，其余按名字稳定排序
        order = {name: index for index, name in enumerate(_VOICE_NOTES)}
        result.sort(key=lambda item: (order.get(item['name'], 999), item['name']))
        return result

    try:
        return asyncio.run(fetch())
    except Exception as e:
        log.warning(f'[document-reader] 取 edge 音色列表失败（将退回内置常用列表）: {e}')
        return []
