"""后台同步的"脉冲"：脏标志、唤醒事件与间隔。

状态自持：两个脏标志、唤醒事件、上次成功时间与上次错误都在这里。它是最底层的一块
—— 保存名单（Storage）、发布注册记录（Node）、登记对端（Peers）都会往这里"打一针"，
后台循环（Discovery）在这里等下一次唤醒。**它不持有线程，也不发请求**：
线程与请求都在 Discovery，因此这个模块不依赖任何上层组件。

为什么脏标志要一把独立的锁（`state_lock`）：它只保护两个布尔值，与连接池、注册表的
那把 `lock` 分开，后台循环读标志时不会把界面请求堵在连接/注册表上。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional, Tuple

from shell.backend.ports import SettingsPort


class SyncPulse:
    def __init__(self, settings: SettingsPort) -> None:
        self._settings = settings
        # 唤醒事件：`request()` 之后后台循环立刻重算间隔并跑一轮，不等到下一个周期。
        self.wake_event = threading.Event()
        # 停止事件由 Discovery 在 start()/stop() 里清/立；放在这里是为了让"该不该继续"
        # 与"有没有变更"读同一份状态。
        self.stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._roster_dirty = False
        self._registry_dirty = False
        self.last_sync_at: Optional[int] = None
        self.last_error = ''

    def interval(self) -> int:
        """后台同步间隔；设置越界或非法时回落到默认 60 秒。"""
        try:
            value = int(self._settings.setting('sync_interval_seconds', 60) or 60)
        except (TypeError, ValueError):
            value = 60
        return max(5, min(3600, value))

    def request(self, *, roster_dirty: bool = False,
                registry_dirty: bool = False) -> None:
        """标记"有变更待同步"并立刻唤醒后台循环（不等下一个轮询周期）。"""
        with self._state_lock:
            if roster_dirty:
                self._roster_dirty = True
            if registry_dirty:
                self._registry_dirty = True
        self.wake_event.set()

    def clear(self, *, roster: bool = False, registry: bool = False) -> None:
        with self._state_lock:
            if roster:
                self._roster_dirty = False
            if registry:
                self._registry_dirty = False

    def dirty(self) -> Tuple[bool, bool]:
        """(名单脏, 注册表脏)。一次取两个的原因：调用方总是同时判两者。"""
        with self._state_lock:
            return self._roster_dirty, self._registry_dirty

    def status(self) -> Dict[str, Any]:
        roster_dirty, registry_dirty = self.dirty()
        return {
            'interval': self.interval(),
            'last_sync_at': self.last_sync_at,
            'last_error': self.last_error,
            'roster_dirty': roster_dirty,
            'registry_dirty': registry_dirty,
        }
