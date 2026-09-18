"""设置弹窗的遮罩关闭必须只看**按下**位置，不看松开位置 —— 真壳真渲染的验证。

问题：遮罩关闭原先绑的是 `click`，而 `click` 的 target 是**按下点与松开点的最近公共祖先**。
于是"在设置里点住某个输入框 → 向左滑出 .modal-box → 松手"会被算成"点了遮罩"，
设置界面立刻关闭；反过来在弹窗内按住在遮罩上松手，也不会关。

修法：`shell/frontend/public/shell/base.js`（及同一写法的 folder-picker.js、
image-viewer / manga-library / media-player 的插件弹窗）改用 `pointerdown`，
只在按下的位置判断 `e.target === 遮罩`。

本脚本用 CDP 发真实鼠标事件（按下 → 移动 → 松开），不是合成 DOM 事件 ——
只有真实事件才会走浏览器的 click target 计算。用例 A 先挂回旧实现复现问题，
避免"CDP 序列其实没触发 click"时其余用例空过。

用法：
    venv/Scripts/python.exe tests/debug_modal_backdrop_press.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FAILURES: list[str] = []

# 壳的设置弹窗：插件页里还有插件自己的 .modal，必须按 .settings-form 区分
SETTINGS_MODAL = ("Array.from(document.querySelectorAll('.modal'))"
                  ".filter(function(m){return m.querySelector('.settings-form');})[0]")

SETTINGS_SCHEMA = """{
  title: '验证设置字段',
  schema: [{key: 'recent_count', label: '最近阅读显示数量', type: 'number',
            min: 1, max: 50, default: 10, help: '首页「最近阅读」展示的漫画数量'}],
  values: {recent_count: 10}
}"""


def check(name: str, ok: bool, detail: str = '') -> None:
    print(f'  {"PASS" if ok else "FAIL"}  {name}{(" — " + str(detail)) if detail else ""}')
    if not ok:
        FAILURES.append(f'{name} ({detail})')


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _wait_for(predicate, timeout: float = 20.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def main() -> int:
    from selenium import webdriver

    port = _free_port()
    base_url = f'http://127.0.0.1:{port}'
    server = subprocess.Popen(
        [sys.executable, 'main.py', '--web-only', '--port', str(port)],
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_for(lambda: urllib.request.urlopen(
            f'{base_url}/health', timeout=0.5).status == 200, 40.0):
        server.terminate()
        print('壳服务未在超时内就绪')
        return 1

    options = webdriver.ChromeOptions()
    for arg in ('--headless=new', '--disable-gpu', '--no-sandbox', '--window-size=1400,900'):
        options.add_argument(arg)
    driver = webdriver.Chrome(options=options)

    def js(script: str):
        return driver.execute_script(script)

    def open_settings() -> bool:
        # 显式给 schema / values：web-only 下插件页直接调 Bridge 拿不到设置 API，
        # 这里验的是壳弹窗（openSettingsModal + createSettingsForm）的关闭语义
        js(f'window.openSettingsModal({SETTINGS_SCHEMA});')
        return _wait_for(lambda: js(
            f"var m = {SETTINGS_MODAL};"
            "return !!(m && m.querySelector('.field[data-key] input'));"))

    def modal_open() -> bool:
        return bool(js(f'return !!{SETTINGS_MODAL};'))

    def geometry() -> dict:
        """输入框中心（弹窗内）与遮罩上的一点（弹窗外、弹窗层内）。"""
        return js(f"""
          var m = {SETTINGS_MODAL}, box = m.querySelector('.modal-box');
          var input = m.querySelector('.field[data-key="recent_count"] input') || box;
          var r = m.getBoundingClientRect(), b = box.getBoundingClientRect();
          var i = input.getBoundingClientRect(), out = null;
          if (b.left - r.left > 30) out = [b.left - 25, b.top + 20];
          else if (b.top - r.top > 30) out = [b.left + 20, b.top - 25];
          else if (r.bottom - b.bottom > 30) out = [b.left + 20, b.bottom + 25];
          return {{inX: i.left + i.width / 2, inY: i.top + i.height / 2,
                   outX: out ? out[0] : -1, outY: out ? out[1] : -1}};""")

    def mouse(kind: str, x: float, y: float, buttons: int) -> None:
        driver.execute_cdp_cmd('Input.dispatchMouseEvent', {
            'type': kind, 'x': x, 'y': y, 'button': 'left', 'buttons': buttons,
            'clickCount': 1 if kind != 'mouseMoved' else 0})

    def drag(x1: float, y1: float, x2: float, y2: float) -> None:
        mouse('mousePressed', x1, y1, 1)
        for step in range(1, 6):
            mouse('mouseMoved', x1 + (x2 - x1) * step / 5, y1 + (y2 - y1) * step / 5, 1)
        mouse('mouseReleased', x2, y2, 0)

    try:
        driver.get(f'{base_url}/plugins/manga-library/frontend/index.html')
        if not _wait_for(lambda: js("return typeof window.openSettingsModal === 'function';")):
            print('插件页里没有壳注入的 openSettingsModal')
            return 1
        if not open_settings():
            print('设置弹窗未渲染出字段')
            return 1
        pos = geometry()
        if pos['outX'] < 0:
            print('弹窗铺满整屏，找不到遮罩上的落点')
            return 1

        print('\n[A 对照：挂回旧实现（click + e.target === 遮罩）]')
        js(f"""
          var m = {SETTINGS_MODAL};
          m.addEventListener('click', function (e) {{ if (e.target === m) m.remove(); }});""")
        drag(pos['inX'], pos['inY'], pos['outX'], pos['outY'])
        check('弹窗内按下 + 遮罩上松开，旧实现会关闭（确认真实鼠标序列能复现问题）', not modal_open())

        print('\n[B 修复后：弹窗内按下 + 拖到遮罩上松开]')
        if not open_settings():
            print('无法重新打开设置弹窗')
            return 1
        pos = geometry()
        drag(pos['inX'], pos['inY'], pos['outX'], pos['outY'])
        check('设置界面保持不动', modal_open())

        print('\n[C 修复后：按下在遮罩上]')
        if not modal_open() and not open_settings():
            print('无法重新打开设置弹窗')
            return 1
        pos = geometry()
        drag(pos['outX'], pos['outY'], pos['outX'], pos['outY'])
        check('设置界面关闭', not modal_open())

        print('\n[D 修复后：弹窗内按下 + 弹窗内松开]')
        if not open_settings():
            print('无法重新打开设置弹窗')
            return 1
        pos = geometry()
        drag(pos['inX'], pos['inY'], pos['inX'] + 5, pos['inY'] + 5)
        check('设置界面保持不动', modal_open())
    finally:
        driver.quit()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print(f'\n{"全部通过" if not FAILURES else "失败 " + str(len(FAILURES)) + " 项"}')
    for item in FAILURES:
        print('  -', item)
    return 1 if FAILURES else 0


if __name__ == '__main__':
    raise SystemExit(main())
