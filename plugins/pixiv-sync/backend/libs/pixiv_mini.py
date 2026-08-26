#!/usr/bin/env python
"""
Pixiv 精简同步客户端 (pixiv-mini)
=================================
从 pixivpy (upbit/pixivpy, v3.13.0) 大幅精简而来，仅保留：
  1. refresh_token 认证 (auth)
  2. 关注用户新作流   (illust_follow)   —— 同步/更新关注画作
  3. 关注列表         (user_following)  —— 可配合 user_illusts 全量同步单个画师
  4. 用户作品列表     (user_illusts)
  5. 用户收藏画作     (user_bookmarks_illust) —— 用户喜欢的画作(非关注来源)
  6. 图片下载         (download) + 翻页 (parse_qs)

已删除: pydantic 模型(models.py)、BypassSniApi(bapi.py)、小说/搜索/写操作等全部接口。
所有接口直接返回原始 JSON dict，天然绕过 pixivpy v3.13.0 的 pydantic 校验 bug。

依赖: 仅 requests。可用 pip install requests 安装。
用法:
    from pixiv_mini import PixivClient
    c = PixivClient(proxies={"https": "http://127.0.0.1:7890", "http": "http://127.0.0.1:7890"})
    c.auth(refresh_token="...")
    data = c.illust_follow()          # 关注新作第一页
    while data.get("next_url"):
        qs = c.parse_qs(data["next_url"])
        data = c.illust_follow(**qs)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs as _split_qs
from urllib.parse import urlencode, urlparse

import requests

__version__ = "0.1.0"

# 与 pixivpy 一致的官方 iOS 客户端凭据
_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
_HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"
_USER_AGENT = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"

_API_HOST = "https://app-api.pixiv.net"
_OAUTH_HOST = "https://oauth.secure.pixiv.net"

# OAuth PKCE 流程（用于重新获取 refresh_token）
_REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
_LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"


class PixivError(Exception):
    """Pixiv API 异常"""

    def __init__(self, reason: str, body: str | None = None) -> None:
        self.reason = str(reason)
        self.body = body
        super().__init__(reason)

    def __str__(self) -> str:
        return self.reason


class JsonDict(dict):
    """支持属性访问的 dict，方便 data.illusts[0].title 这种写法"""

    def __getattr__(self, attr: Any) -> Any:
        return self.get(attr)

    def __setattr__(self, attr: Any, value: Any) -> None:
        self[attr] = value


class PixivClient:
    def __init__(
        self,
        proxies: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> None:
        self.session = requests.Session()
        if proxies:
            self.session.proxies.update(proxies)
        self.timeout = timeout
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.user_id: int | None = None
        self.hosts = _API_HOST

    # ------------------------------------------------------------------
    # 内部请求（带重试：代理抖动 / TLS 握手中断等连接层错误自动重试）
    # ------------------------------------------------------------------
    def _request(self, method: str, url: str, retries: int = 3, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return self.session.request(method, url, **kwargs)
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as e:
                last_exc = e
                if attempt < retries:
                    time.sleep(1.5 * attempt)  # 1.5s / 3s 退避
        raise PixivError(f"请求失败（已重试 {retries} 次）: {last_exc}")

    # ------------------------------------------------------------------
    # OAuth PKCE：重新获取 refresh_token（授权码流程，RFC 7636）
    # ------------------------------------------------------------------
    @staticmethod
    def generate_pkce() -> tuple[str, str]:
        """生成 (code_verifier, code_challenge)；challenge 用于拼登录 URL。"""
        code_verifier = secrets.token_urlsafe(32)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return code_verifier, code_challenge

    @staticmethod
    def login_url(code_challenge: str) -> str:
        return f"{_LOGIN_URL}?{urlencode({'code_challenge': code_challenge, 'code_challenge_method': 'S256', 'client': 'pixiv-android'})}"

    def auth_with_code(self, code: str, code_verifier: str) -> JsonDict:
        """用授权码 code + code_verifier 换取 access_token / refresh_token。"""
        data = {
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": _REDIRECT_URI,
        }
        headers = {"user-agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"}
        resp = self._request("POST", f"{_OAUTH_HOST}/auth/token", headers=headers, data=data)
        if resp.status_code not in {200, 301, 302}:
            raise PixivError(
                f"auth_with_code() failed! HTTP {resp.status_code}: {resp.text[:200]}",
                body=resp.text,
            )
        try:
            token = json.loads(resp.text, object_hook=JsonDict)
            self.user_id = token.response.user.id
            self.access_token = token.response.access_token
            self.refresh_token = token.response.refresh_token
        except Exception as e:
            raise PixivError(f"Get access_token error! {e}", body=resp.text) from None
        return token

    # ------------------------------------------------------------------
    # 认证：用 refresh_token 换取 access_token（密码登录已被 Pixiv 废弃）
    # ------------------------------------------------------------------
    def auth(self, refresh_token: str) -> JsonDict:
        local_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00")
        headers = {
            "x-client-time": local_time,
            "x-client-hash": hashlib.md5(
                (local_time + _HASH_SECRET).encode("utf-8")
            ).hexdigest(),
            "app-os": "ios",
            "app-os-version": "14.6",
            "user-agent": _USER_AGENT,
        }
        data = {
            "get_secure_url": 1,
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        resp = self._request(
            "POST", f"{_OAUTH_HOST}/auth/token", headers=headers, data=data
        )
        if resp.status_code not in {200, 301, 302}:
            raise PixivError(
                f"auth() failed! check refresh_token. HTTP {resp.status_code}: {resp.text[:200]}",
                body=resp.text,
            )
        try:
            token = json.loads(resp.text, object_hook=JsonDict)
            self.user_id = token.response.user.id
            self.access_token = token.response.access_token
            self.refresh_token = token.response.refresh_token
        except Exception as e:
            raise PixivError(f"Get access_token error! {e}", body=resp.text) from None
        return token

    def set_auth(self, access_token: str, refresh_token: str | None = None) -> None:
        """手动设置 token（若已有 access_token 可跳过 auth 网络请求）"""
        self.access_token = access_token
        self.refresh_token = refresh_token

    # ------------------------------------------------------------------
    # 内部请求
    # ------------------------------------------------------------------
    def _call(self, path: str, params: dict[str, Any] | None = None) -> JsonDict:
        if not self.access_token:
            raise PixivError("Authentication required! Call auth() or set_auth() first!")
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "user-agent": _USER_AGENT,
            "app-os": "ios",
            "app-os-version": "14.6",
        }
        resp = self._request("GET", f"{self.hosts}{path}", headers=headers, params=params)
        if resp.status_code != 200:
            raise PixivError(
                f"GET {path} HTTP {resp.status_code}: {resp.text[:200]}", body=resp.text
            )
        return json.loads(resp.text, object_hook=JsonDict)

    @staticmethod
    def parse_qs(next_url: str | None) -> dict[str, Any] | None:
        """从 next_url 提取翻页参数；无下一页返回 None"""
        if not next_url:
            return None
        result: dict[str, Any] = {}
        for key, values in _split_qs(urlparse(next_url).query).items():
            if "[" in key and key.endswith("]"):  # seed_illust_ids[] 这类数组
                result[key.split("[")[0]] = values
            else:
                result[key] = values[-1]
        return result

    # ------------------------------------------------------------------
    # 业务接口（全部返回原始 JSON dict）
    # ------------------------------------------------------------------
    def illust_follow(self, restrict: str = "public", offset: int | None = None) -> JsonDict:
        """关注用户的新作（同步/更新关注画作的主接口，可翻页拉全部）"""
        params: dict[str, Any] = {"restrict": restrict}
        if offset:
            params["offset"] = offset
        return self._call("/v2/illust/follow", params)

    def user_following(
        self, user_id: int | str, restrict: str = "public", offset: int | None = None
    ) -> JsonDict:
        """用户的关注列表"""
        params: dict[str, Any] = {"user_id": user_id, "restrict": restrict}
        if offset:
            params["offset"] = offset
        return self._call("/v1/user/following", params)

    def user_illusts(
        self, user_id: int | str, type: str = "illust", offset: int | None = None
    ) -> JsonDict:
        """某个用户的作品列表（可对单个画师全量同步）"""
        params: dict[str, Any] = {"user_id": user_id, "type": type}
        if offset:
            params["offset"] = offset
        return self._call("/v1/user/illusts", params)

    def user_bookmarks_illust(
        self,
        user_id: int | str,
        restrict: str = "public",
        max_bookmark_id: int | str | None = None,
    ) -> JsonDict:
        """用户喜欢的画作（收藏列表，restrict=public 为公开收藏）"""
        params: dict[str, Any] = {"user_id": user_id, "restrict": restrict}
        if max_bookmark_id:
            params["max_bookmark_id"] = max_bookmark_id
        return self._call("/v1/user/bookmarks/illust", params)

    def illust_detail(self, illust_id: int | str) -> JsonDict:
        """作品详情（返回 illust 对象，含画师 user 信息；用于迁移/查询归属）"""
        return self._call("/v1/illust/detail", {"illust_id": illust_id})

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def download(
        self,
        url: str,
        path: str = os.path.curdir,
        name: str | None = None,
        replace: bool = False,
    ) -> bool:
        """下载图片（i.pximg.net 需要 Referer 防盗链头）"""
        name = name or os.path.basename(url)
        file = os.path.join(path, name)
        if os.path.exists(file) and not replace:
            return False
        os.makedirs(path, exist_ok=True)
        headers = {"Referer": "https://app-api.pixiv.net/", "user-agent": _USER_AGENT}
        with self._request("GET", url, headers=headers, stream=True) as resp:
            if resp.status_code != 200:
                raise PixivError(f"download HTTP {resp.status_code}: {url}")
            try:
                with open(file, "wb") as f:
                    shutil.copyfileobj(resp.raw, f)
            except Exception:
                # 流中断：清理半写/空文件，避免残留
                try:
                    os.remove(file)
                except OSError:
                    pass
                raise PixivError(f"download 中断: {url}") from None
        # 0 字节检查：空文件视为下载失败，删除后报错（下次重试）
        if os.path.getsize(file) == 0:
            try:
                os.remove(file)
            except OSError:
                pass
            raise PixivError(f"download 得到空文件: {url}")
        return True
