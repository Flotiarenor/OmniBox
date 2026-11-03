"""Pixiv 客户端、认证与全局限速。

状态自持：客户端缓存、令牌轮换回写、限速器与 429 冷却截止时间都在这里。
`refresh_token` 由 Pixiv 在认证时轮换，回写设置是账号级副作用，因此持久化端口的
调用点也收在本组件内，而不是散在任务线程里。
"""

import logging
import time
from typing import Any, Callable, Dict, Optional

from pixiv_mini import PixivClient, PixivError

from shell.backend.ports import SettingsPort

from .limiter import RateLimiter

log = logging.getLogger(__name__)


class PixivSession:
    def __init__(self, settings: SettingsPort,
                 persist_setting: Callable[[str, Any], bool]) -> None:
        # 回写设置不用 ports.PersistPort：它的 save_settings 形参名与
        # PluginBase 的实现不一致，结构化协议在这里不成立（类型检查会拒绝）；
        # 本组件只需要"写单个键"这一件事，窄成可调用对象更准确。
        self._settings = settings
        self._persist_setting = persist_setting
        try:
            initial_rate = float(self._settings.setting("rate_limit", 3))
        except (TypeError, ValueError):
            initial_rate = 3.0
        self.limiter = RateLimiter(rate=initial_rate)
        self._client: Optional[PixivClient] = None
        self._rate_limited_until: Optional[float] = None

    # ---------- 客户端 ----------

    @property
    def cached_client(self) -> Optional[PixivClient]:
        return self._client

    @cached_client.setter
    def cached_client(self, value: Optional[PixivClient]) -> None:
        self._client = value

    def reset_client(self) -> None:
        """丢弃缓存的客户端（代理或凭据变了，下一次调用重新创建）。"""
        self._client = None

    def client(self) -> PixivClient:
        """取（或按当前设置创建）客户端。"""
        if self._client is None:
            kwargs: Dict[str, Any] = {"timeout": 30}
            proxy = (self._settings.setting("proxy") or "").strip()
            if proxy:
                if "://" not in proxy:
                    proxy = f"http://{proxy}"  # 允许只填 127.0.0.1:7890
                kwargs["proxies"] = {"https": proxy, "http": proxy}
            self._client = PixivClient(**kwargs)
        return self._client

    # ---------- 认证 ----------

    def authenticate(self) -> None:
        """用 refresh_token 换 access_token，并回写 Pixiv 可能轮换的 refresh_token。

        轮换后的 token 写不进设置时只记 warning（本会话的 client 仍带着新 token 可用，
        重启后需要重新授权）：认证本身的失败仍以抛 `PixivError` 表达，`None` 返回值
        语义不变。
        """
        token = (self._settings.setting("refresh_token") or "").strip()
        if not token:
            raise PixivError("未配置 refresh_token，请先在设置中填写")
        client = self.client()
        client.auth(refresh_token=token)
        rotated = client.refresh_token
        if rotated and rotated != token:
            if not self._persist_setting("refresh_token", rotated):
                # 轮换后的 token 没落盘：本会话的 client 已经带着新 token，还能继续用，
                # 但设置文件里仍是旧值（下次启动拿它去认证，多半已被 Pixiv 判无效）。
                # 不能打"已回写"——否则"为什么过一段时间就要重新授权"在日志里查不到。
                log.warning("[pixiv-sync] refresh_token 已轮换但未能保存："
                            "本会话仍可用，重启后需要重新授权")
                return
            log.info("[pixiv-sync] refresh_token 已轮换，自动回写设置")

    # ---------- 限速 ----------

    def note_rate_limited(self, cooldown: float = 600.0) -> None:
        """记录 429 后的冷却截止时间（面板据此显示倒计时）。"""
        self._rate_limited_until = time.time() + cooldown

    def cooldown_seconds(self) -> int:
        if self._rate_limited_until is None:
            return 0
        return max(0, int(self._rate_limited_until - time.time()))

    def apply_rate_setting(self) -> None:
        """把 `rate_limit` 设置应用到限速器；非法值保持原速率。"""
        try:
            self.limiter.set_rate(float(self._settings.setting("rate_limit", 3)))
        except (TypeError, ValueError):
            pass
