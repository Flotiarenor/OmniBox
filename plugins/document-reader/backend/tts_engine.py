"""朗读引擎：同一段文本、三种来源、一个接口。

放在 document-reader 的 backend/ 下（而不是独立插件）是因为朗读已经并进阅读器：
宿主自己的状态机才知道"当前读到哪一段、翻页后该续哪里"，跨进程/跨 iframe 拿不到。

三档引擎，按音质降序：

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
* 时间戳一律换算成**毫秒**再往外给（edge 的 100ns 单位留在这一层）。
* `mode='auto'` 时的顺序就是 ENGINES 的顺序，可被 `order` 覆盖；
  定了具体引擎还失败就**不**悄悄换音色（用户选了 A 却听到 B 是更糟的体验）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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


# ===== edge-tts =====


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
    async for chunk in communicate.stream():
        if chunk['type'] == 'audio':
            if anchor is None:
                anchor = communicate.state['chunk_audio_bytes']
            audio += chunk['data']
        elif chunk['type'] == 'WordBoundary':
            if anchor is None:
                anchor = communicate.state['chunk_audio_bytes']
            offset = int(chunk['offset'])
            marks.append({
                'text': chunk.get('text') or '',
                'chars': len(chunk.get('text') or ''),
                # 相对音频起点的时间轴：offset 减去首帧音频到位时的字节数
                'startMs': round((offset - anchor) / 10000.0, 1),
                'durationMs': round(int(chunk.get('duration') or 0) / 10000.0, 1),
            })
    marks.sort(key=lambda m: m['startMs'])
    return bytes(audio), marks


def edge_synthesize(text: str, params: Dict[str, Any]) -> Tuple[bytes, str, List[dict]]:
    import edge_tts  # noqa: F401 - 明确报错来源：没装就是没装

    voice = str(params.get('voice') or DEFAULT_VOICE)
    rate = f"{int(params.get('rate') or 0):+d}%"
    audio, marks = asyncio.run(_edge_stream(text, voice, rate))
    if not audio:
        raise RuntimeError('edge-tts 没有返回音频')
    return audio, '.mp3', marks


def _validated_endpoint_base(base: str) -> str:
    """校验 OpenAI 兼容端点的根地址，返回去掉尾部 `/` 的形式；不合法时抛异常。

    这个地址会收到 `Authorization: Bearer <tts_api_key>`，因此**不能接受任意字符串**：
      * 只放行 http/https —— `file://`、`ftp://` 一类会让 requests 走别的适配器，
        或在报错信息里回显本地内容；
      * 必须带主机名 —— `http:///v1/audio/speech` 之类是拼错的地址，早失败早报错。
    保留环回与私网地址：本地 TTS 端点（`http://127.0.0.1:8880`）正是设置项文档推荐的
    用法，用"禁私网"来防 SSRF 会把正常功能一起砍掉。改这个地址的权限另由设置项的
    `"admin_only": True` 限定（见 PluginBase.save_settings）。
    """
    from urllib.parse import urlsplit

    parts = urlsplit(base)
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        raise RuntimeError(f'朗读端点地址必须以 http:// 或 https:// 开头并带主机名: {base!r}')
    return base


# ===== OpenAI 兼容端点 =====


def openai_synthesize(text: str, params: Dict[str, Any]) -> Tuple[bytes, str, List[dict]]:
    import requests

    base = str(params.get('base_url') or '').strip().rstrip('/')
    if not base:
        raise RuntimeError('未配置 OpenAI 兼容端点地址')
    base = _validated_endpoint_base(base)
    payload = {
        'input': text,
        'voice': str(params.get('voice') or 'alloy'),
        'response_format': str(params.get('format') or 'mp3'),
        'speed': float(params.get('speed') or 1.0),
    }
    model = str(params.get('model') or '').strip()
    if model:
        payload['model'] = model
    headers = {'Content-Type': 'application/json'}
    api_key = str(params.get('api_key') or '').strip()
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'

    response = requests.post(f'{base}/v1/audio/speech', json=payload, headers=headers,
                             timeout=(5, 120))
    if response.status_code >= 400:
        detail = response.text[:200]
        raise RuntimeError(f'端点返回 {response.status_code}: {detail}')
    content_type = (response.headers.get('Content-Type') or '').lower()
    if 'wav' in content_type:
        ext = '.wav'
    elif 'ogg' in content_type or 'opus' in content_type:
        ext = '.ogg'
    elif 'mpeg' in content_type or 'mp3' in content_type:
        ext = '.mp3'
    else:
        # 端点不报 Content-Type 时按请求的格式猜；猜错只是文件名后缀不对，播放不受影响
        ext = '.wav' if payload['response_format'] == 'wav' else '.mp3'
    if not response.content:
        raise RuntimeError('端点返回了空音频')
    # 第三方端点不给时间戳，逐字跟随这一档就只能退化成"整句高亮"
    return response.content, ext, []


# ===== 音色清单（面板要显示"这个引擎到底有哪些音色"）=====

# 取音色的网络超时：设置页一打开就要它，端点挂着不能转圈十秒。
VOICE_LIST_TIMEOUT = 5.0
# 成功结果的缓存寿命：自动模式下每段音频都要问一次"这个音色归谁"，
# 不缓存就是每个句子都去问一遍 edge 端点与朗读端点。
VOICE_CACHE_TTL = 300.0
_VOICE_CACHE: Dict[tuple, Tuple[float, Any]] = {}

def _cached(key: tuple, produce: Callable[[], Any]) -> Any:
    """按 key 缓存 `produce()` 的**成功**结果 `VOICE_CACHE_TTL` 秒。

    失败（返回 None/空）不写缓存：端点没起来时缓存一次失败，用户修好之后还要再等
    一个 TTL 才看得到音色。代价是失败时会反复重试，但每次都由用户动作触发。
    """
    now = time.time()
    hit = _VOICE_CACHE.get(key)
    if hit and now - hit[0] < VOICE_CACHE_TTL:
        return hit[1]
    value = produce()
    if value:
        _VOICE_CACHE[key] = (now, value)
    return value


def clear_voice_cache() -> None:
    """丢掉缓存的音色清单（设置改了就重新问；测试用它隔离用例）。"""
    _VOICE_CACHE.clear()


def _parse_voice_names(data: Any) -> Optional[List[str]]:
    """从端点回应里取出音色名；形状不认识时返回 None。

    认两种形状：`{"voices": ["vivian", …]}`（Qwen3-TTS / Kokoro 一类）与
    `{"data": [{"id": …}, …]}`（OpenAI 列表风格）。字符串项与 `{"id"/"name": …}`
    两种写法都收，其余的忽略。
    """
    if not isinstance(data, dict):
        return None
    items: List[Any] = []
    for key in ('voices', 'data'):
        if isinstance(data.get(key), list):
            items = data[key]
            break
    names: List[str] = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get('id') or item.get('name')
            if isinstance(name, str) and name:
                names.append(name)
    return list(dict.fromkeys(names)) or None


def _endpoint_voice_names(base: str, api_key: str, model: str,
                          timeout: float) -> Optional[List[str]]:
    """问一次 OpenAI 兼容端点的 `/v1/voices`。

    返回 None 表示"问不到"（404/网络错误/形状不认识）。这个路由是部分服务的扩展，
    不是 OpenAI 规范的一部分，普通 TTS 服务没有它很正常，调用方据此决定跳过还是照旧尝试。

    先带 `?model=` 问（把音色钉在当前模型上）；模型名写错时这类服务可能直接 5xx
    （Qwen3-TTS 就是 `KeyError` 未接住 → 500），此时摘掉模型名再问一次：
    列不出精确清单，也好过一个音色都看不到。
    """
    import requests

    headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
    url = f'{base}/v1/voices'

    def ask(query: Dict[str, str]) -> Optional[List[str]]:
        try:
            response = requests.get(url, headers=headers, params=query, timeout=timeout)
        except Exception as e:  # 连不上就是问不到，不该让调用方炸
            log.warning(f'[document-reader] 取朗读端点音色失败（{url}）: {e}')
            return None
        if response.status_code in (401, 403):
            log.warning(f'[document-reader] 朗读端点拒绝列出音色（{response.status_code}），检查 API Key')
            return None
        if response.status_code >= 400:
            return None
        try:
            return _parse_voice_names(response.json())
        except ValueError:
            return None

    names = ask({'model': model} if model else {})
    if names is None and model:
        names = ask({})
    return names


def endpoint_voices(base_url: str, api_key: str = '', model: str = '') -> Optional[List[str]]:
    """OpenAI 兼容端点上的音色名清单；问不到时返回 None（不是空列表）。

    None 与 `[]` 的区分是必要的：None 表示"这个服务不提供音色清单"，
    调用方应当照旧把音色名发给它；空列表则表示"端点在，但一个音色都没有"。
    """
    base = str(base_url or '').strip().rstrip('/')
    if not base:
        return None
    base = _validated_endpoint_base(base)
    key = ('endpoint', base, str(model or '').strip())
    return _cached(key, lambda: _endpoint_voice_names(
        base, str(api_key or '').strip(), str(model or '').strip(), VOICE_LIST_TIMEOUT))


def engine_voice_names(engine: str, params: Dict[str, Any]) -> Optional[List[str]]:
    """某个引擎当前可用的音色名；None = 问不到（别据此判定音色不存在）。

    `system` 返回 None：SAPI 的音色名是"本机装了哪些"（Huihui/Yaoyao/Kangkang…），
    与设置里那个音色名不同域，无法做归属判定，只能照旧尝试。
    """
    if engine == 'edge':
        return _cached(('edge',), lambda: [item['name'] for item in edge_voices()] or None)
    if engine == 'openai':
        return endpoint_voices(str(params.get('base_url') or ''),
                               str(params.get('api_key') or ''),
                               str(params.get('model') or ''))
    return None


# ===== Windows 自带语音（SAPI5，完全离线）=====

# 用 -EncodedCommand 传 PowerShell 代码：脚本以 UTF-16LE 内联，无论控制台代码页是
# 多少，中文文本都不会在命令行参数上被编码破坏（这是子进程方案最容易踩的坑）。
_SAPI_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Speech
  $text = [IO.File]::ReadAllText($env:OBX_TTS_TEXT, [Text.Encoding]::UTF8)
  $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
  try {
    $want = $env:OBX_TTS_VOICE
    if ($want) {
      $installed = @($synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name })
      $hit = $installed | Where-Object { $_ -eq $want } | Select-Object -First 1
      if (-not $hit) { $hit = $installed | Where-Object { $_ -like "*$want*" } | Select-Object -First 1 }
      if (-not $hit) { $hit = $installed | Where-Object { $_ -like '*Huihui*' } | Select-Object -First 1 }
      if (-not $hit) { $hit = $installed | Select-Object -First 1 }
      if ($hit) { $synth.SelectVoice($hit) }
    }
    $synth.Rate = [int]$env:OBX_TTS_RATE
    $synth.SetOutputToWaveFile($env:OBX_TTS_OUT)
    $synth.Speak($text)
  } finally {
    $synth.Dispose()
  }
  [Console]::Out.Write('OK')
} catch {
  [Console]::Error.Write('ERR ' + $_.Exception.Message)
  exit 1
}
'''.strip()


def _decode_stderr(raw: bytes) -> str:
    """从 PowerShell 的 stderr 里取出人能看的一句话。

    重定向 stderr 时 PowerShell 会把输出包成 CLIXML（`#< CLIXML` + XML），里面既有
    我们写的 `ERR …`，也有进度条之类的噪音 —— 直接取前 200 字给用户看，就是一段
    XML 乱码。这里剥掉包装，只留 Error 记录（`<S S="Error">`）或原文第一行非空行。
    """
    text = raw.decode('utf-8', 'replace').strip()
    if text.startswith('#< CLIXML'):
        import re
        messages = re.findall(r'<S S="Error">(.*?)</S>', text, re.DOTALL)
        text = ' '.join(re.sub(r'<[^>]+>', '', item) for item in messages).strip() or text
    # CLIXML 里换行是转义的 _x000D__x000A_，还原成空格更好读
    text = text.replace('_x000D_', ' ').replace('_x000A_', ' ')
    return text[:300] or '（没有任何输出）'



def system_available() -> bool:
    if sys.platform != 'win32':
        return False
    # powershell.exe 在 Win7+ 恒在；找不到它比"非 Windows"更罕见，但仍当不可用处理
    return bool(shutil.which('powershell.exe') or shutil.which('powershell'))


def system_synthesize(text: str, params: Dict[str, Any]) -> Tuple[bytes, str, List[dict]]:
    if not system_available():
        raise RuntimeError('当前平台没有可用的系统语音合成')
    powershell = shutil.which('powershell.exe') or shutil.which('powershell')
    workdir = Path(tempfile.mkdtemp(prefix='omnibox-tts-'))
    text_file = workdir / 'text.txt'
    out_file = workdir / 'out.wav'
    try:
        # utf-8 写入：PowerShell 侧用 ReadAllText(UTF8) 读，中文不会经过命令行参数
        text_file.write_text(text, encoding='utf-8')
        env = dict(os.environ)
        env.update({
            'OBX_TTS_TEXT': str(text_file),
            'OBX_TTS_OUT': str(out_file),
            'OBX_TTS_VOICE': str(params.get('voice') or ''),
            # SAPI 的 Rate 是 -10..10 的档位，而 UI 给的是百分比：10% 语速 ≈ 1 档
            'OBX_TTS_RATE': str(max(-10, min(10, round(int(params.get('rate') or 0) / 10.0)))),
        })
        encoded = _encode_powershell(_SAPI_SCRIPT)
        result = subprocess.run(
            [powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-EncodedCommand', encoded],
            capture_output=True, timeout=180, env=env, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if result.returncode != 0:
            raise RuntimeError(f'系统语音合成失败: {_decode_stderr(result.stderr)}')
        if not out_file.is_file() or out_file.stat().st_size == 0:
            raise RuntimeError('系统语音没有产出音频')
        return out_file.read_bytes(), '.wav', []
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _encode_powershell(script: str) -> str:
    import base64
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


# ===== 统一入口 =====

_RUNNERS: Dict[str, Callable[[str, Dict[str, Any]], Tuple[bytes, str, List[dict]]]] = {
    'edge': edge_synthesize,
    'openai': openai_synthesize,
    'system': system_synthesize,
}


def engine_available(engine: str, params: Dict[str, Any]) -> bool:
    """这个引擎现在能不能用（不实际合成，只判断前置条件）。"""
    if engine == 'edge':
        import importlib.util
        return importlib.util.find_spec('edge_tts') is not None
    if engine == 'openai':
        return bool(str(params.get('base_url') or '').strip())
    if engine == 'system':
        return system_available()
    return False


# 同一引擎的尝试次数：edge 是网络调用，偶发失败（握手超时、连接被重置）很常见。
# 一次就降级等于把偶发当永久 —— 用户会听到系统音色或旧缓存，却再也不会回到首选引擎。
ENGINE_ATTEMPTS = 3
# 重试间隔：网络抖动很快过去，等太久会让"点朗读后半天不响"。
RETRY_DELAY_SECONDS = 0.4


def available_engines(params: Dict[str, Any]) -> List[str]:
    return [name for name in ENGINES if engine_available(name, params)]


def _attempt_engine(engine: str, text: str, params: Dict[str, Any], errors: List[str]):
    """同一引擎重试 `ENGINE_ATTEMPTS` 次；失败返回 (None, 是否还有别的可试)。

    只有"还有重试次数"时才算可重试 —— 参数类错误（如端点返回 400）重试也是白等，
    但网络类错误（超时、连接重置）重试往往就好了，而调用方无法可靠区分两者，
    所以统一重试：代价是最多多等 0.8 秒，收益是不必降到系统音色。
    """
    runner = _RUNNERS[engine]
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


def engine_has_voice(engine: str, voice: str, params: Dict[str, Any]) -> bool:
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
    names = engine_voice_names(engine, params)
    if names is None:
        return True
    return want in names


def synthesize(text: str, params: Dict[str, Any]) -> Dict[str, Any]:
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
    if mode in _RUNNERS:
        order = [mode]
    else:
        order = [name for name in (params.get('order') or ENGINES) if name in _RUNNERS]
        voice = str(params.get('voice') or '').strip()
        if voice:
            owned = [name for name in order if engine_has_voice(name, voice, params)]
            if owned and len(owned) < len(order):
                skipped = [name for name in order if name not in owned]
                log.info(f'[document-reader] 音色 {voice} 属于 {owned}，自动模式跳过 {skipped}')
                order = owned

    errors: List[str] = []
    for engine in order:
        if not engine_available(engine, params):
            errors.append(f'{engine}: 不可用（未安装/未配置/平台不支持）')
            continue
        result, _ = _attempt_engine(engine, text, params, errors)
        if result is not None:
            audio, ext, marks = result
            return {'audio': audio, 'ext': ext, 'marks': marks, 'engine': engine}

    raise RuntimeError('所有朗读引擎都不可用 —— ' + '；'.join(errors))


def status(params: Dict[str, Any]) -> Dict[str, Any]:
    """给前端/设置面板看的一览：每个引擎可用性与说明。"""
    return {
        'engines': [
            {
                'name': name,
                'label': ENGINE_LABELS[name],
                'available': engine_available(name, params),
                'marks': name == 'edge',  # 是否支持字级时间戳（逐字跟随）
            }
            for name in ENGINES
        ],
        'order': list(params.get('order') or ENGINES),
    }


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


def edge_voices() -> List[Dict[str, Any]]:
    """列出 edge 端点上**真实可用**的中文音色。

    为什么必须问端点而不是写死一份表：微软会调整音色池（实测有些音色已经下线，
    写死的列表点一下就报"找不到音色"）。这里失败时返回空列表 —— 前端退回内置的
    常用几个，不会因为断网就没得选。
    """
    try:
        import asyncio

        import edge_tts
    except ImportError:
        return []

    async def fetch() -> List[Dict[str, Any]]:
        voices = await edge_tts.list_voices()
        result = []
        for voice in voices:
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
