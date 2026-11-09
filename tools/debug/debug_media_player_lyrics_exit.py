"""媒体播放器「退出歌词沉浸页后底部出现黑色阴影」的现场诊断（手动运行，不是用例）。

要回答的问题
------------
用户在媒体播放器里播放音乐，点播放栏的 ⛶（`#btn-fullscreen`，标题「沉浸歌词」）打开歌词
沉浸页，再点返回退出；退出之后**页面底部**会出现一条黑色阴影 / 黑边。本脚本在真实浏览器里
复现这条操作链，并逐项量出退出后底部到底是什么东西：

  1. 底部若干行的像素均值（退出前后对比：用户看到的"黑"必须能被数值复现，而不是凭印象）；
  2. `elementFromPoint` 在底边上从上往下命中的元素（黑边是谁画的）；
  3. `#lyrics-page` 的矩形、computed transform 与文档滚动尺寸（沉浸页是否真的完全移出视口）；
  4. 底边带内所有元素的背景 / 阴影 / 圆角（黑边是不是某个元素的 `box-shadow` 或渐变）。

为什么单独立这个脚本
--------------------
缺陷只在「进过一次歌词页再退出」之后出现，静态读 CSS 只能看到 `transform: translateY(102%)`
这类声明，判断不了退出后的实际合成结果；用真实 GPU 合成后的截图与几何数据才能定性。

运行
----
    venv\\Scripts\\python.exe tools\\debug\\debug_media_player_lyrics_exit.py

环境变量：
    CUBBY_HEADLESS=1                             用无头 Chrome（默认 0）
    CUBBY_CHROME_BINARY / CUBBY_CHROMEDRIVER   指定 Chrome / chromedriver 路径
    CUBBY_CHROME_SIZE                            窗口尺寸，形如 1184x749（默认 1400,900）

产物：截图与 JSON 快照写在 `.build/lyrics-exit/`（已忽略，不进版本库）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_media_player_browser_e2e import (  # noqa: E402
    STARTUP_TIMEOUT,
    STUB_SCRIPT,
    _chrome_binary,
    _chromedriver_path,
    _free_port,
    _wait_health,
)

PLUGIN_ENTRY = '/plugins/media-player/frontend/index.html'
OUT_DIR = PROJECT_ROOT / '.build' / 'lyrics-exit'

# 注入的歌词：沉浸页必须真的渲染出歌词行，否则测的是"暂无歌词"空态的合成结果
LYRICS_PATCH = r"""
(function () {
  var lines = ['第一句歌词', '第二句歌词', '第三句歌词', '第四句歌词', '第五句歌词',
               '第六句歌词', '第七句歌词', '第八句歌词', '第九句歌词', '第十句歌词'];
  var lrc = lines.map(function (t, i) {
    var sec = i * 3;
    return '[00:' + String(sec).padStart(2, '0') + '.00]' + t;
  }).join('\n');
  window.pywebview.api['media-player__media_get_lyrics'] = function () {
    return Promise.resolve({ lyrics: lrc, source: 'lrc' });
  };
})();
"""

# 现场快照：几何 + 底边命中 + 底边带内元素
PROBE_SCRIPT = r"""
const h = innerHeight, w = innerWidth;
const page = document.getElementById('lyrics-page');
const cs = page ? getComputedStyle(page) : null;
const pr = page ? page.getBoundingClientRect() : null;

const hits = [1, 2, 4, 8, 16, 32].map(function (d) {
  const y = h - d;
  const el = document.elementFromPoint(w / 2, y);
  if (!el) return { d: d, hit: '（空）' };
  const cls = typeof el.className === 'string' && el.className.trim()
    ? '.' + el.className.trim().split(/\s+/).join('.') : '';
  return { d: d, hit: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + cls };
});

function describe(el) {
  const r = el.getBoundingClientRect();
  const s = getComputedStyle(el);
  const cls = typeof el.className === 'string' && el.className.trim()
    ? '.' + el.className.trim().split(/\s+/).join('.') : '';
  return {
    tag: el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + cls,
    rect: [Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)],
    bg: s.backgroundColor,
    bgImage: s.backgroundImage === 'none' ? '' : s.backgroundImage.slice(0, 90),
    shadow: s.boxShadow === 'none' ? '' : s.boxShadow.slice(0, 90),
    position: s.position,
    zIndex: s.zIndex,
    opacity: s.opacity,
    transform: s.transform === 'none' ? '' : s.transform,
    filter: s.filter === 'none' ? '' : s.filter.slice(0, 60),
  };
}

// 底边带 [h-40, h+40] 内、且在视口里露出来的元素：黑边只可能由这些元素的背景/阴影画出来
const band = [];
document.querySelectorAll('body *').forEach(function (el) {
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return;
  if (r.bottom < h - 40 || r.top > h + 40) return;
  band.push(describe(el));
});

const out = {
  viewport: [w, h],
  lyricsPage: page ? {
    rect: [Math.round(pr.left), Math.round(pr.top), Math.round(pr.right), Math.round(pr.bottom)],
    transform: cs.transform,
    transition: cs.transition,
    background: cs.backgroundColor,
    active: page.classList.contains('active'),
    visiblePixels: Math.max(0, Math.min(pr.bottom, h) - Math.max(pr.top, 0)),
  } : null,
  doc: {
    scrollY: window.scrollY,
    innerH: h,
    docClientH: document.documentElement.clientHeight,
    docScrollH: document.documentElement.scrollHeight,
    bodyScrollH: document.body.scrollHeight,
    docClientW: document.documentElement.clientWidth,
    docScrollW: document.documentElement.scrollWidth,
    docOverflowY: getComputedStyle(document.documentElement).overflowY,
    bodyOverflowY: getComputedStyle(document.body).overflowY,
  },
  appRect: describe(document.getElementById('app')),
  stage: describe(document.getElementById('mp-stage')),
  playerBar: describe(document.getElementById('player-bar')),
  hits: hits,
  band: band,
};
return out;
"""

# 底部像素剖面：逐行给出均值与最暗值，退出前后对比
PIXEL_ROWS = 24


def _log(message: str = '') -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(message.encode('ascii', 'backslashreplace').decode('ascii'), flush=True)


def _window_size() -> tuple:
    raw = (os.environ.get('CUBBY_CHROME_SIZE') or '').strip()
    width, _, height = raw.partition('x')
    if width.isdigit() and height.isdigit():
        return int(width), int(height)
    return 1400, 900


def _bottom_profile(png_path: Path) -> list:
    """逐行给出底部像素的均值与最暗通道（离底 1px 起向上 24 行）。

    `Image.getpixel` 在类型上返回「单通道数值或元组」两种形态；本脚本只处理 RGB 图像，
    取到非元组的像素直接丢掉，省去每处下标都要断言一次。
    """
    from PIL import Image  # pyright: ignore[reportMissingImports]

    with Image.open(png_path) as img:
        rgb = img.convert('RGB')
        width, height = rgb.size
        rows = []
        for depth in range(1, PIXEL_ROWS + 1):
            y = height - depth
            raw = [rgb.getpixel((x, y)) for x in range(0, width, max(1, width // 60))]
            line = [px for px in raw if isinstance(px, tuple) and len(px) >= 3]
            mean = tuple(round(sum(px[i] for px in line) / len(line), 1) for i in range(3))
            darkest = min(sum(px) // 3 for px in line)
            rows.append({'y': y, 'mean': mean, 'darkest': darkest})
        return rows


def _probe(driver: Any) -> dict:
    return driver.execute_script(PROBE_SCRIPT)


def _shot(driver: Any, name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f'{name}.png'
    path.write_bytes(driver.get_screenshot_as_png())
    return path


def _click_js(driver: Any, selector: str) -> None:
    driver.execute_script(
        "const el = document.querySelector(arguments[0]); if (el) el.click();", selector)


def _wait_app(driver: Any, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if driver.execute_script("return !!(window.mediaPlayerApp && mediaPlayerApp.core);"):
            return True
        time.sleep(0.2)
    return False


def _wait_playing(driver: Any, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = driver.execute_script(
            "const c = mediaPlayerApp.core; const el = c.mediaElement;"
            "return {title: c.currentItem ? c.currentItem.title : null,"
            " ct: el ? +(el.currentTime || 0).toFixed(2) : 0,"
            " paused: el ? el.paused : null,"
            " err: el && el.error ? el.error.code : null};")
        if state.get('title') and (state.get('ct') or 0) > 0.15:
            return True
        time.sleep(0.2)
    _log(f'  未进入播放状态：{driver.execute_script("return 1;")}')
    return False


def main() -> int:
    chrome = _chrome_binary()
    if chrome is None:
        _log('未检测到 Chrome：可用 CUBBY_CHROME_BINARY 指定可执行文件路径')
        return 2
    try:
        import selenium  # noqa: F401  # pyright: ignore[reportMissingImports]
    except Exception:
        _log('未安装 selenium：先执行 venv\\Scripts\\pip install -r requirements-e2e.txt')
        return 2

    from selenium import webdriver  # pyright: ignore[reportMissingImports]
    from selenium.webdriver.chrome.options import Options  # pyright: ignore[reportMissingImports]
    from selenium.webdriver.chrome.service import Service  # pyright: ignore[reportMissingImports]

    headless = os.environ.get('CUBBY_HEADLESS', '0') not in ('', '0', 'false', 'False')
    width, height = _window_size()
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
    if chrome:
        options.binary_location = chrome
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-gpu')
    options.add_argument('--mute-audio')
    options.add_argument('--autoplay-policy=no-user-gesture-required')
    options.add_argument(f'--window-size={width},{height}')
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

    snapshots: dict[str, dict] = {}
    try:
        driver.get(f'{base}{PLUGIN_ENTRY}?lyrics-exit=1')
        if not _wait_app(driver):
            _log('插件页未初始化完成')
            return 2
        driver.execute_script(LYRICS_PATCH)

        # 走真实路径播放：切到「全部音乐」→ 点第 1 行
        driver.execute_script("mediaPlayerApp.switchView('all-audio');")
        deadline = time.time() + 20
        while time.time() < deadline:
            if driver.execute_script(
                    "return document.querySelectorAll('#media-content .mp-row').length > 0;"):
                break
            time.sleep(0.2)
        driver.execute_script(
            "const row = document.querySelector('#media-content .mp-row'); if (row) row.click();")
        if not _wait_playing(driver):
            _log('未进入播放状态，后续测量无意义')
            return 2
        driver.execute_script(
            "const item = mediaPlayerApp.core.currentItem;"
            "mediaPlayerApp.lyrics.loadForItem(item);")
        time.sleep(0.5)

        # ---- 1. 基线（未进歌词页） ----
        base_shot = _shot(driver, '1-before')
        snapshots['before'] = _probe(driver)

        # ---- 2. 打开歌词沉浸页 ----
        _click_js(driver, '#btn-fullscreen')
        time.sleep(0.9)
        open_shot = _shot(driver, '2-open')
        snapshots['open'] = _probe(driver)

        # ---- 3. 退出（返回按钮），过渡中与稳定后各取一次 ----
        _click_js(driver, '#btn-lyrics-back')
        time.sleep(0.25)
        _shot(driver, '3-exit-mid')
        snapshots['exit_mid'] = _probe(driver)
        time.sleep(0.9)
        after_shot = _shot(driver, '4-after')
        snapshots['after'] = _probe(driver)
        time.sleep(2.0)
        late_shot = _shot(driver, '5-after-3s')
        snapshots['after_3s'] = _probe(driver)

        # ---- 4. 键盘 Esc 退出的对照（同一状态再验证一条退出路径） ----
        _click_js(driver, '#btn-fullscreen')
        time.sleep(0.9)
        driver.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));")
        time.sleep(1.5)
        esc_shot = _shot(driver, '6-after-escape')
        snapshots['after_escape'] = _probe(driver)

        profiles = {
            'before': _bottom_profile(base_shot),
            'open': _bottom_profile(open_shot),
            'after': _bottom_profile(after_shot),
            'after_3s': _bottom_profile(late_shot),
            'after_escape': _bottom_profile(esc_shot),
        }

        # ---- 5. 假设检验：逐层关掉候选绘制者，看黑带由谁画出来 ----
        # 结论若是「隐藏的歌词页不裁剪自身的溢出子元素」，那么只加 overflow:hidden
        # 就应该让底边恢复底色；分别关掉背景层 / 极光层还能看出各自占比。
        for name, css in (
            ('overflow-hidden', '.mp-lyrics-page { overflow: hidden !important; }'),
            ('bg-hidden', '.mp-lyrics-bg { display: none !important; }'),
            ('aurora-hidden', '.mp-lyrics-aurora { display: none !important; }'),
            ('page-visibility', '.mp-lyrics-page { visibility: hidden !important; }'),
        ):
            driver.execute_script(
                "let el = document.getElementById('diag-css');"
                "if (!el) { el = document.createElement('style'); el.id = 'diag-css';"
                "document.head.appendChild(el); }"
                "el.textContent = arguments[0];", css)
            time.sleep(0.4)
            profiles[f'hyp_{name}'] = _bottom_profile(_shot(driver, f'7-hyp-{name}'))
            driver.execute_script(
                "const el = document.getElementById('diag-css'); if (el) el.textContent = '';")
            time.sleep(0.3)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / 'snapshots.json').write_text(
            json.dumps({'snapshots': snapshots, 'profiles': profiles},
                       ensure_ascii=False, indent=2), encoding='utf-8')

        _log(f'截图与快照：{OUT_DIR}')
        _log('')
        _log('=== 底部像素剖面（每行：视口纵坐标 y / 均值 RGB / 最暗通道）===')
        header = f'{"y(退出前)":>10} {"均值":>18} | {"y(退出后)":>10} {"均值":>18} | {"差":>6}'
        _log(header)
        for before, after in zip(profiles['before'], profiles['after'], strict=False):
            diff = sum(abs(before['mean'][i] - after['mean'][i]) for i in range(3)) / 3
            _log(f'{before["y"]:>10} {before["mean"]!s:>18} | '
                 f'{after["y"]:>10} {after["mean"]!s:>18} | {diff:>6.1f}')

        _log('')
        _log('=== 逐层关掉候选绘制者后的底边亮度（离底 1 / 8 / 24 px 的均值 RGB）===')
        for raw_name in ('before', 'after', 'after_3s', 'after_escape',
                         'hyp_overflow-hidden', 'hyp_bg-hidden', 'hyp_aurora-hidden',
                         'hyp_page-visibility'):
            rows = profiles[raw_name]
            _log(f'  {raw_name:<20} 离底 1px={rows[0]["mean"]}'
                 f'  离底 8px={rows[7]["mean"]}  离底 24px={rows[23]["mean"]}')

        for name in ('before', 'open', 'after', 'after_3s', 'after_escape'):
            snap = snapshots[name]
            page_info = snap.get('lyricsPage') or {}
            _log('')
            _log(f'--- 状态 {name} ---')
            _log(f'  视口 {snap["viewport"]}；#lyrics-page 矩形 {page_info.get("rect")}；'
                 f'transform={page_info.get("transform")}；可见高度 {page_info.get("visiblePixels")}px')
            _log(f'  文档滚动：scrollY={snap["doc"]["scrollY"]}，'
                 f'innerH={snap["doc"]["innerH"]}，docScrollH={snap["doc"]["docScrollH"]}，'
                 f'bodyScrollH={snap["doc"]["bodyScrollH"]}，'
                 f'innerW={snap["viewport"][0]}，docScrollW={snap["doc"]["docScrollW"]}')
            _log('  底边命中：' + '，'.join(
                f'离底 {h["d"]}px→{h["hit"]}' for h in snap['hits']))
            _log('  底边带内的元素：')
            for item in snap['band']:
                _log(f'    {item["tag"]} rect={item["rect"]} bg={item["bg"]}'
                     f'{" img=" + item["bgImage"] if item["bgImage"] else ""}'
                     f'{" shadow=" + item["shadow"] if item["shadow"] else ""}'
                     f' pos={item["position"]} z={item["zIndex"]}'
                     f'{" transform=" + item["transform"] if item["transform"] else ""}'
                     f'{" filter=" + item["filter"] if item["filter"] else ""}')

        doc = snapshots['after']['doc']
        base_doc = snapshots['before']['doc']
        _log('')
        _log('=== 结论线索 ===')
        page_visible = (snapshots['after'].get('lyricsPage') or {}).get('visiblePixels')
        _log(f'  退出后 #lyrics-page 仍露出的高度：{page_visible}px')
        _log(f'  退出前 docScrollH={base_doc["docScrollH"]} / 退出后 {doc["docScrollH"]}'
             f'（innerH={doc["innerH"]}）')
        _log(f'  退出后底边命中元素（离底 1px）：{snapshots["after"]["hits"][0]["hit"]}')
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
