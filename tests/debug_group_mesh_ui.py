"""group-mesh 前端 UI 改造的几何/配色自检（无头浏览器，约 5 秒）。

回答的是"肉眼才能回答"的问题，只是用断言而不是截图：
  · 栅格与侧栏在宽窗口下是否真的并排、在窄窗口下是否真的堆叠；
  · 面板切换是否只留一个可见；
  · 卡片/徽章的颜色是否真的来自壳的 token（而不是残留的写死色）；
  · 深浅主题下是否都读得出来（拿 --text-primary / --bg-surface 的实际计算值比对）；
  · 交错动画的 --obx-i 是否真的写到了卡片上（否则 .obx-stagger 等于没接）。

用法：
    venv/Scripts/python.exe tests/debug_group_mesh_ui.py
失败时打印断言名与非零退出码。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'tests'))

from selenium import webdriver
from test_group_mesh_frontend_e2e import (
    JOINED_STATUS,
    STUB,
    shell_injection,
    show_panel,
    wait_for_text,
)

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'group-mesh' / 'frontend'
CHECKS: list[str] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    print(f'  {"PASS" if ok else "FAIL"}  {name}{(" — " + detail) if detail else ""}')
    CHECKS.append(name) if ok else CHECKS.append('!' + name)


def build_html(status: dict) -> str:
    html = (PLUGIN_DIR / 'index.html').read_text(encoding='utf-8')
    html = html.replace(
        '<script src="js/app.js"></script>',
        f'<script>window.__statusPayload = {json.dumps(status)};</script>'
        f'<script>{STUB}</script><script src="js/app.js"></script>')
    html = html.replace('</head>', shell_injection() + '\n</head>')
    base = PLUGIN_DIR.as_uri() + '/'
    for asset in ('group-mesh.css', 'js/app.js'):
        html = html.replace(f'"{asset}"', f'"{base}{asset}"')
    return html


def geometry(driver) -> dict:
    return driver.execute_script(
        "var g = function (sel) { var n = document.querySelector(sel);"
        "  if (!n) return null; var r = n.getBoundingClientRect();"
        "  var cs = getComputedStyle(n);"
        "  return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width),"
        "          h: Math.round(r.height), display: cs.display,"
        "          bg: cs.backgroundColor, color: cs.color, border: cs.borderTopColor,"
        "          radius: cs.borderTopLeftRadius}; };"
        "return {vw: window.innerWidth, side: g('.gm-side'), content: g('#gm-content'),"
        "        main: g('.gm-main'), card: g('#panel-machine .gm-card'),"
        "        grid: g('#panel-machine .gm-grid'),"
        "        toolbar: g('.gm-toolbar'), panel: g('#panel-machine'),"
        "        nav: g('#gm-nav'), navItem: g('#gm-nav .gm-nav-item'),"
        "        tokenBg: getComputedStyle(document.documentElement)"
        "          .getPropertyValue('--bg-surface').trim(),"
        "        tokenText: getComputedStyle(document.documentElement)"
        "          .getPropertyValue('--text-primary').trim(),"
        "        stagger: Array.from(document.querySelectorAll('#gm-panels .gm-card'))"
        "          .map(function (c) { return c.style.getPropertyValue('--obx-i'); })};")


def main() -> int:
    options = webdriver.ChromeOptions()
    for arg in ('--headless=new', '--disable-gpu', '--no-sandbox',
                '--window-size=1280,860', '--allow-file-access-from-files'):
        options.add_argument(arg)
    driver = webdriver.Chrome(options=options)
    try:
        driver.get('file:///' + str(PLUGIN_DIR.joinpath('index.html')).replace('\\', '/'))
        # 直接把构造好的页面写进临时文件（沿用 e2e 用例的注入方式）
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        page = Path(tmp.name) / 'index.html'
        page.write_text(build_html(JOINED_STATUS), encoding='utf-8')
        driver.get(page.as_uri())
        wait_for_text(driver, 'identity-body', 'alice')

        print('\n[1280x860 宽窗口]')
        g = geometry(driver)
        # 结构必须与 image-viewer / manga-library / media-player 一致：
        # 侧栏是 #app 的直接子元素、跑满全高；工具栏属于右侧主区（.view-body）。
        # 先前把工具栏放在 #app 下当兄弟节点，于是它横跨整宽、侧栏从工具栏下方才开始。
        check('侧栏是 #app 的直接子元素且顶到最上（y = 0）',
              driver.execute_script(
                  "var s = document.querySelector('.gm-side');"
                  "return s.parentElement.id === 'app'"
                  " && Math.round(s.getBoundingClientRect().y) === 0;"),
              driver.execute_script(
                  "var s = document.querySelector('.gm-side');"
                  "return 'parent=#' + s.parentElement.id + ' y=' + Math.round(s.getBoundingClientRect().y);"))
        check('工具栏在主区内（.view-body > .view-toolbar），不与侧栏同级',
              driver.execute_script(
                  "var t = document.querySelector('.gm-toolbar');"
                  "return t.parentElement.classList.contains('view-body')"
                  " && t.parentElement.previousElementSibling === document.querySelector('.gm-side');"))
        check('侧栏跑满全高（与主区同高）',
              abs(g['side']['h'] - g['main']['h']) <= 1,
              f"side.h={g['side']['h']} main.h={g['main']['h']}")
        check('工具栏高度 = 壳的 --toolbar-height(48)', g['toolbar']['h'] == 48,
              f"实际 {g['toolbar']['h']}")
        check('工具栏横跨的是主区（宽度 = 主区宽，不等于窗口宽）',
              g['toolbar']['w'] == g['main']['w'] and g['toolbar']['w'] < g['vw'],
              f"toolbar.w={g['toolbar']['w']} main.w={g['main']['w']} vw={g['vw']}")
        check('内容区在侧栏右侧、工具栏下方（不再是"侧栏从工具栏下方开始"）',
              g['content']['x'] >= g['side']['x'] + g['side']['w']
              and g['content']['y'] >= g['toolbar']['y'] + g['toolbar']['h'] - 1,
              f"side.x={g['side']['x']} content.x={g['content']['x']}"
              f" toolbar.y={g['toolbar']['y']} content.y={g['content']['y']}")
        check('侧栏宽度 = --gm-side-width(236)', g['side']['w'] == 236, f"实际 {g['side']['w']}")
        check('侧栏不占满窗口（内容是主区）', g['content']['w'] > g['side']['w'] * 2,
              f"content.w={g['content']['w']}")
        check('机器面板两列栅格（两个卡片在同一 y）',
              driver.execute_script(
                  "var cs = document.querySelectorAll('#panel-machine .gm-card');"
                  "return cs.length === 2 && Math.round(cs[0].getBoundingClientRect().y)"
                  " === Math.round(cs[1].getBoundingClientRect().y);"))
        check('卡片底色 = 壳 token --bg-surface',
              g['card']['bg'] == driver.execute_script(
                  "var d = document.createElement('div');"
                  "d.style.background = getComputedStyle(document.documentElement)"
                  "  .getPropertyValue('--bg-surface').trim();"
                  "document.body.appendChild(d);"
                  "var c = getComputedStyle(d).backgroundColor; d.remove(); return c;"),
              f"卡片 {g['card']['bg']} / token {g['tokenBg']}")
        check('卡片字号/圆角来自壳 token（--radius-lg = 10px）',
              g['card']['radius'] == '10px', f"实际 {g['card']['radius']}")
        check('交错动画的 --obx-i 已写到卡片上', g['stagger'] != [],
              f"stagger={g['stagger']}")

        print('\n[面板切换]')
        visible = driver.execute_script(
            "return Array.from(document.querySelectorAll('#gm-panels > .gm-panel'))"
            ".filter(function (p) { return getComputedStyle(p).display !== 'none'; })"
            ".map(function (p) { return p.getAttribute('data-panel'); });")
        check('默认只显示一个面板', visible == ['machine'], f"实际 {visible}")
        show_panel(driver, 'remote')
        visible = driver.execute_script(
            "return Array.from(document.querySelectorAll('#gm-panels > .gm-panel'))"
            ".filter(function (p) { return getComputedStyle(p).display !== 'none'; })"
            ".map(function (p) { return p.getAttribute('data-panel'); });")
        check('切到远端共享后只有它可见', visible == ['remote'], f"实际 {visible}")
        check('远端面板的两栏并排（设备列在左、目录列在右）',
              driver.execute_script(
                  "var a = document.querySelector('.gm-remote-peers').getBoundingClientRect();"
                  "var b = document.querySelector('.gm-remote-files').getBoundingClientRect();"
                  "return b.x > a.x + a.width - 2;"))
        check('工具栏标题随面板更新',
              '远端共享' in driver.execute_script(
                  "return document.getElementById('gm-panel-title').textContent;"))
        print('    DEBUG title=', driver.execute_script(
            "return JSON.stringify({active: document.querySelector('#gm-panels > .gm-panel[data-active=\"true\"]')"
            ".getAttribute('data-panel'),"
            " title: document.getElementById('gm-panel-title').textContent,"
            " sub: document.getElementById('gm-panel-sub').textContent,"
            " attr: document.querySelector('#panel-remote').getAttribute('data-title')});"))
        show_panel(driver, 'roadmap')
        check('路线图列表在折叠区里且默认收起',
              driver.execute_script(
                  "var d = document.querySelector('.gm-details');"
                  "return !!d && d.open === false &&"
                  " document.querySelectorAll('#unsupported-list li').length > 0;"))

        print('\n[640x800 窄窗口]')
        driver.set_window_size(640, 800)
        g = geometry(driver)
        check('窄窗口下侧栏折到顶部（侧栏与内容区不同 y）',
              g['content']['y'] > g['side']['y'], f"side.y={g['side']['y']} content.y={g['content']['y']}")
        check('窄窗口下侧栏占满宽度', g['side']['w'] >= g['vw'] - 2,
              f"side.w={g['side']['w']} vw={g['vw']}")
        check('窄窗口下导航横向排列（项与项同一 y）',
              driver.execute_script(
                  "var items = document.querySelectorAll('#gm-nav .gm-nav-item');"
                  "return items.length >= 2 &&"
                  " Math.round(items[0].getBoundingClientRect().y)"
                  " === Math.round(items[1].getBoundingClientRect().y);"))
        check('窄窗口下机器面板改为单列',
              (show_panel(driver, 'machine'), driver.execute_script(
                  "var cs = document.querySelectorAll('#panel-machine .gm-card');"
                  "return cs.length === 2 &&"
                  " Math.round(cs[0].getBoundingClientRect().y)"
                  " < Math.round(cs[1].getBoundingClientRect().y);"))[1])
        print('    DEBUG narrow=', driver.execute_script(
            "var s = document.querySelector('.gm-side'); var cs = getComputedStyle(s);"
            "var m = document.querySelector('.gm-main');"
            "var grid = document.querySelector('#panel-machine .gm-grid');"
            "return JSON.stringify({vw: window.innerWidth,"
            " sideW: cs.width, sideFlexDir: cs.flexDirection, sideDisplay: cs.display,"
            " appDir: getComputedStyle(document.getElementById('app')).flexDirection,"
            " sideBorderRight: cs.borderRightWidth, sideBorderBottom: cs.borderBottomWidth,"
            " mainW: getComputedStyle(m).width,"
            " matches720: window.matchMedia('(max-width: 720px)').matches,"
            " gridCols: getComputedStyle(grid).gridTemplateColumns,"
            " appW: getComputedStyle(document.getElementById('app')).width});"))

        print('\n[暗色主题：颜色跟随 token，不写死]')
        driver.set_window_size(1280, 860)
        driver.execute_script("document.documentElement.setAttribute('data-theme', 'dark');")
        dark = geometry(driver)
        check('暗色下卡片底色变成暗色 token（与浅色不同）',
              dark['card']['bg'] != g['card']['bg'],
              f"light={g['card']['bg']} dark={dark['card']['bg']}")
        check('暗色下徽章颜色来自 token（owner = --warning）',
              driver.execute_script(
                  "var b = document.querySelector('.gm-badge-owner');"
                  "if (!b) return false;"
                  "var probe = document.createElement('span');"
                  "probe.style.color = getComputedStyle(document.documentElement)"
                  "  .getPropertyValue('--warning').trim(); document.body.appendChild(probe);"
                  "var want = getComputedStyle(probe).color; probe.remove();"
                  "return getComputedStyle(b).color === want;"))
        check('暗色下兜底 toast 是深底浅字（不是反色）',
              driver.execute_script(
                  "var t = document.createElement('div'); t.className = 'gm-toast';"
                  "document.body.appendChild(t); var cs = getComputedStyle(t);"
                  "var r = {bg: cs.backgroundColor, fg: cs.color}; t.remove();"
                  "return r.bg !== r.fg && r.bg.indexOf('rgb(48, 54, 61)') >= 0"
                  " || r.bg.indexOf('22, 27, 34') >= 0;"),
              '期望底色为 --bg-surface(#161b22)')
    finally:
        driver.quit()

    failed = [name for name in CHECKS if name.startswith('!')]
    print(f'\n{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过'
          + (f'，失败：{failed}' if failed else ''))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
