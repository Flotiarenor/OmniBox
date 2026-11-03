"""朗读的插件层：把设置项翻成引擎参数、切句、按内容缓存音频、下发音色表。

引擎本身在 `tts_engine.py`（分派与降级）、`tts_edge.py` / `tts_endpoint.py` /
`tts_system.py`（三档实现）；缓存目录与清理在 `tts_cache.py`。本组件只做"设置 ↔ 引擎"
的翻译与落盘编排，三档引擎选择逻辑一概不在这里。

文本从哪来：**由前端给**（它读的是已经渲染进 DOM 的当前章正文），而不是后端按章节号
重新解析一遍。三个理由：

  1. txt/md/epub 三种格式在前端 `.chapter-content` 那一层已经归一成同一份正文，
     后端再解析就要把格式分支写第二遍；
  2. 用户"从这一段开始读"的位置只有 DOM 知道，后端拿不到；
  3. 缓存键直接用文本内容算，同一段文字重复合成天然命中，不用跟章节号、编码、
     解析版本这些上下文纠缠。

代价是后端不校验文本内容，只限长度 —— 这是同一台机器上的同源调用，可接受。

组件形态：本模块不 import 兄弟模块。引擎、音色清单、缓存与宿主的 `setting` /
`_raw_settings` / `_image_url` / `open_with_system` 全部由入口注入。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

from shell.backend.ports import SettingsPort

log = logging.getLogger(__name__)


def normalize_params(settings: SettingsPort, raw: Mapping[str, Any],
                     speech: Any) -> Dict[str, Any]:
    """把设置项整理成引擎参数。语音名交给引擎自己解释（各家音色命名不同）。

    这里是"参数整理"的唯一实现：测速工具 `tool/tts_speed.py` 直接复用本函数，
    所以不要在两处各写一份 —— 漏掉下面那条 openai 音色替换规则，测的就是另一个
    声音了。
    """
    order = [item.strip() for item in str(settings.setting('tts_order') or '').split(',')]
    mode = str(settings.setting('tts_engine') or 'auto')
    voice = str(settings.setting('tts_voice') or '').strip() or speech.DEFAULT_VOICE
    if mode == 'openai' and speech.is_edge_voice(voice):
        # edge 的 ShortName（默认的晓晓、用户先前选的晓伊…）对端点没有意义，
        # 端点只会静默换成它自己的第一个说话人；改成 OpenAI 的通用名字，
        # 让端点自己去映射（Qwen3-TTS 把 alloy 映射成 vivian）。
        voice = 'alloy'
    return {
        'mode': mode,
        'order': [item for item in order if item] or list(speech.ENGINES),
        'voice': voice,
        'rate': int(settings.setting('tts_rate') or 0),
        'base_url': str(settings.setting('tts_base_url') or ''),
        # 凭据要读未脱敏的设置：get_settings() 返回的是掩码
        'api_key': raw.get('tts_api_key') or '',
        'model': str(settings.setting('tts_model') or ''),
    }


def clamp_prefetch(raw: Any, minimum: int, maximum: int) -> int:
    """预取深度（句）：读不到或超出 [minimum, maximum] 一律退回 minimum。

    为什么配坏时退回 1 而不是默认值 5：能配出非法值说明这条设置链路不可信，而端点
    本来就慢时再并发五路合成是最糟的选择 —— 保守的 1 只表现为"偶尔卡一下"，
    激进的值会表现为"点了半天才出声"。前端另有一道同样的夹取（设置值会经 HTTP
    回来），两侧各夹一次，任何一边被绕过都不会把播放器带进并发狂潮。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return minimum
    if minimum <= value <= maximum:
        return value
    return minimum


class TtsService:
    """朗读的设置读取、切句、合成与缓存。"""

    def __init__(self, settings: SettingsPort, raw_settings: Callable[[], Dict[str, Any]],
                 image_url: Callable[[str], str],
                 open_with_system: Callable[[Path], None], speech: Any, voices: Any,
                 cache: Any, limits: Any, plugin_name: str) -> None:
        self._settings = settings
        self._raw_settings = raw_settings
        self._image_url = image_url
        self._open_with_system = open_with_system
        self._speech = speech
        self._voices = voices
        self._cache = cache
        self._limits = limits
        self._plugin_name = plugin_name

    # ===== 参数与状态 =====

    def params(self) -> Dict[str, Any]:
        return normalize_params(self._settings, self._raw_settings(), self._speech)

    def prefetch(self) -> int:
        return clamp_prefetch(self._settings.setting('tts_prefetch'),
                              self._limits.prefetch_min, self._limits.prefetch_max)

    def tts_status(self) -> Dict[str, Any]:
        """引擎可用性一览 + 当前配置，供阅读器的朗读面板显示。"""
        params = self.params()
        data = self._speech.status(params)
        data['mode'] = params['mode']
        data['voice'] = params['voice']
        data['rate'] = params['rate']
        # 预取深度也走这里下发：它是前端播放队列的行为参数，不是引擎参数
        # （引擎不关心"要几段"），但朗读面板与播放器都要知道当前值。
        data['prefetch'] = self.prefetch()
        return data

    # ===== 音色表 =====

    def tts_voices(self) -> Dict[str, Any]:
        """当前引擎可选的音色表：自动模式给两张（edge + 端点），指定引擎只给一张。

        音色名跨引擎不通用：`zh-CN-YunxiNeural` 是 edge 的名字，发给第三方端点会被它
        静默换成自己的默认说话人。因此这张表必须跟着 `tts_engine` 走，而不是永远问 edge
        （旧实现就是永远问 edge，于是选了 OpenAI 兼容端点的人拿不到一个能用的音色名）。

        `tables[].source` 的取值：
          * `edge`     —— edge 端点上真实可见的音色（问不到时 `unavailable`，前端用内置常用表）；
          * `endpoint` —— OpenAI 兼容端点自报的音色（`GET /v1/voices`，不是 OpenAI 规范的一部分，
                          普通服务没有这个路由，因此问不到是常态，不算错误）；
          * `system`   —— 系统离线音色不出表（它是兜底，SAPI 的音色名与设置里那个不同域）。
        """
        params = self.params()
        return {'mode': params['mode'], 'tables': self._voice_tables(params)}

    def _voice_tables(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """按引擎拼音色表：`auto` 两张，指定引擎一张，`system` 零张。"""
        mode = params['mode']
        if mode == 'system':
            return []
        if mode == 'edge':
            return [self._edge_table()]
        if mode == 'openai':
            return [self._endpoint_table(params)]

        # auto：边缘一张、端点一张，点哪张的哪个音色由 synthesize 按归属决定谁去念
        tables = []
        if 'edge' in params['order']:
            tables.append(self._edge_table())
        if 'openai' in params['order']:
            tables.append(self._endpoint_table(params))
        return tables

    def _edge_table(self) -> Dict[str, Any]:
        voices = self._voices.edge_voices()
        return {'source': 'edge', 'title': 'edge-tts 音色', 'voices': voices,
                'unavailable': not voices, 'note': 'zh-CN-XiaoxiaoNeural'}

    def _endpoint_table(self, params: Dict[str, Any]) -> Dict[str, Any]:
        base_url = str(params.get('base_url') or '').strip()
        names = (self._voices.endpoint_voices(base_url, params.get('api_key') or '',
                                              params.get('model') or '')
                 if base_url else None)
        return {'source': 'endpoint', 'title': 'OpenAI 兼容端点音色',
                'voices': [{'name': name, 'note': ''} for name in (names or [])],
                'unavailable': not names, 'note': ''}

    # ===== 切句与合成 =====

    def tts_segment(self, text: str, max_chars: int = 240) -> Dict[str, Any]:
        """把正文切成"一句一片"：前端按片请求音频，边合成边播，不用等整章。"""
        segments = self._speech.split_text(
            str(text or ''), max(40, min(int(max_chars or 240), 1000)))
        return {'segments': segments}

    def tts_speak(self, text: str, cache_key: str = '', segment_index: int = 0,
                  engine: str = '') -> Dict[str, Any]:
        """合成一段文本并返回可播放的同源 URL（+ 可选的逐字时间戳）。

        `cache_key` 由前端给：**文本内容本身**的哈希（同一段文字必定同一把钥匙）。
        不给就按文本现算，行为等价，只是前端能省掉一次传输。
        """
        text = str(text or '')
        if not text.strip():
            return {'error': '没有可朗读的文本'}
        if len(text) > self._speech.MAX_CHARS:
            return {'error': f'这一段太长（{len(text)} 字），请缩短后再朗读'}

        params = self.params()
        if engine and engine in self._speech.ENGINES:
            params['mode'] = engine
        cache_dir = self._cache.cache_dir()
        os.makedirs(cache_dir, exist_ok=True)

        prefix = self._cache.prefix(text, cache_key, params)
        cached = self._cache.hit(cache_dir, prefix)
        if cached:
            path, meta = cached
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = time.time()
            return {'url': self._image_url(str(path)), 'mtime': mtime, 'cached': True,
                    'engine': meta.get('engine', ''),
                    'marks': self._cache.meta_marks(prefix),
                    'segmentIndex': segment_index}

        try:
            result = self._speech.synthesize(text, params)
        except Exception as e:
            log.warning(f'[{self._plugin_name}] 朗读失败: {e}')
            return {'error': str(e)}
        if len(result['audio']) > self._limits.result_bytes:
            return {'error': '合成结果异常大，已放弃播放'}

        # 落盘路径再过一次包含判定：前缀已做过字符白名单，这里是纵深防御 ——
        # 音频与 .json 元数据都必须落在缓存目录内（审计项 P1-3）。
        path = self._cache.path(cache_dir, prefix + result['ext'])
        meta_path = self._cache.path(cache_dir, prefix + '.json')
        if path is None or meta_path is None:
            log.warning(f'[{self._plugin_name}] 朗读缓存路径越界，已放弃写入: {prefix!r}')
            return {'error': '缓存路径越界，已放弃写入'}
        try:
            with open(path, 'wb') as handle:
                handle.write(result['audio'])
            with open(meta_path, 'w', encoding='utf-8') as handle:
                json.dump({'engine': result['engine'], 'marks': result['marks']},
                          handle, ensure_ascii=False)
        except OSError as e:
            log.warning(f'[{self._plugin_name}] 朗读音频落盘失败: {e}')
            return {'error': f'音频写入失败: {e}'}

        self._cache.prune(cache_dir)
        return {'url': self._image_url(str(path)), 'mtime': os.path.getmtime(path),
                'cached': False, 'engine': result['engine'], 'marks': result['marks'],
                'segmentIndex': segment_index}

    # ===== 缓存管理 =====

    def tts_cache_info(self) -> Dict[str, Any]:
        return self._cache.info()

    def file_root(self):
        """`/file` 允许根里的朗读缓存父目录（见 `TtsCache.file_root`）。"""
        return self._cache.file_root()

    def tts_clear_cache(self) -> Dict[str, Any]:
        return self._cache.clear()

    def tts_open_cache_dir(self) -> Dict[str, Any]:
        """用系统文件管理器打开朗读缓存目录（用户在界面点"打开所在文件夹"）。

        路径不来自请求参数，而是缓存目录自身，所以不存在越界问题；仍然先确认它存在，
        避免系统弹出一个"找不到路径"的对话框。
        """
        cache_dir = self._cache.cache_dir()
        if not cache_dir.is_dir():
            return {'success': False, 'error': '缓存目录不存在'}
        try:
            self._open_with_system(cache_dir)
        except Exception as e:
            return {'success': False, 'error': f'调用文件管理器失败: {e}'}
        return {'success': True, 'path': str(cache_dir)}
