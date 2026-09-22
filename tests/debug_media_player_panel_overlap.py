"""媒体播放器弹出面板与播放栏的遮挡诊断（手动运行，不是 unittest 用例）。

要回答的问题
------------
弹出面板（均衡器 / 播放队列）用 `bottom: calc(var(--mp-pb-height) + 28px)` 贴底定位，
壳窗口变小时面板会往下压到播放栏上，把播放栏的按钮盖住：用户点按钮其实点在面板上，
第一次点击只相当于关掉面板，要再点一次才打开。

本脚本在若干视口尺寸下量三件事：
  1. 面板矩形与播放栏矩形是否相交、相交多少像素；
  2. 播放栏里哪些可点控件被面板盖住（用 elementFromPoint 逐控件判定）；
  3. 面板自身是否超出视口（顶部被裁 / 高度不够）。

运行
----
    venv\\Scripts\\python tests\\debug_media_player_panel_overlap.py

环境变量：
    OMNIBOX_HEADLESS=1                             用无头 Chrome（默认 0）
    OMNIBOX_CHROME_BINARY / OMNIBOX_CHROMEDRIVER   指定 Chrome / chromedriver 路径
    OMNIBOX_SIZES                                  自定义尺寸，形如 1400x900,1184x749,1024x600
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_media_player_browser_e2e import (
    STARTUP_TIMEOUT,
    STUB_SCRIPT,
    _chrome_binary,
    _chromedriver_path,
    _free_port,
    _wait_health,
)

PLUGIN_ENTRY = '/plugins/media-player/frontend/index.html'
DEFAULT_SIZES = ((1400, 900), (1184, 749), (1100, 650), (1024, 600), (960, 520), (1400, 480))

GEO_SCRIPT = """
const panel = document.getElementById(arguments[0]);
const bar = document.getElementById('player-bar');
const p = panel.getBoundingClientRect(), b = bar.getBoundingClientRect();
const controls = [];
document.querySelectorAll('#player-bar button').forEach((btn) => {
    const r = btn.getBoundingClientRect();
    if (!r.width || !r.height) return;
    if (getComputedStyle(btn).display === 'none') return;
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    controls.push({
        id: btn.id || btn.className, covered: !!hit && hit !== btn && !btn.contains(hit),
        coveredBy: hit ? hit.tagName + (hit.id ? '#' + hit.id : '') : '',
        top: Math.round(r.top), bottom: Math.round(r.bottom),
    });
});
return {
    panel: [Math.round(p.left), Math.round(p.top), Math.round(p.right), Math.round(p.bottom)],
    bar: [Math.round(b.left), Math.round(b.top), Math.round(b.right), Math.round(b.bottom)],
    barHeight: Math.round(b.height),
    panelBottom: getComputedStyle(panel).bottom,
    panelMaxHeight: getComputedStyle(panel).maxHeight,
    panelHeight: Math.round(p.height),
    overlapY: Math.max(0, Math.min(p.bottom, b.bottom) - Math.max(p.top, b.top)),
    overlapX: Math.max(0, Math.min(p.right, b.right) - Math.max(p.left, b.left)),
    offscreenTop: p.top < 0 ? Math.round(-p.top) : 0,
    viewport: [innerWidth, innerHeight],
    controls: controls,
};
"""


def _log(message: str = '') -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode('ascii', 'backslashreplace').decode('ascii'), flush=True)


def _sizes() -> list:
    raw = (os.environ.get('OMNIBOX_SIZES') or '').strip()
    if not raw:
        return list(DEFAULT_SIZES)
    out = []
    for item in raw.split(','):
        width, _, height = item.strip().partition('x')
        if width.isdigit() and height.isdigit():
            out.append((int(width), int(height)))
    return out or list(DEFAULT_SIZES)


def main() -> int:
    if _chrome_binary() is None:
        _log('未检测到 Chrome/Edge：可用 OMNIBOX_CHROME_BINARY 指定可执行文件路径')
        return 2
    try:
        import selenium  # noqa: F401
    except Exception:
        _log('未安装 selenium：先执行 venv\\Scripts\\pip install -r requirements-e2e.txt')
        return 2

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    headless = os.environ.get('OMNIBOX_HEADLESS', '0') not in ('', '0', 'false', 'False')
    port = _free_port()
    base = f'http://127.0.0.1:{port}'
    server = subprocess.Popen(
        [sys.executable, 'main.py', '--web-only', '--port', str(port)],
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_health(base, STARTUP_TIMEOUT):
        server.terminate()
        _log('服务未在超时内就绪')
        return 2

    options = Options()
    binary = _chrome_binary()
    if binary:
        options.binary_location = binary
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-gpu')
    options.add_argument('--mute-audio')
    options.add_argument('--window-size=1400,900')
    driver_path = _chromedriver_path()
    try:
        driver = (webdriver.Chrome(options=options, service=Service(driver_path))
                  if driver_path else webdriver.Chrome(options=options))
    except Exception as exc:
        server.terminate()
        _log(f'无法启动 WebDriver：{type(exc).__name__}: {exc}')
        return 2
    driver.set_page_load_timeout(30)
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': STUB_SCRIPT})

    problems: list[str] = []
    try:
        driver.get(f'{base}{PLUGIN_ENTRY}?overlap=1')
        deadline = time.time() + 30
        while time.time() < deadline:
            if driver.execute_script("return !!(window.mediaPlayerApp && mediaPlayerApp.core);"):
                break
            time.sleep(0.2)
        else:
            _log('插件页未初始化完成')
            return 2

        for width, height in _sizes():
            driver.set_window_size(width, height)
            time.sleep(0.4)
            viewport = driver.execute_script("return [innerWidth, innerHeight];")
            _log('')
            _log(f'=== 窗口 {width}×{height}（插件页视口 {viewport[0]}×{viewport[1]}） ===')
            for panel_id in ('eq-panel', 'queue-popup'):
                driver.execute_script(
                    "document.querySelectorAll('.mp-pop').forEach(p => p.classList.add('hidden'));")
                # 走真实打开路径（含 _positionPops 的调用），不自己摆面板
                driver.execute_script(
                    "mediaPlayerApp.currentItem = null;"
                    "if (arguments[0] === 'eq-panel') mediaPlayerApp._toggleEQ();"
                    "else mediaPlayerApp._toggleQueue();", panel_id)
                time.sleep(0.25)
                geo = driver.execute_script(GEO_SCRIPT, panel_id)
                covered = [c for c in geo['controls'] if c['covered']]
                _log(f'  {panel_id}：面板={geo["panel"]}，播放栏={geo["bar"]}'
                     f'（高 {geo["barHeight"]}，computed bottom={geo["panelBottom"]}，'
                     f'面板高 {geo["panelHeight"]}，max-height={geo["panelMaxHeight"]}）')
                _log(f'    与播放栏相交：纵向 {geo["overlapY"]}px，横向 {geo["overlapX"]}px；'
                     f'面板顶边离视口上沿 {geo["panel"][1]}px（负值表示被裁）')
                if covered:
                    names = '，'.join(f'#{c["id"]}（被 {c["coveredBy"]} 盖住）' for c in covered)
                    _log(f'    被盖住的播放栏控件：{names}')
                    problems.append(f'{width}×{height}：{panel_id} 盖住 {len(covered)} 个播放栏控件')
                else:
                    _log('    被盖住的播放栏控件：无')
                if geo['offscreenTop']:
                    problems.append(f'{width}×{height}：{panel_id} 顶部被视口裁掉 {geo["offscreenTop"]}px')

        _log('')
        if problems:
            _log('结论：存在遮挡或裁切：')
            for item in problems:
                _log(f'  - {item}')
            return 1
        _log('结论：所有被测尺寸下，面板都不盖播放栏控件，也没有超出视口。')
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == '__main__':   # pragma: no cover
    raise SystemExit(main())
