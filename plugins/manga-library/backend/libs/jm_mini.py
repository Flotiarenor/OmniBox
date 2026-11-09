#!/usr/bin/env python
"""禁漫精简下载客户端 (jm-mini)
================================

移植自 jmcomic 2.6.19，只覆盖下载路径：
  1. 域名单引导（newsvr 密文列表）与 `/setting` 版本协商
  2. 请求签名（token / tokenparam）与响应体 AES-ECB 解密
  3. 漫画详情 / 章节详情 / 章节模板（scramble_id）
  4. 图片 URL 拼接与字节下载
  5. 乱序图片的分割数计算与纵向条带重排
许可: MIT (Copyright (c) 2023 hect0x7)，出处见仓库根 THIRD_PARTY_NOTICES.md。

依赖: requests、pycryptodome（Crypto）、Pillow。用法:
    from jm_mini import JmClient, save_image
    client = JmClient()
    album = client.album('1043134')
    chapter = client.chapter(album.chapters[0].photo_id)
    save_image(client.image_bytes(chapter.photo_id, chapter.images[0]), path,
               scramble_id=chapter.scramble_id, photo_id=chapter.photo_id,
               source_name=chapter.images[0])
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import random
import re
import threading
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

API_VERSION = '2.0.21'
#: 域名单服务器：响应是 AES 密文，解出来是当前可用的接口域名列表。
DOMAIN_SERVERS = (
    'https://rup4a04-c01.tos-ap-southeast-1.bytepluses.com/newsvr-2025.txt',
    'https://rup4a04-c02.tos-cn-hongkong.bytepluses.com/newsvr-2025.txt',
)
#: 域名单取不到时的兜底（与站点内置列表同源，仅作最后一道保险）。
FALLBACK_API_DOMAINS = ('www.cdnhjk.net', 'www.cdngwc.cc', 'www.cdngwc.net',
                         'www.cdngwc.club', 'www.cdnhjk.cc')
#: 图片 CDN 域名（同一张图在各域名内容一致，随机挑一个）。
IMAGE_DOMAINS = (
    'cdn-msp.jmapiproxy1.cc', 'cdn-msp.jmapiproxy2.cc', 'cdn-msp2.jmapiproxy2.cc',
    'cdn-msp3.jmapiproxy2.cc', 'cdn-msp.jmapinodeudzn.net', 'cdn-msp3.jmapinodeudzn.net',
)

#: 四个协议密钥：前两个用于请求头签名（章节模板接口必须用第二个）。
TOKEN_SECRET = '18comicAPP'
TOKEN_SECRET_CONTENT = '18comicAPPContent'
DATA_SECRET = '185Hcomic3PAPP7R'
DOMAIN_SECRET = 'diosfjckwpqpdfjkvnqQjsik'

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Linux; Android 9; V1938CT Build/PQ3A.190705.11211812; wv) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 Safari/537.36'
)
IMAGE_HEADERS = {
    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    'X-Requested-With': 'com.JMComic3.app',
    'Accept-Language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
}
#: 章节模板页把 scramble_id 直接写在脚本里，只能按文本抠。
SCRAMBLE_PATTERN = re.compile(r'var scramble_id = (\d+);')
#: 域名单里也可能带版本号（形如 2.1.12），用来更新签名版本。
VERSION_PATTERN = re.compile(r'^\d+\.\d+\.\d+$')


class JmError(RuntimeError):
    """接口/网络层失败（域名全挂、响应不是预期 JSON、解密失败）。"""


@dataclass(frozen=True)
class ChapterRef:
    """漫画里的一章（列表项）：只有 id、标题与序号，详情另行按需拉取。"""

    photo_id: str
    name: str
    sort: int


@dataclass
class Album:
    """漫画详情。`chapters` 已按序号去重（站点同一序号会返回多条记录）。"""

    album_id: str
    name: str
    author: str
    actors: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    series_id: int = 0
    chapters: List[ChapterRef] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.chapters)


@dataclass
class Chapter:
    """章节详情：图片名列表与解密参数。"""

    photo_id: str
    name: str
    sort: int
    series_id: int
    images: List[str] = field(default_factory=list)
    scramble_id: int = 0

    def __len__(self) -> int:
        return len(self.images)

    @property
    def is_single_album(self) -> bool:
        """单章本子：站点把 `series_id` 写成 0。"""
        return self.series_id == 0

    @property
    def album_index(self) -> int:
        """章节在本子里的序号（从 1 起）。单章本子站点给的 `sort` 是 2，归一成 1。"""
        if self.is_single_album and self.sort == 2:
            return 1
        return self.sort

    @property
    def indextitle(self) -> str:
        """多章本子的目录名。与既有漫画库的落盘目录逐字一致，不要改措辞。"""
        return f'第{self.album_index}話 {self.name}'


def _md5hex(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def _decrypt(payload: str, ts: str, secret: str) -> str:
    """AES-ECB 解密接口响应：base64 → 解密 → 去掉 PKCS 补位。"""
    from Crypto.Cipher import AES  # 延迟导入：与 group-mesh 共用同一份 pycryptodome

    key = _md5hex(f'{ts}{secret}').encode('utf-8')
    raw = AES.new(key, AES.MODE_ECB).decrypt(base64.b64decode(payload))
    return raw[: -raw[-1]].decode('utf-8')


class JmClient:
    """一个线程安全的接口客户端；构造一次，下载任务共用。"""

    def __init__(self,
                 *,
                 timeout: float = 20.0,
                 retries: int = 2,
                 user_agent: str = DEFAULT_USER_AGENT,
                 proxies: Optional[Dict[str, str]] = None) -> None:
        self._timeout = timeout
        self._retries = max(1, retries)
        self._user_agent = user_agent
        self._proxies = proxies
        self._local = threading.local()
        self._lock = threading.Lock()
        self._api_domains: Tuple[str, ...] = ()
        self._version = API_VERSION
        self._ready = False
        self._scramble: Dict[str, int] = {}

    # ------------------------------------------------------------------ HTTP

    def _session(self) -> requests.Session:
        """每线程一个 Session：连接池按线程复用，避免并发下共享 Session 的竞态。"""
        session = getattr(self._local, 'session', None)
        if session is None:
            session = requests.Session()
            session.headers.update({'user-agent': self._user_agent, 'Accept-Encoding': 'gzip, deflate'})
            self._local.session = session
        return session

    def _token_headers(self, ts: int, version: str, secret: str) -> Dict[str, str]:
        return {
            'user-agent': self._user_agent,
            'Accept-Encoding': 'gzip, deflate',
            'token': _md5hex(f'{ts}{secret}'),
            'tokenparam': f'{ts},{version}',
        }

    def _bootstrap_domains(self) -> Tuple[str, ...]:
        """取域名单：两个服务器依次试，都失败就用内置兜底列表。"""
        for server in DOMAIN_SERVERS:
            try:
                resp = self._session().get(server, timeout=self._timeout,
                                           proxies=self._proxies)
                # 响应开头可能带 BOM 或其它非 ASCII 前缀，base64 解不了（站点行为）
                text = resp.text
                while text and not text[0].isascii():
                    text = text[1:]
                payload = json.loads(_decrypt(text.strip(), '', DOMAIN_SECRET))
                domains = payload.get('Server') or []
                if isinstance(domains, str):
                    domains = [line.strip() for line in domains.splitlines() if line.strip()]
                domains = [str(item).strip() for item in domains if str(item).strip()]
                if domains:
                    log.info(f'[jm] 域名单已更新：{len(domains)} 个')
                    return tuple(domains)
            except Exception as error:      # 域名单失败不致命，换下一个服务器
                log.warning(f'[jm] 域名单获取失败（{server}）：{type(error).__name__}: {error}')
        log.warning('[jm] 域名单不可用，改用内置域名列表')
        return FALLBACK_API_DOMAINS

    def _ensure_ready(self) -> None:
        """首次请求前解析域名与签名版本；并发进入时只做一次。"""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self._api_domains = self._bootstrap_domains()
            self._refresh_version()
            self._ready = True

    def _refresh_version(self) -> None:
        """`/setting` 会返回站点当前版本，签名版本落后会被拒。失败时保留内置值。"""
        try:
            body, _ts, _domain = self._raw_api('/setting', None, TOKEN_SECRET, self._version)
            version = str(body.get('jm3_version') or '').strip()
            if VERSION_PATTERN.match(version):
                self._version = version
                log.info(f'[jm] 使用站点版本号 {version}')
        except Exception as error:
            log.warning(f'[jm] 版本协商失败，沿用内置版本 {self._version}：{type(error).__name__}: {error}')

    def _raw_api(self, path: str, params: Optional[Dict[str, Any]], secret: str,
                 version: str) -> Tuple[dict, str, str]:
        """发一次接口请求并解密；域名轮换与重试都在这里。返回 (JSON, ts, domain)。"""
        errors: List[str] = []
        for domain in self._api_domains:
            for attempt in range(self._retries):
                ts = str(int(time.time()))
                url = f'https://{domain}{path}'
                try:
                    resp = self._session().get(
                        url,
                        params=params,
                        headers=self._token_headers(int(ts), version, secret),
                        timeout=self._timeout,
                        proxies=self._proxies,
                    )
                    if resp.status_code != 200:
                        raise JmError(f'HTTP {resp.status_code}')
                    body = json.loads(resp.text)
                    return json.loads(_decrypt(body['data'], ts, DATA_SECRET)), ts, domain
                except Exception as error:
                    errors.append(f'{domain}#{attempt + 1}: {type(error).__name__}: {error}')
                    if attempt + 1 < self._retries:
                        time.sleep(0.3 * (attempt + 1))
        raise JmError(f'接口请求全部失败 {path}: ' + ' | '.join(errors[-4:]))

    def _api_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        self._ensure_ready()
        body, _ts, _domain = self._raw_api(path, params, TOKEN_SECRET, self._version)
        return body

    # ------------------------------------------------------------------ 实体

    def album(self, album_id: Any) -> Album:
        """漫画详情。`series` 里同一序号会有多条记录，必须按序号去重。"""
        data = self._api_json('/album', {'id': str(album_id)})
        if data.get('name') is None:
            raise JmError(f'漫画 {album_id} 不存在或接口返回空数据')
        refs = self._distinct_chapters(data.get('series') or [])
        return Album(
            album_id=str(data.get('id') or album_id),
            name=str(data.get('name') or ''),
            author=self._author(data.get('author')),
            actors=[str(item) for item in (data.get('actors') or [])],
            tags=[str(item) for item in (data.get('tags') or [])],
            series_id=self._series_id(data.get('series_id')),
            chapters=refs,
        )

    def chapter(self, photo_id: Any) -> Chapter:
        """章节详情：图片名列表 + scramble_id。"""
        data = self._api_json('/chapter', {'id': str(photo_id)})
        if data.get('name') is None:
            raise JmError(f'章节 {photo_id} 不存在或接口返回空数据')
        series = data.get('series') or []
        sort = self._chapter_sort(series, data.get('id'))
        result = Chapter(
            photo_id=str(data.get('id') or photo_id),
            name=str(data.get('name') or ''),
            sort=sort,
            series_id=self._series_id(data.get('series_id')),
            images=[str(item) for item in (data.get('images') or [])],
        )
        result.scramble_id = self.scramble_id(result.photo_id)
        return result

    def scramble_id(self, photo_id: Any) -> int:
        """章节的乱序参数；模板页是 HTML，只能按正则抠，抠不到用首个阈值兜底。"""
        key = str(photo_id)
        with self._lock:
            cached = self._scramble.get(key)
        if cached is not None:
            return cached
        self._ensure_ready()
        text = ''
        for domain in self._api_domains:
            try:
                ts = str(int(time.time()))
                resp = self._session().get(
                    f'https://{domain}/chapter_view_template',
                    params={'id': key, 'mode': 'vertical', 'page': '0',
                            'app_img_shunt': '1', 'express': 'off', 'v': int(time.time())},
                    headers=self._token_headers(int(ts), self._version, TOKEN_SECRET_CONTENT),
                    timeout=self._timeout,
                    proxies=self._proxies,
                )
                text = resp.text
                break
            except Exception as error:
                log.warning(f'[jm] 取 scramble_id 失败（{domain}）：{type(error).__name__}: {error}')
        match = SCRAMBLE_PATTERN.search(text)
        value = int(match.group(1)) if match else 220980
        with self._lock:
            self._scramble[key] = value
        return value

    def cover_url(self, album_id: Any) -> str:
        return f'https://{IMAGE_DOMAINS[0]}/media/albums/{album_id}.jpg'

    def image_url(self, photo_id: Any, name: str) -> str:
        """章节内第 N 张图的 URL：`.../media/photos/{章节 id}/{序号:05}{后缀}`。"""
        suffix = name[name.rfind('.'):] if '.' in name else '.jpg'
        index = int(name[:5]) if name[:5].isdigit() else 1
        domain = random.choice(IMAGE_DOMAINS)
        return f'https://{domain}/media/photos/{photo_id}/{index:05}{suffix}'

    def image_bytes(self, photo_id: Any, name: str) -> bytes:
        """下载一张图的原始字节（不重排；重排在 `save_image`）。"""
        self._ensure_ready()
        errors: List[str] = []
        for attempt in range(max(2, self._retries)):
            url = self.image_url(photo_id, name)
            headers = {**IMAGE_HEADERS, 'user-agent': self._user_agent,
                       'Referer': f'https://{self._api_domains[0]}'}
            try:
                resp = self._session().get(url, headers=headers, timeout=self._timeout,
                                           proxies=self._proxies)
                if resp.status_code != 200 or not resp.content:
                    raise JmError(f'HTTP {resp.status_code}')
                return resp.content
            except Exception as error:
                errors.append(f'{url}: {type(error).__name__}: {error}')
                time.sleep(0.3 * (attempt + 1))
        raise JmError(f'图片下载失败（{photo_id}/{name}）: ' + ' | '.join(errors[-2:]))

    # ------------------------------------------------------------------ 解析

    @staticmethod
    def _author(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            return ' '.join(str(item).strip() for item in value if str(item).strip())
        return str(value or '')

    @staticmethod
    def _series_id(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _distinct_chapters(series: List[Any]) -> List[ChapterRef]:
        """同一序号只保留第一条：站点会把同一章重复列出（实测 35 条 → 19 章）。"""
        refs: List[ChapterRef] = []
        for item in series:
            if not isinstance(item, dict):
                continue
            try:
                sort = int(item.get('sort') or 0)
            except (TypeError, ValueError):
                continue
            if refs and refs[-1].sort == sort:
                continue
            refs.append(ChapterRef(photo_id=str(item.get('id') or ''),
                                   name=str(item.get('name') or ''),
                                   sort=sort))
        refs.sort(key=lambda ref: ref.sort)
        deduped: List[ChapterRef] = []
        for ref in refs:
            if deduped and deduped[-1].sort == ref.sort:
                continue
            deduped.append(ref)
        return deduped

    @staticmethod
    def _chapter_sort(series: List[Any], photo_id: Any) -> int:
        """章节序号：从所在漫画的 `series` 里按 id 找，找不到按 1 处理。"""
        for item in series:
            if not isinstance(item, dict):
                continue
            if str(item.get('id')) == str(photo_id):
                try:
                    return int(item.get('sort') or 1)
                except (TypeError, ValueError):
                    return 1
        return 1


#: 三个分割阈值：小于第一个时是早期图片（不切条）；小于第二个时固定切 10 条；
#: 之后按图片名摘要决定条数，且 421926 之后算法由 10 档改为 8 档。
SCRAMBLE_220980 = 220980
SCRAMBLE_268850 = 268850
SCRAMBLE_421926 = 421926


def segmentation_num(scramble_id: Any, photo_id: Any, filename: str) -> int:
    """纵向分割数；0 表示这张图不需要重排。

    `filename` 是不含后缀的图片名（如 `00001`），它与章节 id 一起参与摘要计算。
    """
    scramble = int(scramble_id)
    aid = int(photo_id)
    if aid < scramble:
        return 0
    if aid < SCRAMBLE_268850:
        return 10
    buckets = 10 if aid < SCRAMBLE_421926 else 8
    digest = hashlib.md5(f'{aid}{filename}'.encode()).hexdigest()
    return ord(digest[-1]) % buckets * 2 + 2


def decode(image: Any, num: int) -> Any:
    """把纵向条带按 `num` 倒序拼回；`num == 0` 时原样返回。

    条带高度向下取整、余数补在第一条上 —— 这是参考实现的取整方式，改成四舍五入
    会让每一条错开一两个像素，累积起来就是整页斜切。
    """
    if num <= 0:
        return image
    from PIL import Image  # 延迟导入：只有真正下载时才需要 Pillow

    width, height = image.size
    decoded = Image.new('RGB', (width, height))
    over = height % num
    for index in range(num):
        move = math.floor(height / num)
        y_src = height - move * (index + 1) - over
        y_dst = move * index
        if index == 0:
            move += over
        else:
            y_dst += over
        decoded.paste(image.crop((0, y_src, width, y_src + move)), (0, y_dst, width, y_dst + move))
    return decoded


def save_image(raw: bytes, save_path: Path, *, scramble_id: Any, photo_id: Any,
               source_name: str) -> None:
    """把接口返回的原始字节落盘。

    `.gif` 原样写出：动画没有单帧可重排，切开就废了。其余按分割数重排后存成 JPEG
    （调用方给的 `save_path` 因此是 `.jpg`；漫画库的扫描只认 jpg/jpeg/png/webp）。
    """
    from PIL import Image  # 延迟导入：只有真正下载时才需要 Pillow

    save_path.parent.mkdir(parents=True, exist_ok=True)
    if source_name.lower().endswith('.gif'):
        save_path.write_bytes(raw)
        return

    stem = Path(source_name).stem
    with Image.open(BytesIO(raw)) as opened:
        num = segmentation_num(scramble_id, photo_id, stem)
        decoded = decode(opened.convert('RGB'), num)
    decoded.save(save_path)
