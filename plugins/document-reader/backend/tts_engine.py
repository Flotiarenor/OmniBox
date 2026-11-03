"""朗读引擎：同一段文本、三种来源、一个接口。

放在 document-reader 的 backend/ 下（而不是独立插件）是因为朗读已经并进阅读器：
宿主自己的状态机才知道"当前读到哪一段、翻页后该续哪里"，跨进程/跨 iframe 拿不到。

三档引擎，按音质降序（各自实现在 `tts_edge.py` / `tts_endpoint.py` / `tts_system.py`）：

  edge  —— edge-tts（微软 Edge 的神经音色，晓晓/云希这一档）。联网，音质最好，
           并且**带字级时间戳**（WordBoundary），逐字跟随靠它。
  openai——任何 OpenAI 兼容的 `POST /v1/audio/speech`（Kokoro / GPT-SoVITS / 云 API）。
           **不带模型**：模型跑在用户自己的服务里，这里只当客户端。没有时间戳。
  system——Windows 自带的 System.Speech（SAPI5，Huihui/Yaoyao/Kangkang）。完全离线，
           音质最差，定位是"断网/没配端点时至少能出声"。通过 PowerShell 子进程调用，
           因此**不新增任何第三方依赖**（win32com 也是同一批音色，白搭一个依赖）。

设计约束（都是踩过或明确权衡过的）：

* 每个引擎只做一件事：`(文本, 参数) -> (音频字节, 后缀, 时间戳)`。谁都不碰 HTML。
* 失败必须能被降级链吃掉：任何一个引擎抛异常，`synthesize` 按顺序试下一个；
  全挂就把最后一个异常抛给调用方，由它转成给用户看的一句话。
* 时间戳一律换算成**毫秒**再往外给（edge 的 100ns 单位留在那一层）。
* `mode='auto'` 时的顺序就是 ENGINES 的顺序，可被 `order` 覆盖；
  定了具体引擎还失败就**不**悄悄换音色（用户选了 A 却听到 B 是更糟的体验）。

组件形态：本模块不 import 兄弟模块。三个引擎的"能不能用 / 合成 / 音色归属"由入口
装配成映射注入 `SpeechEngine`，所以同一个类可以脱离插件单独测。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, ClassVar, Dict, List, Mapping, Optional, Tuple

log = logging.getLogger(__name__)

# 一次合成的文本上限（字符）。太长会让"点播放"到"听见声音"之间干等，
# 前端的切句器已经保证不会送这么长，这里是后端的第二道闸。
MAX_CHARS = 4000

ENGINES: Tuple[str, ...] = ('edge', 'openai', 'system')

ENGINE_LABELS = {
    'edge': 'edge-tts（神经音色，联网）',
    'openai': 'OpenAI 兼容端点（本地模型/云服务）',
    'system': '系统离线音色（SAPI5，兜底）',
}

# edge 的默认音色：晓晓，通用最自然，也是 Edge 朗读本身的默认
DEFAULT_VOICE = 'zh-CN-XiaoxiaoNeural'

# 合成结果：(音频字节, 后缀, 字级时间戳)
Synthesis = Tuple[bytes, str, List[dict]]
Runner = Callable[[str, Dict[str, Any]], Synthesis]
Availability = Callable[[Dict[str, Any]], bool]
VoiceNames = Callable[[str, Dict[str, Any]], Optional[List[str]]]


def is_edge_voice(name: str) -> bool:
    """这个音色名像不像 edge 的 ShortName（`zh-CN-XiaoxiaoNeural`、`zh-HK-HiuGaaiNeural`…）。

    判据是 `zh-` 前缀 **加** `Neural` 后缀两条都要：只看前缀会把 `zh-female-01`
    这种自定义端点的音色名当成 edge 的，从而被替换掉（用户选的音色被悄悄改掉）。

    用来挡住"把 edge 的名字填进 OpenAI 兼容端点"这一种误配：端点上没有这个名字时
    不会报错，只会静默换成它自己的说话人（实测 Qwen3-TTS 就是退回第一个音色），
    用户听到的是别人，且完全没有提示。
    """
    text = str(name or '').strip().lower()
    return text.startswith('zh-') and text.endswith('neural')


_SENTENCE_END = '。！？；…!?;\n'

# 句末标点后至少攒够这么多字符才切一刀：中文短句常只有 6~8 字（"明天会晴吗？"），
# 阈值定高了会把好几个短句并成一片，逐句高亮就变成了"一大段一起亮"。
# 只有标点没有内容时不切（"。"、"……"），避免切出一堆空转的片段。
MIN_SENTENCE_CHARS = 4


def split_text(text: str, max_chars: int = 240) -> List[str]:
    """把一段正文切成"一句一片"。

    切点是句末标点（含换行），超长句再按逗号退让。**返回值拼接后必须与原文逐字节相等**
    —— 前端的字级高亮按"字符偏移量"定位，切文本时丢掉或改写一个标点，读到第 3 句
    高亮就会整体错位。所以这里只做切分，绝不 strip、绝不补标点。
    """
    if not text:
        return []
    pieces: List[str] = []
    buffer = ''
    for char in text:
        buffer += char
        if char in _SENTENCE_END and len(buffer) >= MIN_SENTENCE_CHARS:
            pieces.append(buffer)
            buffer = ''
        elif len(buffer) >= max_chars:
            # 超长句（无标点的长段落）：从缓冲里找最后一个逗号断，找不到就硬断
            cut = max(buffer.rfind('，'), buffer.rfind(','), buffer.rfind('、'))
            if cut >= MIN_SENTENCE_CHARS:
                pieces.append(buffer[:cut + 1])
                buffer = buffer[cut + 1:]
            else:
                pieces.append(buffer)
                buffer = ''
    if buffer:
        pieces.append(buffer)
    return pieces


# 同一引擎的尝试次数：edge 是网络调用，偶发失败（握手超时、连接被重置）很常见。
# 一次就降级等于把偶发当永久 —— 用户会听到系统音色或旧缓存，却再也不会回到首选引擎。
ENGINE_ATTEMPTS = 3
# 重试间隔：网络抖动很快过去，等太久会让"点朗读后半天不响"。
RETRY_DELAY_SECONDS = 0.4


class SpeechEngine:
    """三档引擎的分派与降级链。

    `runners` / `availability` 按引擎名给出"怎么合成"与"现在能不能用"，
    `voice_names` 回答"这个引擎有没有这个音色"（问不到时返回 None）。
    三者都由入口装配，本类不 import 具体引擎模块。
    """

    ENGINES: ClassVar[Tuple[str, ...]] = ENGINES
    DEFAULT_VOICE: ClassVar[str] = DEFAULT_VOICE
    MAX_CHARS: ClassVar[int] = MAX_CHARS

    @staticmethod
    def split_text(text: str, max_chars: int = 240) -> List[str]:
        """切句入口（实现见模块级 `split_text`）。"""
        return split_text(text, max_chars)

    @staticmethod
    def is_edge_voice(name: str) -> bool:
        """音色归属判定（实现见模块级 `is_edge_voice`）。"""
        return is_edge_voice(name)

    def __init__(self, runners: Mapping[str, Runner],
                 availability: Mapping[str, Availability],
                 voice_names: VoiceNames) -> None:
        self._runners = dict(runners)
        self._availability = dict(availability)
        self._voice_names = voice_names

    def engine_available(self, engine: str, params: Dict[str, Any]) -> bool:
        """这个引擎现在能不能用（不实际合成，只判断前置条件）。"""
        probe = self._availability.get(engine)
        return bool(probe(params)) if probe is not None else False

    def available_engines(self, params: Dict[str, Any]) -> List[str]:
        return [name for name in ENGINES if self.engine_available(name, params)]

    def _attempt_engine(self, engine: str, text: str, params: Dict[str, Any],
                        errors: List[str]) -> Tuple[Optional[Synthesis], bool]:
        """同一引擎重试 `ENGINE_ATTEMPTS` 次；失败返回 (None, 是否还有别的可试)。

        只有"还有重试次数"时才算可重试 —— 参数类错误（如端点返回 400）重试也是白等，
        但网络类错误（超时、连接重置）重试往往就好了，而调用方无法可靠区分两者，
        所以统一重试：代价是最多多等 0.8 秒，收益是不必降到系统音色。
        """
        runner = self._runners[engine]
        last_error: Optional[Exception] = None
        for attempt in range(1, ENGINE_ATTEMPTS + 1):
            try:
                return runner(text, params), True
            except Exception as e:
                last_error = e
                log.warning(f'[document-reader] 朗读引擎 {engine} 第 {attempt}/'
                            f'{ENGINE_ATTEMPTS} 次失败: {e}')
                if attempt < ENGINE_ATTEMPTS:
                    time.sleep(RETRY_DELAY_SECONDS * attempt)
        errors.append(f'{engine}: {last_error}')
        return None, False

    def engine_has_voice(self, engine: str, voice: str, params: Dict[str, Any]) -> bool:
        """这个引擎有没有 `voice` 这个音色；问不到音色清单时一律返回 True。

        用途是自动模式下的**归属判定**：用户在自动模式里选了 `vivian`（端点的音色），
        顺序里的 edge 没有它 —— 从前会拿 edge 念一遍"晓晓"再降级，现在是直接跳过 edge，
        由真正拥有这个音色的引擎来念（都不拥有就落到 system 兜底）。

        两种情况下返回 True、照旧尝试，而不是判它"没有"：
          * 音色清单问不到（端点没实现 /v1/voices、断网）—— 没有依据说它不支持；
          * 音色名是空的（用引擎自己的默认音色），或该引擎的音色名的确包含它。

        `system` 与设置里的音色名不同域，一律返回 True（见 `engine_voice_names`）。
        """
        want = str(voice or '').strip()
        if not want:
            return True
        names = self._voice_names(engine, params)
        if names is None:
            return True
        return want in names

    def synthesize(self, text: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """合成一段文本，返回 `{audio, ext, marks, engine}`。

        `mode` 为具体引擎名时只用它（失败直接抛，不偷偷换音色）；为 `auto` 时按
        `order`（默认 ENGINES）逐个尝试，**每个引擎先重试 `ENGINE_ATTEMPTS` 次**，
        都失败才降级到下一个。全部失败则抛出最后一个异常。

        自动模式还会先按音色归属筛一遍顺序：**只剔除"确实没有这个音色"的引擎**
        （音色属于哪一档就用哪一档），问不到清单的引擎留在顺序里照旧尝试。
        """
        text = (text or '').strip()
        if not text:
            raise ValueError('没有可朗读的文本')
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]

        mode = str(params.get('mode') or 'auto')
        if mode in self._runners:
            order = [mode]
        else:
            raw_order = params.get('order') or ENGINES
            order = [str(name) for name in raw_order if name in self._runners]
            voice = str(params.get('voice') or '').strip()
            if voice:
                owned = [name for name in order if self.engine_has_voice(name, voice, params)]
                if owned and len(owned) < len(order):
                    skipped = [name for name in order if name not in owned]
                    log.info(f'[document-reader] 音色 {voice} 属于 {owned}，自动模式跳过 {skipped}')
                    order = owned

        errors: List[str] = []
        for engine in order:
            if not self.engine_available(engine, params):
                errors.append(f'{engine}: 不可用（未安装/未配置/平台不支持）')
                continue
            result, _ = self._attempt_engine(engine, text, params, errors)
            if result is not None:
                audio, ext, marks = result
                return {'audio': audio, 'ext': ext, 'marks': marks, 'engine': engine}

        raise RuntimeError('所有朗读引擎都不可用 —— ' + '；'.join(errors))

    def status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """给前端/设置面板看的一览：每个引擎可用性与说明。"""
        return {
            'engines': [
                {
                    'name': name,
                    'label': ENGINE_LABELS[name],
                    'available': self.engine_available(name, params),
                    'marks': name == 'edge',  # 是否支持字级时间戳（逐字跟随）
                }
                for name in ENGINES
            ],
            'order': list(params.get('order') or ENGINES),
        }
