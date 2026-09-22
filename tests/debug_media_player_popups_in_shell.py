"""媒体播放器「均衡器 / 队列 / 歌单菜单」修复后的壳内复核脚本（手动运行，不是 unittest 用例）。

与 tests/debug_media_player_popups.py 的区别：本脚本不注入桩，走的是
**真实壳页面 → 真实插件 iframe → 真实媒体库**，即用户实际看到的那条加载路径。
桩脚本直接打开插件页（顶层文档），position:fixed 的弹出面板参照的是插件页视口；
壳内插件页在 iframe 里，面板参照的是 iframe 视口 —— 命中测试的坐标系不同，
所以修复必须在壳内再复核一次。

检查项（全部用真实鼠标点图标落点，即用户点字形的位置）：
  1. 均衡器按钮：点开 / 再点关；
  2. 播放队列按钮：点开 / 再点关；
  3. 交替各 5 轮：点队列必须打开队列并关掉均衡器；
  4. 切换视图（最近播放 / 全部音乐）与播放模式（下一档）后均衡器仍能点开；
  5. 侧栏第一个歌单行：点开后有列表行（没有歌单则跳过该项）。

运行
----
Windows:
    venv\\Scripts\\python tests\\debug_media_player_popups_in_shell.py

环境变量：
    OMNIBOX_HEADLESS=1     用无头 Chrome（默认 0：带界面，便于肉眼复核）
    OMNIBOX_CHROME_BINARY / OMNIBOX_CHROMEDRIVER   指定 Chrome / chromedriver 路径
    OMNIBOX_DEBUG_VIEW     左侧导航项名称（默认「媒体播放器」）
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
    _chrome_binary,
    _chromedriver_path,
    _free_port,
    _wait_health,
)

SETTLE = 0.45
_LOG_FILE = None


def _log(message: str = '') -> None:
    if _LOG_FILE is not None:
        _LOG_FILE.write(message + '\n')
        _LOG_FILE.flush()
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode('ascii', 'backslashreplace').decode('ascii'), flush=True)


def _click_center(driver, selector: str) -> str:
    """在元素中心「按真实鼠标的落点」派发 click，返回命中的元素描述。

    壳内插件页在 iframe 里，Selenium 的原始指针坐标按顶层窗口解释（实测点插件按钮会落到
    顶层文档的其他元素上，日志里出现 `target=DIV`、`target=BUTTON#btn-next` 这类外来的
    target）。这里改为**按命中测试的落点派发**：真实鼠标点在按钮中心时，浏览器给出的
    `event.target` 就是该点最深的元素（图标是 `<svg>` / `<use>`），从该元素派发冒泡的
    click 与真实点击的 target、冒泡路径、文档级监听器行为完全一致 —— 正是本缺陷的路径。
    """
    hit = driver.execute_script("""
        const rect = document.querySelector(arguments[0]).getBoundingClientRect();
        const el = document.elementFromPoint(rect.left + rect.width / 2,
                                             rect.top + rect.height / 2);
        if (!el) return '（空）';
        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        return el.tagName + (el.id ? '#' + el.id : '');
    """, selector)
    time.sleep(SETTLE)
    return hit


def _panel(driver, panel_id: str) -> str:
    return driver.execute_script(
        "const el = document.getElementById(arguments[0]);"
        "if (!el) return '不存在';"
        "if (getComputedStyle(el).display === 'none') return '未打开';"
        "const r = el.getBoundingClientRect();"
        "return (r.width > 0 && r.height > 0) ? '打开' : '打开了但不可见';", panel_id)


def _hide_panels(driver) -> None:
    driver.execute_script(
        "document.querySelectorAll('.mp-pop').forEach(p => p.classList.add('hidden'));")
    time.sleep(0.1)


def _wait(driver, script: str, message: str, timeout: float = 20.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        if driver.execute_script(script):
            return
        time.sleep(0.2)
    raise AssertionError(f'{message}（{timeout:.0f}s 超时）')


def main() -> int:
    global _LOG_FILE
    log_path = PROJECT_ROOT / '.build' / 'debug_media_player_popups_in_shell.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _LOG_FILE = log_path.open('w', encoding='utf-8')
    _log(f'日志：{log_path}')
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
        _log('服务未在超时内就绪：先确认 `python main.py --web-only` 能正常启动')
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
    options.add_argument('--autoplay-policy=no-user-gesture-required')
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

    failures: list[str] = []
    try:
        _log(f'浏览器：{"无头" if headless else "带界面"} Chrome，入口 {base}（真实壳 → 插件 iframe）')
        driver.get(base)
        _wait(driver, "return document.querySelectorAll('.nav-item').length > 0;", '壳导航未渲染')
        target = (os.environ.get('OMNIBOX_DEBUG_VIEW') or '媒体播放器').strip()
        entered = driver.execute_script("""
            const item = Array.from(document.querySelectorAll('.nav-item'))
                .find(e => e.textContent.includes(arguments[0]));
            if (!item) return false;
            item.click();
            return true;
        """, target)
        if not entered:
            _log(f'壳导航里找不到「{target}」：可用 OMNIBOX_DEBUG_VIEW 指定其他名称')
            return 2

        frame = None
        deadline = time.time() + 20
        while time.time() < deadline and frame is None:
            for candidate in driver.find_elements('tag name', 'iframe'):
                src = candidate.get_attribute('src') or ''
                if 'media-player' in src:
                    frame = candidate
                    break
            time.sleep(0.2)
        if frame is None:
            _log('未找到媒体播放器 iframe')
            return 2
        frame_src = frame.get_attribute('src')     # 切进 frame 后元素引用失效，先取 src
        driver.switch_to.frame(frame)
        _log(f'已进入插件 iframe：{frame_src}')
        _wait(driver, "return !!window.mediaPlayerApp && !!mediaPlayerApp.core;", '插件未初始化')
        _wait(driver, "return !!document.querySelector('#media-content .mp-row, #media-detail-list .mp-row,"
                      "#mp-detail-list .mp-row, .empty-state, .loading');", '媒体区未渲染')
        time.sleep(0.5)

        _log('')
        _log('—— 0. 环境自证：脚本是否为修复后的版本、图标是否渲染 ——')
        loaded = driver.execute_script("""
            const svg = document.getElementById('btn-eq').querySelector('svg');
            const use = svg && svg.querySelector('use');
            const r = svg ? svg.getBoundingClientRect() : null;
            const ur = use ? use.getBoundingClientRect() : null;
            const rect = document.getElementById('btn-eq').getBoundingClientRect();
            const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
            return {
                knowPanelButton: typeof mediaPlayerApp._isClickInside === 'function',
                svgBox: r ? [Math.round(r.width), Math.round(r.height)] : null,
                useBox: ur ? [Math.round(ur.width), Math.round(ur.height)] : null,
                centerHit: hit ? hit.tagName + (hit.id ? '#' + hit.id : '') : '（空）',
                buttonBox: [Math.round(rect.left), Math.round(rect.top),
                            Math.round(rect.right), Math.round(rect.bottom)],
                viewport: [innerWidth, innerHeight],
            };
        """)
        _log(f'  已加载含修复助手的脚本={loaded["knowPanelButton"]}')
        _log(f'  #btn-eq：按钮盒={loaded["buttonBox"]}，图标 svg 盒={loaded["svgBox"]}，'
             f'use 盒={loaded["useBox"]}，中心命中={loaded["centerHit"]}')
        _log(f'  插件页视口={loaded["viewport"]}')
        if not loaded['knowPanelButton']:
            _log('  ! 运行中的 app-ui-events.js 没有 _isClickInside：本脚本针对修复后的版本，'
                 '旧版本请改用 tests/debug_media_player_popups.py')
            return 2
        if loaded['svgBox'] == [0, 0]:
            _log('  ! 图标未渲染（svg 盒为 0×0）：壳内 sprite 没进文档，'
                 '此时鼠标点中心落在按钮本身，本脚本无法复核图标落点路径')

        _log('')
        _log('—— 1/2. 均衡器与播放队列：点图标落点开 / 再点关 ——')
        for button_id, panel_id in (('btn-eq', 'eq-panel'), ('btn-queue', 'queue-popup')):
            _hide_panels(driver)
            hit = _click_center(driver, f'#{button_id}')
            opened = _panel(driver, panel_id)
            _log(f'  #{button_id} 点中心（命中 {hit}）→ {panel_id}={opened}')
            if opened != '打开':
                failures.append(f'壳内点 #{button_id} 未打开 {panel_id}（命中 {hit}）')
                continue
            _click_center(driver, f'#{button_id}')
            closed = _panel(driver, panel_id)
            _log(f'  #{button_id} 再点中心 → {panel_id}={closed}')
            if closed != '未打开':
                failures.append(f'壳内再点 #{button_id} 未关闭 {panel_id}')

        _log('')
        _log('—— 3. 交替 5 轮：点队列要打开队列并关掉均衡器 ——')
        for round_index in range(5):
            _hide_panels(driver)
            _click_center(driver, '#btn-eq')
            if _panel(driver, 'eq-panel') != '打开':
                failures.append(f'壳内第 {round_index + 1} 轮均衡器未打开')
                break
            _click_center(driver, '#btn-queue')
            queue_state = _panel(driver, 'queue-popup')
            eq_state = _panel(driver, 'eq-panel')
            _log(f'  第 {round_index + 1} 轮：queue={queue_state}，eq={eq_state}')
            if queue_state != '打开' or eq_state != '未打开':
                failures.append(f'壳内第 {round_index + 1} 轮交替点击异常（queue={queue_state}，eq={eq_state}）')
                break

        _log('')
        _log('—— 4. 切视图与播放模式后点均衡器 ——')
        for view in ('recent', 'all-audio'):
            driver.execute_script("mediaPlayerApp.switchView(arguments[0]);", view)
            time.sleep(0.6)
            _hide_panels(driver)
            _click_center(driver, '#btn-eq')
            state = _panel(driver, 'eq-panel')
            _log(f'  视图 {view:<10} → 均衡器={state}')
            if state != '打开':
                failures.append(f'壳内视图 {view} 下均衡器未打开')
        driver.execute_script("document.getElementById('btn-play-mode').click();")
        time.sleep(0.3)
        mode = driver.execute_script("return mediaPlayerApp.core.playMode;")
        _hide_panels(driver)
        _click_center(driver, '#btn-eq')
        state = _panel(driver, 'eq-panel')
        _log(f'  切到播放模式 {mode} → 均衡器={state}')
        if state != '打开':
            failures.append('壳内切换播放模式后均衡器未打开')

        _log('')
        _log('—— 5. 侧栏歌单行与「⋯」菜单 ——')
        rows = driver.execute_script("return document.querySelectorAll('.mp-playlist-item').length")
        if not rows:
            _log('  当前没有歌单：跳过（先建一个歌单再跑本项）')
        else:
            driver.execute_script(
                "document.querySelector('.mp-playlist-item .pl-name').click();")
            _wait(driver, "return mediaPlayerApp.currentView === 'playlist';",
                  '点歌单行后未切到歌单视图')
            count = driver.execute_script(
                "return document.querySelectorAll('#mp-detail-list .mp-row').length;")
            _log(f'  点第 1 行 → 视图=playlist，列表行数={count}')
            if not count:
                failures.append('壳内点歌单行后列表没有行')

            _hide_panels(driver)
            driver.execute_script("""
                const more = document.querySelector('.mp-playlist-item .pl-more');
                const r = more.getBoundingClientRect();
                mediaPlayerApp.showPlaylistMenu(more.dataset.plId,
                    { clientX: r.left, clientY: r.top });
            """)
            time.sleep(0.2)
            hit = _click_center(driver, '.mp-context-menu button[data-menu-act="rename"] svg')
            time.sleep(0.4)
            title = driver.execute_script(
                "const m = document.getElementById('modal-playlist');"
                "if (!m || !m.classList.contains('active')) return '（未打开）';"
                "return document.getElementById('playlist-modal-title').textContent;")
            _log(f'  点「重命名」图标（命中 {hit}）→ 弹窗标题={title!r}')
            if title != '重命名歌单':
                failures.append('壳内点菜单「重命名」图标未打开弹窗')

        _log('')
        if failures:
            _log('结论：发现失败项：')
            for item in failures:
                _log(f'  - {item}')
            return 1
        _log('结论：壳内（真实 iframe 环境）全部通过。')
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
        if _LOG_FILE is not None:
            _LOG_FILE.close()


if __name__ == '__main__':   # pragma: no cover
    raise SystemExit(main())
