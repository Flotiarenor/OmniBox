'''
Copyright 2026 flotiarenor

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

桌面窗口的窗口态（目前只有全屏）：两条通道的唯一实现。

为什么单独成模块：`system_toggle_fullscreen` 原先在两条通道上有两份实现 ——
HTTP 侧（`file_server.api_proxy`）是一个恒返回失败的桩，桌面 `js_api` 侧切真窗口。
而壳的 `bridge.ts` 在 pywebview 注入 `window.pywebview` 之前就先装上了 HTTP 替身，
`check()` 随即命中它并把它固化成 `_api`：**桌面窗口里的调用实际走的是 HTTP 那条**，
于是「启动后进入全屏」与「切换窗口全屏」在桌面窗口里都不生效（报告 §6）。桥的初始化
顺序已按 `pywebviewready` 修正，但两条通道仍必须给出同一个结果 —— 判定只写一处。

`webview` 在函数内导入：`webview.platforms.edgechromium` 在导入期就摸 CLR/WinForms
类型，非 Windows 平台上导入会直接抛异常（`main.py` 里也有同一处说明）。
'''

import logging
import threading
from typing import Any, Dict

log = logging.getLogger(__name__)

#: 本进程是否真的出现过一个桌面窗口。**不能**用"库里有没有 window 对象"代替：
#: pywebview 建窗口时才把对象放进 `webview.windows`，`--web-only` 下永远是空的，
#: 而那时 HTTP 端点必须给出"浏览器访问模式"这个明确的拒绝理由。
_has_window = threading.Event()
#: 最近一次由本应用设置的全屏态（用户用 F11 自己切换时读到的值可能滞后）。
_last_fullscreen = False
_state_lock = threading.Lock()

NO_WINDOW_ERROR = '当前没有桌面窗口（浏览器访问模式无法切换窗口全屏）'


def register_window() -> None:
    """桌面窗口创建后调用：此后 `system_*_window_fullscreen` 才有意义。"""
    _has_window.set()
    log.info('[WindowState] 桌面窗口已登记，全屏接口可用')


def has_window() -> bool:
    with _state_lock:
        return _has_window.is_set()


def _first_window() -> Any:
    """当前窗口对象；没有窗口（含 `--web-only`）或窗口已关闭时返回 None。"""
    try:
        import webview
    except Exception as e:  # 非 Windows / 未安装 GUI 后端
        log.warning(f'[WindowState] 无法导入 webview，按无桌面窗口处理: {e}')
        return None
    windows = getattr(webview, 'windows', None) or []
    return windows[0] if windows else None


def get_state() -> Dict[str, Any]:
    """当前窗口全屏态。没有桌面窗口时 `success=False` 并给出原因。"""
    global _last_fullscreen

    window = _first_window()
    if window is None:
        return {'success': False, 'error': NO_WINDOW_ERROR, 'desktop': False}
    value = getattr(window, 'fullscreen', None)
    # 取不到属性（旧版 pywebview / 后端未实现）时退回本应用记录的值：
    # 报告"暂时读不到"比报告一个猜出来的状态更有用。
    if isinstance(value, bool):
        with _state_lock:
            _last_fullscreen = value
        return {'success': True, 'fullscreen': value, 'desktop': True}
    with _state_lock:
        known = _last_fullscreen
    return {'success': True, 'fullscreen': known, 'desktop': True, 'estimated': True}


def set_fullscreen(enabled) -> Dict[str, Any]:
    """把窗口切到指定全屏态。取值必须是布尔值：非布尔值直接拒绝，不猜测意图。"""
    global _last_fullscreen

    if not isinstance(enabled, bool):
        return {'success': False, 'error': f'取值必须是布尔值，实际是 {type(enabled).__name__}'}
    window = _first_window()
    if window is None:
        return {'success': False, 'error': NO_WINDOW_ERROR, 'desktop': False}
    try:
        if _needs_toggle(window, enabled):
            # pywebview 的 toggle_fullscreen() 是**无条件翻转**：重复调用会把状态
            # 翻回去，所以只在目标态与当前态不一致时调它。
            window.toggle_fullscreen()
    except Exception as e:
        log.error(f'[WindowState] 切换全屏失败: {e}')
        return {'success': False, 'error': f'切换全屏失败: {e}'}
    with _state_lock:
        _last_fullscreen = enabled
    log.info(f'[WindowState] 窗口全屏已{"开启" if enabled else "关闭"}')
    return {'success': True, 'fullscreen': enabled, 'desktop': True}


def toggle_fullscreen() -> Dict[str, Any]:
    """在两种全屏态之间切换（设置页的「切换窗口全屏」）。"""
    current = get_state()
    if not current.get('success'):
        return current
    return set_fullscreen(not current.get('fullscreen', False))


def _needs_toggle(window: Any, enabled: bool) -> bool:
    """目标态与当前态不一致时才需要切。

    读不到 `fullscreen` 属性时保守地切一次：静默不动会让用户点了按钮却没有任何
    反应，而多切一次至少还能看到窗口状态变化。
    """
    value = getattr(window, 'fullscreen', None)
    if isinstance(value, bool):
        return value is not enabled
    return True
