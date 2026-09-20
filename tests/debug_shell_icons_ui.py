"""壳图标集的**真渲染**自检（本地手动运行，不进 CI）。

为什么必需
----------
图标的失效形态全都是"不报错、只是画不出来"，静态检查与普通 DOM 断言都看不见：

- 只写 `viewBox` 不给宽高 → `<svg>` 按默认 300×150 撑开整行；
- `<symbol>` 上漏写 `stroke="currentColor"` → 图标恒为黑色，不跟随主题；
- **`<use>` 引用外部文件 → 在这个应用真正使用的内核（pywebview / WebView2）里根本不渲染**，
  而 Chrome 会渲染它。于是"Chrome 里一切正常、窗口里一片空白、DOM 与控制台都干净"。
  这一条只能靠真实渲染 + 读取图形包围盒（`getBBox()`）来发现：
  `<svg>` 的 `getBoundingClientRect()` 会被 CSS 撑成 18×18（"有位置"是假象），
  而 `getBBox()` 会诚实地给出 0。

用法（需要本机有 Chrome，约 15 秒）
----------------------------------
    venv/Scripts/python.exe tests/debug_shell_icons_ui.py

注意：本用例用 Chrome 跑。**Chrome 通过不代表窗口通过** —— 外部引用的那条缺陷恰恰
Chrome 测不出来，所以脚本会显式断言"引用必须是同文档的 `#名字`"与"图形包围盒非零"，
而不是只断言"图标存在且尺寸对"。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 侧栏：每个可见插件一个图标 + 「设置」一个；尺寸由 .icon 的 18px 决定
PROBE_SHELL = r"""
return (function () {
  var out = {};
  var svgs = Array.from(document.querySelectorAll('.nav-sidebar .nav-item svg.obx-icon'));
  out.navIconCount = svgs.length;
  out.navItemCount = document.querySelectorAll('.nav-sidebar .nav-item').length;
  out.textFallbacks = Array.from(document.querySelectorAll('.nav-sidebar .obx-icon-text'))
    .map(function (e) { return e.textContent; });
  out.icons = svgs.map(function (s) {
    var r = s.getBoundingClientRect();
    var use = s.querySelector('use');
    var href = use ? (use.getAttribute('href') || '') : '';
    // 真正画出来没有：<svg> 的宽高会被 CSS 撑开（那只是"有位置"），
    // 图形本体在不在要看 getBBox()。外部文件的 <use> 会让它恒为 0。
    var drawn = 0;
    try { drawn = Math.round(s.getBBox().width); } catch (e) { drawn = -1; }
    return { id: href.replace(/^#/, ''), href: href,
             w: Math.round(r.width), h: Math.round(r.height), drawn: drawn,
             color: getComputedStyle(s).color };
  });
  // sprite 必须**内联在文档里**：引用外部文件在 WebView2 里不渲染（见 tools/build_icons.py）
  var container = document.getElementById('obx-icons');
  out.spriteInlined = !!container;
  out.symbolCount = container ? container.querySelectorAll('symbol').length : 0;
  out.hasCurrentColor = container
    ? container.innerHTML.indexOf('stroke="currentColor"') >= 0 : false;
  out.foreignHrefs = out.icons.filter(function (i) { return i.href.indexOf('icons.svg') >= 0; }).length;
  // viewBox 只写不写宽高时会变成 300×150；这里同时排除 0 尺寸与异常大尺寸
  out.allSized = svgs.every(function (s) {
    var r = s.getBoundingClientRect();
    return r.width > 6 && r.width < 40 && Math.abs(r.width - r.height) < 1;
  });
  // 最要紧的一条：图形本体非空
  out.allDrawn = out.icons.every(function (i) { return i.drawn > 4; });
  return out;
})();
"""

# 设置页：左栏 6 个分区图标 + 标题栏 1 个 + 主题开关 2 个
PROBE_SETTINGS = r"""
return (function () {
  var out = {};
  function ids(list) {
    return list.map(function (s) {
      var u = s.querySelector('use');
      return u ? (u.getAttribute('href') || '').split('#')[1] : '';
    });
  }
  var nav = Array.from(document.querySelectorAll('.settings-nav-item svg.obx-icon'));
  var panel = Array.from(document.querySelectorAll('.settings-panel-header svg.obx-icon'));
  var toggle = Array.from(document.querySelectorAll('.theme-toggle svg.obx-icon'));
  out.navIds = ids(nav);
  out.panelIds = ids(panel);
  out.toggleIds = ids(toggle);
  var all = nav.concat(panel, toggle);
  out.allSized = all.every(function (s) {
    var r = s.getBoundingClientRect();
    return r.width > 6 && r.width < 40 && Math.abs(r.width - r.height) < 1;
  });
  out.emptyIds = out.navIds.concat(out.panelIds, out.toggleIds).filter(function (v) { return !v; });
  // 取色继承：选中导航项的文字色与它的图标色必须一致（都是 currentColor）
  var active = document.querySelector('.settings-nav-item.active');
  if (active) {
    var icon = active.querySelector('svg.obx-icon');
    out.activeTextColor = getComputedStyle(active).color;
    out.activeIconColor = icon ? getComputedStyle(icon).color : null;
  }
  return out;
})();
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def main() -> int:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    port = free_port()
    base = f'http://127.0.0.1:{port}'
    server = subprocess.Popen(
        [sys.executable, 'main.py', '--web-only', '--port', str(port)],
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    try:
        ready = False
        for _ in range(200):
            try:
                with urllib.request.urlopen(f'{base}/health', timeout=0.5) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.2)
        if not ready:
            print('服务未在超时内就绪')
            return 1

        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1400,900')
        options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
        driver = webdriver.Chrome(options=options)
        try:
            driver.set_page_load_timeout(30)

            driver.get(base)
            time.sleep(3)
            shell = driver.execute_script(PROBE_SHELL)
            print('侧栏：', json.dumps(shell, ensure_ascii=False))
            if not shell.get('spriteInlined'):
                failures.append('文档里没有内联的 sprite 容器 #obx-icons：图标会全空')
            if not shell.get('hasCurrentColor'):
                failures.append('sprite 的 <symbol> 上没有 stroke="currentColor"，图标不会跟随主题')
            if shell.get('foreignHrefs'):
                failures.append(
                    f"有 {shell.get('foreignHrefs')} 个 <use> 仍引用外部文件："
                    f"该形态在 pywebview 的 WebView2 里不渲染（必须用 #同文档 引用）"
                )
            # 这一条是本次缺陷的直接守卫：图形本体必须有非零包围盒
            if not shell.get('allDrawn'):
                failures.append(
                    f"图标没有画出图形本体（getBBox 为 0）："
                    f"{[i for i in shell.get('icons', []) if i.get('drawn', 0) <= 4]}"
                )
            if not shell.get('allSized'):
                failures.append(f"侧栏图标尺寸异常（应为 18×18）：{shell.get('icons')}")
            if shell.get('textFallbacks'):
                failures.append(f"仍有走文本渲染的 manifest 图标：{shell.get('textFallbacks')}")
            if shell.get('navIconCount') != shell.get('navItemCount'):
                failures.append(
                    f"导航项与图标数量不一致：{shell.get('navItemCount')} 项 / {shell.get('navIconCount')} 图标"
                )

            driver.get(base + '/settings')
            time.sleep(3)
            settings = driver.execute_script(PROBE_SETTINGS)
            print('设置页：', json.dumps(settings, ensure_ascii=False))
            if not settings.get('allSized'):
                failures.append('设置页图标尺寸异常（应为 1em，随字号）')
            if settings.get('emptyIds'):
                failures.append(f"有 <use> 没解析出图标名：{settings.get('emptyIds')}")
            if len(settings.get('navIds') or []) != 6:
                failures.append(f"设置页左栏图标应为 6 个：{settings.get('navIds')}")
            if settings.get('activeTextColor') != settings.get('activeIconColor'):
                failures.append(
                    f"选中项图标没有继承文字颜色：文字 {settings.get('activeTextColor')} / "
                    f"图标 {settings.get('activeIconColor')}"
                )

            logs = [e for e in driver.get_log('browser') if e.get('level') in ('SEVERE', 'ERROR')]
            if logs:
                failures.append(f"控制台报错：{[e.get('message', '')[:200] for e in logs]}")
        finally:
            driver.quit()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    if failures:
        print('\n不合格：')
        for item in failures:
            print(f'  - {item}')
        return 1
    print('\n图标自检通过：sprite 可取、symbol 跟随 currentColor、尺寸与取色均正确。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
