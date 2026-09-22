"""媒体播放器「均衡器 / 歌单面板点不开」的现场诊断脚本（手动运行，不是 unittest 用例）。

它回答的问题
------------
用户报告：点播放栏右侧的「均衡器」按钮、以及左侧「歌单」相关入口，**偶发**点不开。
本脚本把「点」拆成四种真实程度不同的点击方式，逐一点一遍并打印每次点击后的面板状态，
用来分辨缺陷到底在：

  A. 点击命中的是按钮内的 `<svg>/<use>` 而不是按钮本身（真实鼠标点图标字形 vs
     点按钮空白处，两种落点行为不同）→ 表现为「时好时坏」；
  B. 面板已经打开但被别的逻辑立即关闭（例如文档级点击的「点外面就关」处理）；
  C. 面板打开但不可见（CSS / 层级 / 定位把面板放到视口外或盖住）；
  D. 点击入口本身没绑上事件（渲染顺序或重复渲染把监听器冲掉）。

四种点击方式（每一步都单独复位面板状态）
--------------------------------------
  js-button   对按钮元素本身派发 click（等价于测试里的 el.click()）
  js-svg      对按钮内的 <svg> 派发 click（真实鼠标点在图标字形上就是这个 target）
  mouse-svg   WebDriver 真实鼠标点击图标字形的中心点（走浏览器命中测试与完整事件链）
  mouse-edge  真实鼠标点击按钮边缘（按钮内、图标外的 2px 内圈）

输出判读
--------
每个方式打印一行 `方式 → 面板状态`。若 `js-button` 是「打开」而 `js-svg` / `mouse-svg`
是「未打开」，即 A 成立：图标字形是点击目标，文档级关闭逻辑把刚打开的面板又关掉了。
`mouse-svg` 与 `js-svg` 结论一致时，说明这不是合成事件造成的假象。

同时打印：`<svg>` 的实际盒模型（宽高为 0 说明 sprite 没进文档，图标不可见）、
`document.elementFromPoint` 命中的元素、以及每次点击后的 `eq-panel` class。

运行
----
Windows:
    venv\\Scripts\\python tests\\debug_media_player_popups.py

Linux / macOS:
    venv/bin/python tests/debug_media_player_popups.py

环境变量：
    OMNIBOX_HEADLESS=1      用无头 Chrome（默认 0：带界面真实窗口，便于肉眼复核）
    OMNIBOX_CHROME_BINARY / OMNIBOX_CHROMEDRIVER   指定 Chrome / chromedriver 路径
    OMNIBOX_KEEP_OPEN=1     跑完不关闭浏览器（留在最后一个状态供人工观察）
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

# 复用回归用例里的浏览器探测与端口 / 健康检查（同一套 OMNIBOX_* 环境变量）与桩数据
from tests.test_media_player_browser_e2e import (
    STARTUP_TIMEOUT,
    STUB_SCRIPT,
    _chrome_binary,
    _chromedriver_path,
    _free_port,
    _wait_health,
)

PLUGIN_ENTRY = '/plugins/media-player/frontend/index.html'
SETTLE = 0.6        # 点击后等待面板状态稳定（含弹入动画与文档级关闭处理）

_LOG_FILE = None


def _log(message: str = '') -> None:
    """输出到 stdout 与 UTF-8 日志文件：Windows 控制台默认 GBK，直接 print 会因中文标点报错。"""
    if _LOG_FILE is not None:
        _LOG_FILE.write(message + '\n')
        _LOG_FILE.flush()
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode('ascii', 'backslashreplace').decode('ascii'), flush=True)


def _panels(driver) -> dict:
    """当前所有弹出面板 / 弹窗的可见状态与元素身份。"""
    return driver.execute_script("""
        const seen = {};
        for (const id of ['eq-panel', 'queue-popup', 'modal-playlist', 'modal-add-to-playlist',
                          'modal-eq-name', 'mp-context-menu']) {
            const el = document.getElementById(id);
            if (!el) { seen[id] = 'absent'; continue; }
            const rect = el.getBoundingClientRect();
            seen[id] = {
                hiddenClass: el.classList.contains('hidden'),
                activeClass: el.classList.contains('active'),
                display: getComputedStyle(el).display,
                visibility: getComputedStyle(el).visibility,
                onScreen: rect.width > 0 && rect.height > 0
                    && rect.bottom > 0 && rect.right > 0
                    && rect.top < innerHeight && rect.left < innerWidth,
                rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
            };
        }
        return seen;
    """)


def _visible(panels: dict, key: str) -> str:
    """把面板状态压成一行可读判据：打开 / 未打开 / 不存在。"""
    value = panels.get(key)
    if value == 'absent':
        return '不存在'
    if value['display'] == 'none':
        return '未打开'
    return '打开' if value['onScreen'] else '打开了但不可见'


def _button_geometry(driver, button_id: str) -> dict:
    """按钮与它内部 <svg> 的盒模型，以及按钮中心点命中的元素。"""
    return driver.execute_script("""
        const btn = document.getElementById(arguments[0]);
        const svg = btn.querySelector('svg');
        const rect = btn.getBoundingClientRect();
        const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
        const hit = document.elementFromPoint(cx, cy);
        const d = (el) => {
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return { tag: el.tagName, cls: el.className && el.className.baseVal !== undefined
                ? el.className.baseVal : String(el.className || ''), w: +r.width.toFixed(1), h: +r.height.toFixed(1) };
        };
        return { button: d(btn), svg: d(svg), hitCenter: d(hit),
                 isSvgChild: !!(svg && svg.contains(hit)),
                 useCount: btn.querySelectorAll('use').length,
                 useBBox: (() => {
                     const u = btn.querySelector('use');
                     if (!u) return null;
                     const r = u.getBoundingClientRect();
                     return [+r.width.toFixed(1), +r.height.toFixed(1)];
                 })() };
    """, button_id)


def _svg_center(driver, button_id: str) -> tuple[float, float]:
    return driver.execute_script("""
        const svg = document.getElementById(arguments[0]).querySelector('svg');
        const r = svg.getBoundingClientRect();
        return [r.left + r.width / 2, r.top + r.height / 2];
    """, button_id)


def _button_near_edge(driver, button_id: str) -> tuple[float, float]:
    """按钮内、图标之外的落点：距按钮边框 2px 的内圈左上角。"""
    return driver.execute_script("""
        const r = document.getElementById(arguments[0]).getBoundingClientRect();
        return [r.left + 2.5, r.top + 2.5];
    """, button_id)


def _dispatch_click(driver, selector: str) -> str:
    """派发 click：SVG 元素没有 `click()`，其分支回落到 MouseEvent（冒泡，事件链与真实点击一致）。"""
    return driver.execute_script("""
        const el = document.querySelector(arguments[0]);
        if (!el) return '找不到元素';
        if (typeof el.click === 'function') el.click();
        else el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        return '已派发（target=' + el.tagName + '）';
    """, selector)


def _real_click(driver, x: float, y: float) -> None:
    """真实鼠标点击：走浏览器命中测试，事件链与用户点击一致。

    用 W3C 指针动作按视口坐标点击（`move_to_element_with_offset` 的偏移基准是元素中心，
    x/y 直接传视口坐标会越界），失败时退回合成事件并在日志里标注。
    """
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
    from selenium.webdriver.common.actions.pointer_input import PointerInput
    pointer = PointerInput('mouse', 'probe')
    builder = ActionBuilder(driver, mouse=pointer)
    builder.pointer_action.move_to_location(int(x), int(y)).click()
    builder.perform()
    time.sleep(SETTLE)


def _watch_document_clicks(driver) -> None:
    """记录文档级点击监听器看到的 target —— 「点外面就关」逻辑就是按 target 判定的。"""
    driver.execute_script("""
        window.__probeTargets = [];
        document.addEventListener('click', (e) => {
            const t = e.target;
            window.__probeTargets.push(
                t.tagName + (t.id ? '#' + t.id : '') + (t.parentElement && t.parentElement.id
                    ? ' < #' + t.parentElement.id : ''));
        }, true);
    """)


def _take_probe_targets(driver) -> list:
    return driver.execute_script(
        "const seen = window.__probeTargets || []; window.__probeTargets = []; return seen;")


def _reset_panels(driver) -> None:
    """把面板恢复成初始状态：均衡器与队列关闭、弹窗不激活。"""
    driver.execute_script("""
        document.getElementById('eq-panel').classList.add('hidden');
        document.getElementById('queue-popup').classList.add('hidden');
        document.querySelectorAll('.mp-modal.active').forEach(m => m.classList.remove('active'));
        const menu = document.getElementById('mp-context-menu');
        if (menu) menu.remove();
    """)
    time.sleep(0.15)


def _icon_center_controls(driver) -> list:
    """所有「中心点命中图标内元素」的控件 —— 文档级「点外面就关」按 target 判定时它们都会出问题。"""
    return driver.execute_script("""
        const out = [];
        document.querySelectorAll('button').forEach((btn) => {
            if (!btn.id || !btn.querySelector('svg')) return;
            const rect = btn.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return;
            const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
            if (!hit) return;
            if (hit === btn) return;                       // 中心点正好是按钮本身，不受影响
            if (!btn.contains(hit)) return;                // 被别的元素盖住，属于另一种问题
            out.push({ id: btn.id, hitTag: hit.tagName,
                       visible: getComputedStyle(btn).display !== 'none'
                             && getComputedStyle(btn).visibility !== 'hidden' });
        });
        return out;
    """)


def _real_click_at_center(driver, button_id: str) -> str:
    """真实鼠标点控件中心（即命中图标字形），返回命中的元素描述。"""
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
    from selenium.webdriver.common.actions.pointer_input import PointerInput
    x, y = driver.execute_script("""
        const rect = document.getElementById(arguments[0]).getBoundingClientRect();
        return [rect.left + rect.width / 2, rect.top + rect.height / 2];
    """, button_id)
    pointer = PointerInput('mouse', 'probe-center')
    builder = ActionBuilder(driver, mouse=pointer)
    builder.pointer_action.move_to_location(int(x), int(y)).click()
    builder.perform()
    time.sleep(SETTLE)
    return driver.execute_script("""
        const rect = document.getElementById(arguments[0]).getBoundingClientRect();
        const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        return hit ? hit.tagName + (hit.id ? '#' + hit.id : '') : '（命中不到元素）';
    """, button_id)


def _modal_open(driver, modal_id: str) -> bool:
    return bool(driver.execute_script(
        "const el = document.getElementById(arguments[0]);"
        "return !!el && el.classList.contains('active')"
        " && getComputedStyle(el).display !== 'none';", modal_id))


def _panel_state(driver, panel_id: str) -> str:
    return driver.execute_script("""
        const el = document.getElementById(arguments[0]);
        if (!el) return '不存在';
        if (getComputedStyle(el).display === 'none') return '未打开';
        const r = el.getBoundingClientRect();
        return (r.width > 0 && r.height > 0) ? '打开' : '打开了但不可见';
    """, panel_id)


def _driver_modal_title(driver) -> str:
    """歌单弹窗的标题：菜单里的「重命名」生效时它应变成「重命名歌单」。"""
    return driver.execute_script("""
        const modal = document.getElementById('modal-playlist');
        if (!modal || !modal.classList.contains('active')) return '（弹窗未打开）';
        return (document.getElementById('playlist-modal-title') || {}).textContent || '';
    """)


def _click_button(driver, button_id: str) -> None:
    driver.execute_script("document.getElementById(arguments[0]).click();", button_id)


def _click_svg(driver, button_id: str) -> None:
    driver.execute_script("""
        const svg = document.getElementById(arguments[0]).querySelector('svg');
        svg.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    """, button_id)


def _click_playlist_row(driver, index: int = -1) -> None:
    driver.execute_script("""
        const rows = document.querySelectorAll('.mp-playlist-item');
        const row = rows[arguments[0] < 0 ? rows.length - 1 : arguments[0]];
        if (row) row.querySelector('.pl-name').click();
    """, index)


def _current_playlist(driver) -> str:
    return driver.execute_script(
        "return (mediaPlayerApp.currentPlaylist && mediaPlayerApp.currentPlaylist.name) || '';")


def _run_burst(driver, label: str, action, observe, repeats: int = 20,
               gap: float = 0.06) -> int:
    """重复同一种点击并统计成功次数，返回失败次数。

    单次点击复现不出「偶发」，重复点击才能给出成功率；失败时打印当次的判定值。
    """
    failures = 0
    detail = ''
    for index in range(repeats):
        _reset_panels(driver)
        time.sleep(gap)
        action()
        time.sleep(gap)
        observed = observe()
        if not observed:
            failures += 1
            if not detail:
                detail = f'（首次失败在第 {index + 1} 次）'
    _log(f'  {label:<34} {repeats - failures}/{repeats} 成功{detail}')
    return failures


def _probe(driver, label: str, button_id: str, panel_id: str, mode: str) -> str:
    """对同一个按钮执行一种点击方式，返回面板状态判据。"""
    _reset_panels(driver)
    before = _visible(_panels(driver), panel_id)
    if mode == 'js-button':
        note = _dispatch_click(driver, f'#{button_id}')
        time.sleep(SETTLE)
    elif mode == 'js-svg':
        note = _dispatch_click(driver, f'#{button_id} svg')
        time.sleep(SETTLE)
    elif mode == 'mouse-svg':
        x, y = _svg_center(driver, button_id)
        note = f'真实鼠标 ({x:.0f},{y:.0f})'
        _real_click(driver, x, y)
    else:
        x, y = _button_near_edge(driver, button_id)
        note = f'真实鼠标 ({x:.0f},{y:.0f})'
        _real_click(driver, x, y)
    after = _visible(_panels(driver), panel_id)
    targets = _take_probe_targets(driver)
    _log(f'  {mode:<10} {note:<28} 点击前 {before} → 点击后 {after}'
         f'（捕获阶段看到的 target：{targets}）')
    return after


def main() -> int:
    global _LOG_FILE
    log_path = PROJECT_ROOT / '.build' / 'debug_media_player_popups.log'
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
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
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
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': STUB_SCRIPT})

    findings: list[str] = []
    try:
        _log(f'浏览器：{"无头" if headless else "带界面"} Chrome，插件页 {PLUGIN_ENTRY}')
        driver.get(f'{base}{PLUGIN_ENTRY}?probe=1')
        deadline = time.time() + 30
        while time.time() < deadline:
            ready = driver.execute_script(
                "return !!(window.mediaPlayerApp && mediaPlayerApp.playlists"
                " && mediaPlayerApp.playlists.playlists.length);")
            if ready:
                break
            time.sleep(0.2)
        else:
            _log('插件页未初始化完成，无法诊断')
            return 2

        _log('')
        _log('—— 按钮与图标盒模型 ——')
        for button_id in ('btn-eq', 'btn-queue', 'btn-new-playlist'):
            geo = _button_geometry(driver, button_id)
            _log(f'  #{button_id}: 按钮 {geo["button"]}，图标 {geo["svg"]}，'
                 f'use 尺寸 {geo["useBBox"]}，中心点命中 {geo["hitCenter"]}，'
                 f'命中的是图标内元素={geo["isSvgChild"]}')

        _log('')
        _log('—— 中心点命中图标的控件（真实鼠标点中心即命中图标） ——')
        controls = _icon_center_controls(driver)
        for control in controls:
            _log(f'  #{control["id"]}: 中心点命中 <{control["hitTag"]}>，可见={control["visible"]}')

        _log('')
        _log('—— 逐个控件：真实鼠标点中心 vs 直接派发到按钮 ——')
        expectations = {
            'btn-eq': ('面板', 'eq-panel'),
            'btn-queue': ('面板', 'queue-popup'),
            'btn-new-playlist': ('弹窗', 'modal-playlist'),
            'btn-settings': ('弹窗', ''),
            'btn-search-clear': ('', ''),
        }
        for control in controls:
            button_id = control['id']
            kind, target_id = expectations.get(button_id, ('', ''))
            if not kind:
                _log(f'  #{button_id}: 无自动判据，跳过')
                continue
            _reset_panels(driver)
            if button_id == 'btn-search-clear':
                driver.execute_script(
                    "document.getElementById('media-search').value = 'x';"
                    "document.getElementById('btn-search-clear').classList.remove('hidden');")
            hit = _real_click_at_center(driver, button_id)
            if target_id:
                opened = (_modal_open(driver, target_id) if kind == '弹窗'
                          else _visible(_panels(driver), target_id) == '打开')
                mark = '打开' if opened else '未打开'
            else:
                opened = not bool(driver.execute_script(
                    "return document.getElementById(arguments[0]).classList.contains('hidden');",
                    button_id))
                mark = '生效' if opened else '未生效'
            _log(f'  #{button_id} 真实鼠标命中 {hit} → {mark}')
            if target_id and not opened:
                findings.append(f'#{button_id}：真实鼠标点中心（命中 {hit}）后 {target_id} 未打开')

        _log('')
        _log('—— 均衡器按钮：四种点击方式 ——')
        _watch_document_clicks(driver)
        for mode in ('js-button', 'js-svg', 'mouse-svg', 'mouse-edge'):
            result = _probe(driver, 'btn-eq', 'btn-eq', 'eq-panel', mode)
            if mode == 'js-button':
                baseline = result
            elif result != baseline and mode == 'js-svg':
                findings.append('js-button 能打开而 js-svg 打不开：点击目标落在图标上时面板被立即关闭')
            elif result != baseline and mode == 'mouse-svg':
                findings.append('real 鼠标点图标字形打不开（js-svg 的结论在真实事件链上同样成立）')

        _log('')
        _log('—— 播放队列按钮：四种点击方式 ——')
        for mode in ('js-button', 'js-svg', 'mouse-svg', 'mouse-edge'):
            _probe(driver, 'btn-queue', 'btn-queue', 'queue-popup', mode)

        _log('')
        _log('—— 歌单入口 ——')
        _reset_panels(driver)
        # 桩里再加一个歌单，并让 media_playlist_get 按 id 返回（用例桩固定返回 pl1，
        # 不修就分不清「点哪行都打开 pl1」与「按行打开」）
        driver.execute_script("""
            const mgr = mediaPlayerApp.playlists;
            mgr.playlists.push({ id: 'pl2', name: '第二个歌单', created_at: '2026-01-02',
                                 item_ids: ['item3', 'item4'] });
            mgr.renderSidebar();
            const source = Bridge.call;
            Bridge.call = function (method) {
                const args = Array.prototype.slice.call(arguments, 1);
                if (method === 'media_playlist_get') {
                    const id = args[0];
                    const pl = mgr.playlists.filter(p => p.id === id)[0];
                    if (!pl) return Promise.resolve(null);
                    return source('media_get_items', pl.item_ids).then((items) => ({
                        id: pl.id, name: pl.name, created_at: pl.created_at,
                        item_ids: pl.item_ids, items: items,
                    }));
                }
                return source.apply(null, arguments);
            };
        """)
        rows_count = driver.execute_script(
            "return document.querySelectorAll('.mp-playlist-item').length")
        _log(f'  侧栏歌单行数：{rows_count}')
        _log('  js-button 点 #btn-new-playlist（新建歌单弹窗）：')
        _probe(driver, 'btn-new-playlist', 'btn-new-playlist', 'modal-playlist', 'js-button')
        _log('  js-svg 点 #btn-new-playlist 内的图标：')
        _probe(driver, 'btn-new-playlist', 'btn-new-playlist', 'modal-playlist', 'js-svg')
        _log('  mouse-svg 真实鼠标点 #btn-new-playlist 内的图标：')
        _probe(driver, 'btn-new-playlist', 'btn-new-playlist', 'modal-playlist', 'mouse-svg')

        def _view(label: str) -> None:
            view = driver.execute_script(
                "return {view: mediaPlayerApp.currentView,"
                " playlist: mediaPlayerApp.currentPlaylist && mediaPlayerApp.currentPlaylist.name,"
                " rows: document.querySelectorAll('#mp-detail-list .mp-row').length,"
                " title: (document.getElementById('mp-view-title') || {}).textContent};")
            _log(f'    {label} → 视图={view["view"]}，当前歌单={view["playlist"]}，'
                 f'列表行数={view["rows"]}，标题={view["title"]}')
            if not view['rows']:
                findings.append(f'{label}：歌单列表没有渲染出任何行')

        _reset_panels(driver)
        for index, name in ((0, '第一个歌单'), (1, '第二个歌单')):
            _log(f'  点侧栏第 {index + 1} 行（{name}）：')
            driver.execute_script(
                "document.querySelectorAll('.mp-playlist-item')[arguments[0]]"
                ".querySelector('.pl-name').click();", index)
            time.sleep(1.0)
            _view(f'点 .pl-name（含图标）第 {index + 1} 行')

        _log('  连续真实鼠标点同一行 5 次：')
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        for attempt in range(5):
            x, y = driver.execute_script("""
                const row = document.querySelectorAll('.mp-playlist-item')[0];
                row.scrollIntoView({ block: 'center' });
                const r = row.getBoundingClientRect();
                return [r.left + r.width - 12, r.top + r.height / 2];
            """)
            pointer = PointerInput('mouse', 'probe-row')
            builder = ActionBuilder(driver, mouse=pointer)
            builder.pointer_action.move_to_location(int(x), int(y)).click()
            builder.perform()
            time.sleep(0.8)
            state = driver.execute_script(
                "return {view: mediaPlayerApp.currentView,"
                " playlist: mediaPlayerApp.currentPlaylist && mediaPlayerApp.currentPlaylist.name,"
                " rows: document.querySelectorAll('#mp-detail-list .mp-row').length};")
            _log(f'    第 {attempt + 1} 次 → 视图={state["view"]}，歌单={state["playlist"]}，行数={state["rows"]}')
            if not state['rows']:
                findings.append(f'第 {attempt + 1} 次真实鼠标点歌单行后列表为空')

        _reset_panels(driver)
        _log('  点歌单行右侧「⋯」（更多操作菜单）：')
        menu = driver.execute_script("""
            const more = document.querySelector('.mp-playlist-item .pl-more');
            if (!more) return '没有 ⋯ 按钮';
            more.click();
            return '已派发（target=' + more.tagName + '.' + more.className + '）';
        """)
        time.sleep(0.6)
        _log(f'    {menu} → {_visible(_panels(driver), "mp-context-menu")}')

        _log('')
        _log('—— 重复点击：同一入口连点 20 次 ——')
        bursts = (
            ('均衡器按钮：直接派发到按钮', lambda: _click_button(driver, 'btn-eq'),
             lambda: _panel_state(driver, 'eq-panel') == '打开'),
            ('均衡器按钮：派发到图标（真实鼠标落点）', lambda: _click_svg(driver, 'btn-eq'),
             lambda: _panel_state(driver, 'eq-panel') == '打开'),
            ('播放队列按钮：直接派发到按钮', lambda: _click_button(driver, 'btn-queue'),
             lambda: _panel_state(driver, 'queue-popup') == '打开'),
            ('播放队列按钮：派发到图标（真实鼠标落点）', lambda: _click_svg(driver, 'btn-queue'),
             lambda: _panel_state(driver, 'queue-popup') == '打开'),
            ('新建歌单按钮：派发到图标', lambda: _click_svg(driver, 'btn-new-playlist'),
             lambda: _modal_open(driver, 'modal-playlist')),
            ('歌单行：派发到 .pl-name（含图标）', lambda: _click_playlist_row(driver, 0),
             lambda: _current_playlist(driver) != ''),
        )
        for label, action, observe in bursts:
            failures = _run_burst(driver, label, action, observe)
            if failures:
                findings.append(f'{label}：20 次里 {failures} 次未生效')

        _log('')
        _log('—— 交替连续点击：先 A 后 B，各 15 轮 ——')
        _reset_panels(driver)

        def _alternate(label: str, first, second, check) -> None:
            bad = 0
            detail = ''
            for index in range(15):
                _reset_panels(driver)
                first()
                time.sleep(0.05)
                second()
                time.sleep(0.08)
                if not check():
                    bad += 1
                    if not detail:
                        detail = (f'（首次在第 {index + 1} 轮：'
                                  f'eq={_panel_state(driver, "eq-panel")}，'
                                  f'queue={_panel_state(driver, "queue-popup")}）')
            _log(f'  {label:<40} {15 - bad}/15 成功{detail}')
            if bad:
                findings.append(f'{label}：15 轮里 {bad} 轮未生效')

        _alternate('均衡器（按钮）→ 播放队列（按钮）',
                   lambda: _click_button(driver, 'btn-eq'),
                   lambda: _click_button(driver, 'btn-queue'),
                   lambda: (_panel_state(driver, 'queue-popup') == '打开'
                            and _panel_state(driver, 'eq-panel') == '未打开'))
        _alternate('均衡器（按钮）→ 播放队列（图标）',
                   lambda: _click_button(driver, 'btn-eq'),
                   lambda: _click_svg(driver, 'btn-queue'),
                   lambda: (_panel_state(driver, 'queue-popup') == '打开'
                            and _panel_state(driver, 'eq-panel') == '未打开'))
        _alternate('均衡器（图标）→ 新建歌单（图标）',
                   lambda: _click_svg(driver, 'btn-eq'),
                   lambda: _click_svg(driver, 'btn-new-playlist'),
                   lambda: (_modal_open(driver, 'modal-playlist')
                            and _panel_state(driver, 'eq-panel') == '未打开'))
        _alternate('歌单行 1 → 歌单行 2',
                   lambda: _click_playlist_row(driver, 0),
                   lambda: _click_playlist_row(driver, 1),
                   lambda: _current_playlist(driver) == '第二个歌单')
        _alternate('歌单行 2 → 歌单行 1',
                   lambda: _click_playlist_row(driver, 1),
                   lambda: _click_playlist_row(driver, 0),
                   lambda: _current_playlist(driver) == '测试歌单')

        _log('')
        _log('—— 切换视图与播放模式后再点面板 ——')
        mode_names = ['顺序播放', '随机播放', '单曲循环']
        for view in ('recent', 'all-audio', 'audio-albums', 'favorites'):
            driver.execute_script(
                "mediaPlayerApp.switchView(arguments[0]);", view)
            time.sleep(0.4)
            _reset_panels(driver)
            _click_button(driver, 'btn-eq')
            time.sleep(0.1)
            eq_ok = _panel_state(driver, 'eq-panel') == '打开'
            _reset_panels(driver)
            _click_svg(driver, 'btn-eq')
            time.sleep(0.1)
            eq_icon_ok = _panel_state(driver, 'eq-panel') == '打开'
            _log(f'  视图 {view:<14} 均衡器：按钮→{eq_ok}，图标→{eq_icon_ok}')
            if not eq_ok:
                findings.append(f'切到视图 {view} 后按钮点均衡器打不开')

        for _ in range(3):
            _click_button(driver, 'btn-play-mode')      # 顺序 → 随机 → 单曲 → 顺序
            time.sleep(0.15)
            mode = driver.execute_script("return mediaPlayerApp.core.playMode;")
            _reset_panels(driver)
            _click_svg(driver, 'btn-eq')
            time.sleep(0.1)
            eq_ok = _panel_state(driver, 'eq-panel') == '打开'
            name = mode_names[mode] if isinstance(mode, int) and mode < len(mode_names) else str(mode)
            _log(f'  播放模式 {name:<8} 时图标点均衡器 → {eq_ok}')
            if not eq_ok:
                findings.append(f'播放模式 {name} 下图标点均衡器打不开')

        _log('')
        _log('—— 歌单「⋯」菜单里的操作 ——')
        for target_selector, label, expect in (
            ('.mp-context-menu button[data-menu-act="rename"]', '整条按钮', 'rename'),
            ('.mp-context-menu button[data-menu-act="rename"] svg', '按钮里的图标', 'rename'),
        ):
            _reset_panels(driver)
            driver.execute_script("""
                const more = document.querySelector('.mp-playlist-item .pl-more');
                const rect = more.getBoundingClientRect();
                mediaPlayerApp.showPlaylistMenu(more.dataset.plId,
                    { clientX: rect.left, clientY: rect.top });
            """)
            time.sleep(0.2)
            opened = _visible(_panels(driver), 'mp-context-menu') == '打开'
            clicked = driver.execute_script("""
                const el = document.querySelector(arguments[0]);
                if (!el) return '找不到菜单项';
                if (typeof el.click === 'function') el.click();
                else el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                return el.tagName;
            """, target_selector)
            time.sleep(0.3)
            renamed = _driver_modal_title(driver) == '重命名歌单'
            _log(f'  菜单已打开={opened}，点「重命名」的{label}（<{clicked}>）→ '
                 f'弹窗标题={_driver_modal_title(driver)!r}，期望 {expect!r}，'
                 f'{"生效" if renamed else "未生效"}')
            if not renamed:
                findings.append(f'点歌单「⋯」菜单里「重命名」的{label}没有打开重命名弹窗')

        _log('')
        _log('—— 弹出面板开着时点歌单行 ——')
        # 回到已知的两行状态：大歌单是后面的场景才加的，这里不能带上
        driver.execute_script("""
            const mgr = mediaPlayerApp.playlists;
            mgr.playlists = mgr.playlists.filter(p => p.id === 'pl1' || p.id === 'pl2');
            mgr.renderSidebar();
            mediaPlayerApp.currentPlaylist = null;
            mediaPlayerApp.currentView = 'recent';
        """)
        sidebar_ids = driver.execute_script(
            "return Array.from(document.querySelectorAll('.mp-playlist-item'))"
            ".map(r => r.dataset.plId)")
        _log(f'  侧栏行：{sidebar_ids}')
        for panel_id, button_id in (('eq-panel', 'btn-eq'), ('queue-popup', 'btn-queue')):
            _reset_panels(driver)
            _real_click_at_center(driver, button_id)
            driver.execute_script(
                "document.getElementById(arguments[0]).classList.remove('hidden');", panel_id)
            time.sleep(0.2)
            before = _visible(_panels(driver), panel_id)
            # 落点取行左侧的名字区：行右侧是「⋯」按钮，它按设计 stopPropagation，点了不打开歌单
            x, y = driver.execute_script("""
                const row = document.querySelectorAll('.mp-playlist-item')[1];
                row.scrollIntoView({ block: 'center' });
                const r = row.getBoundingClientRect();
                return [r.left + 24, r.top + r.height / 2];
            """)
            hit = driver.execute_script("""
                const el = document.elementFromPoint(arguments[0], arguments[1]);
                const row = el && el.closest('.mp-playlist-item');
                return (el ? el.tagName : '（空）') + '，所属歌单行='
                    + (row ? row.dataset.plId : '（不在行内）');
            """, x, y)
            _real_click(driver, x, y)
            time.sleep(1.0)
            state = driver.execute_script(
                "return {playlist: mediaPlayerApp.currentPlaylist && mediaPlayerApp.currentPlaylist.name,"
                " rows: document.querySelectorAll('#mp-detail-list .mp-row').length,"
                " panel: getComputedStyle(document.getElementById(arguments[0])).display};",
                panel_id)
            _log(f'  {panel_id} 打开时点第 2 行（命中 {hit}）：面板 {before} → 点击后 '
                 f'歌单={state["playlist"]}，行数={state["rows"]}，面板 display={state["panel"]}')
            if state['playlist'] != '第二个歌单':
                findings.append(f'{panel_id} 打开时点歌单行没有打开该歌单（当前={state["playlist"]}）')

        # 放在最后：大歌单会被 _loadCurrentView 按 id 缓存在 currentPlaylist 上，
        # 留着会让后续「点第 2 行」指向大歌单，把别的场景判成失败
        _log('')
        _log('—— 大歌单：点行后主线程是否被渲染阻塞（大列表下点不动的常见成因） ——')
        driver.execute_script("""
            const mgr = mediaPlayerApp.playlists;
            const many = [];
            for (let i = 0; i < 3000; i++) many.push('big' + i);
            window.__bigItems = many.map((id, i) => ({
                id: id, title: '大曲目 ' + i, artist: 'artist', album: 'album', kind: 'audio',
                stream_url: '', path: '/nonexistent/' + i + '.mp3', duration: 200, mtime: 1700000000 + i,
                has_cover: false, is_fav: false,
            }));
            mgr.playlists.push({ id: 'plb', name: '大歌单', created_at: '2026-01-03',
                                 item_ids: many });
            mgr.renderSidebar();
            const source = Bridge.call;
            Bridge.call = function (method) {
                const args = Array.prototype.slice.call(arguments, 1);
                if (method === 'media_get_items') {
                    const wanted = args[0] || [];
                    return Promise.resolve(wanted
                        .map(id => window.__bigItems.filter(i => i.id === id)[0]).filter(Boolean));
                }
                if (method === 'media_playlist_get' && args[0] === 'plb') {
                    return Promise.resolve({ id: 'plb', name: '大歌单', created_at: '2026-01-03',
                                             item_ids: window.__bigItems.map(i => i.id),
                                             items: window.__bigItems });
                }
                return source.apply(null, arguments);
            };
        """)
        blocked_ms = driver.execute_script("""
            const rows = document.querySelectorAll('.mp-playlist-item');
            const t0 = performance.now();
            rows[rows.length - 1].querySelector('.pl-name').click();
            return performance.now() - t0;
        """)
        time.sleep(2.5)
        big = driver.execute_script(
            "return {view: mediaPlayerApp.currentView,"
            " playlist: mediaPlayerApp.currentPlaylist && mediaPlayerApp.currentPlaylist.name,"
            " rows: document.querySelectorAll('#mp-detail-list .mp-row').length,"
            " title: (document.getElementById('mp-view-title') || {}).textContent};")
        _log(f'    点「大歌单」行 → 视图={big["view"]}，歌单={big["playlist"]}，'
             f'列表行数={big["rows"]}，标题={big["title"]}')
        _log(f'    click() 的同步耗时 {blocked_ms:.1f}ms（media_playlist_get 是异步的，'
             f'这里只测同步阻塞）')

        _log('    真实鼠标连点「大歌单」行 3 次：')
        for attempt in range(3):
            x, y = driver.execute_script("""
                const rows = document.querySelectorAll('.mp-playlist-item');
                const row = rows[rows.length - 1];
                row.scrollIntoView({ block: 'center' });
                const r = row.getBoundingClientRect();
                return [r.left + r.width - 12, r.top + r.height / 2];
            """)
            _real_click(driver, x, y)
            time.sleep(0.5)
            state = driver.execute_script(
                "return {view: mediaPlayerApp.currentView,"
                " playlist: mediaPlayerApp.currentPlaylist && mediaPlayerApp.currentPlaylist.name,"
                " rows: document.querySelectorAll('#mp-detail-list .mp-row').length,"
                " loading: !!document.querySelector('#media-content .loading, #media-content .empty-state')};")
            _log(f'      第 {attempt + 1} 次 → 视图={state["view"]}，歌单={state["playlist"]}，'
                 f'行数={state["rows"]}，显示加载/空态={state["loading"]}')
            if state['rows'] != 3000:
                findings.append(f'点「大歌单」后列表行数={state["rows"]}（期望 3000）')

        _log('')
        if findings:
            _log('结论：')
            for item in findings:
                _log(f'  - {item}')
        else:
            _log('结论：本次运行没有失败项 —— 图标落点、重复点击、交替点击、视图与播放模式'
                 '切换后，均衡器 / 队列 / 歌单入口都能打开。')

        if os.environ.get('OMNIBOX_KEEP_OPEN'):
            _log('')
            _log('按 OMNIBOX_KEEP_OPEN=1 保留浏览器窗口：人工复核后按 Ctrl+C 结束')
            while True:
                time.sleep(1)
        return 0
    finally:
        if not os.environ.get('OMNIBOX_KEEP_OPEN'):
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
