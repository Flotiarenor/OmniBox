"""OpenAI 兼容端点这一档：合成、地址校验与音色清单。

"OpenAI 兼容"指的是 `POST /v1/audio/speech`，不是某个具体实现：Kokoro / GPT-SoVITS /
云 API 都行。端点是用户自己的服务，本模块只当客户端；没有字级时间戳（逐字跟随在这一档
退化成整句高亮）。音色清单接口 `/v1/voices` 是部分服务的扩展，问不到是常态。

本模块不 import 兄弟模块：edge 的音色清单由入口通过 `VoiceCatalog` 的构造参数注入。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# 取音色的网络超时：设置页一打开就要它，端点挂着不能转圈十秒。
VOICE_LIST_TIMEOUT = 5.0
# 成功结果的缓存寿命：自动模式下每段音频都要问一次"这个音色归谁"，
# 不缓存就是每个句子都去问一遍 edge 端点与朗读端点。
VOICE_CACHE_TTL = 300.0


def available(params: Dict[str, Any]) -> bool:
    """配了端点地址就能用（连不上的具体错误在合成时给出）。"""
    return bool(str(params.get('base_url') or '').strip())


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


def synthesize(text: str, params: Dict[str, Any]) -> Tuple[bytes, str, List[dict]]:
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


class VoiceCatalog:
    """音色清单与按引擎的归属判定。

    缓存只记**成功**结果：失败（返回 None/空）不写缓存 —— 端点没起来时缓存一次失败，
    用户修好之后还要再等一个 TTL 才看得到音色。
    """

    def __init__(self, edge_voices: Callable[[], List[Dict[str, Any]]]) -> None:
        self._edge_voices = edge_voices
        self._cache: Dict[tuple, Tuple[float, Any]] = {}

    def clear_cache(self) -> None:
        """丢掉缓存的音色清单（设置改了就重新问；测试用它隔离用例）。"""
        self._cache.clear()

    def _cached(self, key: tuple, produce: Callable[[], Any]) -> Any:
        import time

        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < VOICE_CACHE_TTL:
            return hit[1]
        value = produce()
        if value:
            self._cache[key] = (now, value)
        return value

    def endpoint_voices(self, base_url: str, api_key: str = '',
                        model: str = '') -> Optional[List[str]]:
        """OpenAI 兼容端点上的音色名清单；问不到时返回 None（不是空列表）。

        None 与 `[]` 的区分是必要的：None 表示"这个服务不提供音色清单"，
        调用方应当照旧把音色名发给它；空列表则表示"端点在，但一个音色都没有"。
        """
        base = str(base_url or '').strip().rstrip('/')
        if not base:
            return None
        base = _validated_endpoint_base(base)
        key = ('endpoint', base, str(model or '').strip())
        return self._cached(key, lambda: _endpoint_voice_names(
            base, str(api_key or '').strip(), str(model or '').strip(), VOICE_LIST_TIMEOUT))

    def edge_voices(self) -> List[Dict[str, Any]]:
        """edge 端点上真实可用的中文音色（失败时是空列表，见 `tts_edge.voices`）。"""
        return self._edge_voices()

    def engine_voice_names(self, engine: str,
                           params: Dict[str, Any]) -> Optional[List[str]]:
        """某个引擎当前可用的音色名；None = 问不到（别据此判定音色不存在）。

        `system` 返回 None：SAPI 的音色名是"本机装了哪些"（Huihui/Yaoyao/Kangkang…），
        与设置里那个音色名不同域，无法做归属判定，只能照旧尝试。
        """
        if engine == 'edge':
            return self._cached(('edge',), lambda: [item['name'] for item in self.edge_voices()] or None)
        if engine == 'openai':
            return self.endpoint_voices(str(params.get('base_url') or ''),
                                        str(params.get('api_key') or ''),
                                        str(params.get('model') or ''))
        return None
