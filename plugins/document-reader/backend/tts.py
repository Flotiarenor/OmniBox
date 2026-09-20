"""tts 分片：朗读的插件层（引擎本身在 `tts_engine.py`）。

从 `main.py` 切出来，职责是"把设置项翻成引擎参数、切句、按内容缓存音频、把落盘结果
变成可播放的同源 URL"，引擎选择与降级逻辑一概不在这里。

文本从哪来：**由前端给**（它读的是已经渲染进 DOM 的当前章正文），而不是后端按章节号
重新解析一遍。三个理由：

  1. txt/md/epub 三种格式在前端 `.chapter-content` 那一层已经归一成同一份正文，
     后端再解析就要把格式分支写第二遍；
  2. 用户"从这一段开始读"的位置只有 DOM 知道，后端拿不到；
  3. 缓存键直接用文本内容算，同一段文字重复合成天然命中，不用跟章节号、编码、
     解析版本这些上下文纠缠。

代价是后端不校验文本内容，只限长度 —— 这是同一台机器上的同源调用，可接受。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from shell.backend.paths import get_plugins_config_dir
from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_tts = load_sibling(__file__, 'tts_engine', 'document_reader_tts')

# 缓存目录上限：到量就从最旧的开始删（音频 + 时间戳成对删）
TTS_CACHE_LIMIT = 200 * 1024 * 1024
# 单次合成结果的合理上限：超过它说明引擎返回了异常内容，别往磁盘写
TTS_RESULT_LIMIT = 40 * 1024 * 1024
_AUDIO_EXTS = ('.mp3', '.wav', '.ogg', '.opus', '.m4a')
# 缓存键形状：前端给的是文本指纹（SHA-1 前 32 位十六进制；没有 SubtleCrypto 时
# 是 `f<hex>`）。它会被拼成**文件名前缀**，所以只能接受十六进制 —— 一旦放开字符集，
# 一个 `../../../../tmp/x` 就是"以插件权限往任意目录写文件"（见 _tts_cache_prefix）。
_CACHE_KEY_RE = re.compile(r'^[0-9a-fA-F]{1,64}$')


class TtsMixin:
    """朗读的设置读取、切句、合成与缓存。"""

    # 缓存放在 `<config>/plugins/<插件名>/tts/`：
    #   * 跟其它状态放一起（`<config>` 本来就是本机状态的家），容易找到与清理；
    #   * 文档根目录换成外接盘/网络盘时，缓存不会散落在各个盘上；
    #   * `<config>` 已被 .gitignore 与打包门禁排除，不会混进产物。
    # 不放在文档目录的 `.document_state/` 里：那里埋得深、跟着文档根到处跑，
    # 用户想清缓存得先知道根目录在哪。
    _tts_cache_subdir = 'document-reader'

    # 子类（main.py 的插件类）提供：
    #   _raw_settings / _image_url
    #   （缓存目录由这里自己解析，不依赖文档根，见 `_tts_cache_dir`）

    def _tts_cache_dir(self) -> Path:
        """朗读缓存目录：`<config>/plugins/<插件名>/tts`。

        放在这里而不是文档根目录的 `.document_state/` 里：
          * `<config>` 本来就是本机状态的家，用户找得到、清得掉；
          * 文档根换成外接盘/网络盘时缓存不散落在各个盘上；
          * `<config>` 已被 .gitignore 与打包门禁（`tools/check_build_tree.py`）排除，
            不会混进发布产物。

        **纯路径计算，不建目录、不落盘**：只读的调用方（`get_file_roots()` /
        `tts_cache_info()`）不该因为问一次路径就造出一个空目录。需要写入的地方
        （`tts_speak`）自己 `mkdir`。
        """
        return get_plugins_config_dir() / self._tts_cache_subdir / 'tts'

    def _tts_cache_dir_for_write(self) -> Path:
        """取可写的缓存目录，必要时创建；`<config>` 不可写时退到系统临时目录。"""
        cache_dir = self._tts_cache_dir()
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning(f'[{self.name}] 朗读缓存目录不可写（{e}），改用临时目录')
            cache_dir = (Path(tempfile.gettempdir()) / 'omnibox-tts'
                         / self._tts_cache_subdir / 'tts')
            cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    # ===== 缓存管理（给界面用：用户要能看到它在哪、多大、能不能一键清） =====

    def tts_cache_info(self) -> Dict[str, Any]:
        """朗读缓存的位置与占用。界面把它显示出来，用户不必自己去翻目录。"""
        cache_dir = self._tts_cache_dir()
        files = 0
        total = 0
        try:
            for entry in os.scandir(cache_dir):
                if entry.is_file():
                    files += 1
                    total += entry.stat().st_size
        except OSError:
            pass
        return {'path': str(cache_dir), 'files': files,
                'bytes': total, 'limitBytes': TTS_CACHE_LIMIT}

    def tts_clear_cache(self) -> Dict[str, Any]:
        """清空朗读缓存。缓存按需重建，删掉不影响书签与阅读进度。

        "目录还不存在"不是错误 —— 从没合成过就是这状态（清理是幂等的）。
        """
        cache_dir = self._tts_cache_dir()
        if not cache_dir.is_dir():
            return {'success': True, 'removed': 0, 'bytes': 0}
        removed = 0
        total = 0
        for entry in os.scandir(cache_dir):
            if not entry.is_file():
                continue
            try:
                size = entry.stat().st_size
                os.remove(entry.path)
                removed += 1
                total += size
            except OSError:
                continue
        log.info(f'[{self.name}] 已清除朗读缓存 {removed} 个文件 / {total / 1024 / 1024:.1f} MB')
        return {'success': True, 'removed': removed, 'bytes': total}

    def _tts_params(self) -> Dict[str, Any]:
        """把设置项整理成引擎参数。语音名交给引擎自己解释（各家音色命名不同）。"""
        order = [item.strip() for item in str(self.setting('tts_order') or '').split(',')]
        mode = str(self.setting('tts_engine') or 'auto')
        voice = str(self.setting('tts_voice') or '').strip()
        if mode == 'openai' and voice in ('', _tts.DEFAULT_VOICE):
            # 默认音色是 edge 的名字，直接丢给第三方端点会被拒；让它用自己的默认
            voice = 'alloy'
        return {
            'mode': mode,
            'order': [item for item in order if item] or list(_tts.ENGINES),
            'voice': voice or _tts.DEFAULT_VOICE,
            'rate': int(self.setting('tts_rate') or 0),
            'base_url': str(self.setting('tts_base_url') or ''),
            # 凭据要读未脱敏的设置：get_settings() 返回的是掩码
            'api_key': self._raw_settings().get('tts_api_key') or '',
            'model': str(self.setting('tts_model') or ''),
        }

    def tts_status(self) -> Dict[str, Any]:
        """引擎可用性一览 + 当前配置，供阅读器的朗读面板显示。"""
        params = self._tts_params()
        data = _tts.status(params)
        data['mode'] = params['mode']
        data['voice'] = params['voice']
        data['rate'] = params['rate']
        return data

    def tts_voices(self) -> Dict[str, Any]:
        """edge 端点上**真实可用**的中文音色列表。

        `source` 告诉前端这份列表的来路：`edge` 表示问过端点（权威），`fallback`
        表示端点不可达（前端用内置的常用音色兜底）。写死列表的问题是微软会下线音色，
        用户点一下只会得到"找不到音色"。
        """
        voices = _tts.edge_voices()
        return {'voices': voices, 'source': 'edge' if voices else 'fallback'}

    # ===== 切句与合成 =====

    def tts_segment(self, text: str, max_chars: int = 240) -> Dict[str, Any]:
        """把正文切成"一句一片"：前端按片请求音频，边合成边播，不用等整章。"""
        segments = _tts.split_text(str(text or ''), max(40, min(int(max_chars or 240), 1000)))
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
        if len(text) > _tts.MAX_CHARS:
            return {'error': f'这一段太长（{len(text)} 字），请缩短后再朗读'}

        params = self._tts_params()
        if engine and engine in _tts.ENGINES:
            params['mode'] = engine
        cache_dir = self._tts_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)

        prefix = self._tts_cache_prefix(text, cache_key, params, cache_dir)
        cached = self._tts_hit(cache_dir, prefix)
        if cached:
            path, meta = cached
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = time.time()
            return {'url': self._image_url(path), 'mtime': mtime, 'cached': True,
                    'engine': meta.get('engine', ''),
                    'marks': self._tts_meta_marks(cache_dir, prefix),
                    'segmentIndex': segment_index}

        try:
            result = _tts.synthesize(text, params)
        except Exception as e:
            log.warning(f'[{self.name}] 朗读失败: {e}')
            return {'error': str(e)}
        if len(result['audio']) > TTS_RESULT_LIMIT:
            return {'error': '合成结果异常大，已放弃播放'}

        # 落盘路径再过一次包含判定：前缀已做过字符白名单，这里是纵深防御 ——
        # 音频与 .json 元数据都必须落在缓存目录内（审计项 P1-3）。
        path = self._tts_cache_path(cache_dir, prefix + result['ext'])
        meta_path = self._tts_cache_path(cache_dir, prefix + '.json')
        if path is None or meta_path is None:
            log.warning(f'[{self.name}] 朗读缓存路径越界，已放弃写入: {prefix!r}')
            return {'error': '缓存路径越界，已放弃写入'}
        try:
            with open(path, 'wb') as handle:
                handle.write(result['audio'])
            with open(meta_path, 'w', encoding='utf-8') as handle:
                json.dump({'engine': result['engine'], 'marks': result['marks']},
                          handle, ensure_ascii=False)
        except OSError as e:
            log.warning(f'[{self.name}] 朗读音频落盘失败: {e}')
            return {'error': f'音频写入失败: {e}'}

        self._tts_prune(cache_dir)
        return {'url': self._image_url(path), 'mtime': os.path.getmtime(path), 'cached': False,
                'engine': result['engine'], 'marks': result['marks'],
                'segmentIndex': segment_index}

    # ===== 缓存 =====

    def _tts_cache_prefix(self, text: str, cache_key: str,
                          params: Dict[str, Any], cache_dir: str) -> str:
        """缓存文件名前缀：内容哈希 + 参数指纹。

        指纹里带上引擎/音色/语速/端点 —— 换了任何一个都该重新合成，否则用户切换后
        听到的还是上一种声音（缓存"命中"得太成功）。

        `cache_key` 由前端给，但**请求体里的东西一律不可信**：它只接受十六进制
        指纹（`_CACHE_KEY_RE`），形状不符就回落到按文本现算。历史缺口：它被当成
        文件名前缀直接拼进 `os.path.join`，于是 `cache_key='../../../../tmp/x'`
        就能把音频与元数据写到缓存目录之外（审计项 P1-3）。
        """
        raw_key = str(cache_key or '')
        if not _CACHE_KEY_RE.match(raw_key):
            raw_key = hashlib.md5(text.encode('utf-8')).hexdigest()
        key = raw_key[:32]
        stamp = hashlib.md5(
            f"{params['mode']}|{params['voice']}|{params['rate']}|{params['base_url']}"
            .encode('utf-8')).hexdigest()[:8]
        return f'{key}-{stamp}'

    @staticmethod
    def _tts_cache_path(cache_dir: str, name: str) -> str | None:
        """拼出缓存文件路径；结果不在 `cache_dir` 之内时返回 None。

        纵深防御：前缀已经做过字符白名单，这一层保证"即使以后有人放宽了白名单，
        落盘位置也不会跑出缓存目录"。同一判定也用在 .json 元数据上。
        """
        try:
            base = Path(cache_dir).resolve()
            target = Path(os.path.join(cache_dir, name)).resolve()
        except (OSError, ValueError):
            return None
        return str(target) if target.is_relative_to(base) else None

    def _tts_hit(self, cache_dir: str, prefix: str):
        """缓存命中：返回 (音频路径, 元信息)。

        只认音频扩展名 —— 同一把钥匙旁边还躺着 `.json` 元数据文件，用 startswith
        直接取第一个会把元数据当成音频返回（前端拿到噪音）。

        **空文件不算命中**：写盘写了一半就被中断（进程被杀、磁盘满）会留下 0 字节的
        mp3，它会被当成"已缓存"永久命中，表现是这段文字再也读不出声。删掉重新合成。
        """
        try:
            names = os.listdir(cache_dir)
        except OSError:
            return None
        for name in names:
            if not (name.startswith(prefix + '.') and name.lower().endswith(_AUDIO_EXTS)):
                continue
            path = os.path.join(cache_dir, name)
            try:
                if os.path.getsize(path) <= 0:
                    raise OSError('缓存音频为空')
            except OSError as e:
                log.warning(f'缓存音频不可用（{e}），删除后重新合成: {name}')
                for target in (path, os.path.join(cache_dir, prefix + '.json')):
                    try:
                        if os.path.exists(target):
                            os.remove(target)
                    except OSError:
                        pass
                return None
            meta = self._tts_read_json(os.path.join(cache_dir, prefix + '.json'))
            return path, meta
        return None

    def _tts_meta_marks(self, cache_dir: str, prefix: str) -> list:
        meta = self._tts_read_json(os.path.join(cache_dir, prefix + '.json'))
        return meta.get('marks') or []

    @staticmethod
    def _tts_read_json(path: str) -> dict:
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _tts_prune(self, cache_dir: str, keep_bytes: int = TTS_CACHE_LIMIT) -> None:
        """缓存目录超过上限时，从最旧的开始删（音频 + 时间戳成对删）。

        `ponytail:` 上限是"整目录字节数"，不是"按书/按章"，也没有专门的索引文件 ——
        一次 os.scandir 排序足够；真到了几十万文件再看是否需要数据库。
        """
        entries = []
        total = 0
        try:
            for entry in os.scandir(cache_dir):
                if not entry.is_file():
                    continue
                stat = entry.stat()
                total += stat.st_size
                entries.append((stat.st_mtime, stat.st_size, entry.path))
        except OSError:
            return
        if total <= keep_bytes:
            return
        entries.sort()
        for _mtime, size, path in entries:
            # 一对文件（音频 + 时间戳）都要删，但**只按音频**扣减总量 ——
            # 时间戳的字节本来就没计入 total，两边都减会让统计偏低、多删一批。
            stem, ext = os.path.splitext(path)
            for target in (path, stem + '.json'):
                try:
                    if os.path.exists(target):
                        os.remove(target)
                except OSError:
                    pass
            if ext.lower() in _AUDIO_EXTS:
                total -= size
            if total <= keep_bytes:
                break
        log.info(f'朗读缓存超过上限，已清理到 {total / 1024 / 1024:.0f} MB')

