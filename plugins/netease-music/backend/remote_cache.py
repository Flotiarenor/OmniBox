"""远端来源的缓存失效与新鲜期投影。

网易云没有本地文件树：数据全部来自 ncm-cli 请求，本地只有一份播放地址缓存，判据
只有时间。本组件决定两件事——什么时候丢弃缓存（登录态变化）与界面该看到哪段新鲜期；
真正的丢弃动作由入口注入（它持有数据接口）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from shell.backend.freshness import engine_for

log = logging.getLogger(__name__)

#: 失效原因：登录态变化 —— 另一个账号留下的列表与播放地址都不再可信。
IDENTITY_REASON = 'identity'


class RemoteCache:
    def __init__(self, plugin: Any, invalidate: Callable[[str], Dict[str, Any]],
                 ttl_seconds: float) -> None:
        self._plugin = plugin
        self._invalidate = invalidate
        self._ttl_seconds = float(ttl_seconds)
        #: 上一次已知的登录态；`None` 表示还没判断过。
        self._logged_in: Optional[bool] = None

    # ----- 失效 -----

    def invalidate(self, reason: str = IDENTITY_REASON) -> Dict[str, Any]:
        """丢弃远端派生缓存，并让新鲜期从此刻重新起算。"""
        engine = engine_for(self._plugin)
        if engine is None:
            return {'reason': reason, 'cleared': self._invalidate(reason)}
        log.info(f'[{getattr(self._plugin, "name", "netease-music")}] 丢弃远端缓存: {reason}')
        # `force=True` 让远端同步忽略 TTL，`reason` 原样交给插件的 invalidate，
        # 新鲜期同时从这次失效重新起算。
        report = engine.sync('', force=True, reason=reason)
        if not report.get('invalidated'):
            # 引擎正忙或异常：登录态变化必须立刻生效，绕过去直接丢弃本地缓存。
            return {'reason': reason, 'cleared': self._invalidate(reason),
                    'engine': str(report.get('reason') or report.get('action') or '')}
        return {'reason': reason, 'cleared': report.get('cleared')}

    def note_login_state(self, logged_in: bool) -> Optional[Dict[str, Any]]:
        """登录态转为「已登录」时作废缓存；重复的「已登录」不重复丢弃。"""
        previous = self._logged_in
        self._logged_in = logged_in
        if logged_in and previous is not True:
            return self.invalidate(IDENTITY_REASON)
        return None

    def mark_login_started(self) -> Dict[str, Any]:
        """登录流程已开始：旧账号的数据不再可信，随后的 `check_login` 复用这次失效。"""
        self._logged_in = True
        return self.invalidate(IDENTITY_REASON)

    # ----- 状态投影 -----

    def state(self) -> Dict[str, Any]:
        """远端新鲜期：模式、TTL 与到期时刻（界面按"数据新鲜期"显示，不是"已索引 N 项"）。

        引擎给出 `fresh_until` 时直接用它；只有 `ttl_remaining` 时按"上次同步时刻 +
        TTL"折算；两者都拿不到时给 0，表示不清楚。
        """
        engine = engine_for(self._plugin)
        if engine is None:
            return {'mode': 'remote', 'ttl_seconds': self._ttl_seconds,
                    'ttl_remaining': 0.0, 'fresh_until': 0.0}
        engine_state = engine.state() or {}
        ttl = float(engine_state.get('ttl_seconds') or self._ttl_seconds)
        last_sync = float(engine_state.get('last_sync_at') or 0.0)
        until = engine_state.get('fresh_until')
        if until is None:
            until = (last_sync + ttl) if (ttl > 0 and last_sync > 0) else 0.0
        remaining = float(engine_state.get('ttl_remaining') or 0.0)
        return {'mode': str(engine_state.get('mode') or 'remote'),
                'ttl_seconds': ttl,
                'ttl_remaining': round(max(0.0, remaining), 1),
                'fresh_until': round(float(until), 3)}
