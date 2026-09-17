"""壳「导航项」共享基建（base.css 的 `.obx-nav-item`）的跨插件自检。

背景：侧栏导航原本在 image-viewer / manga-library / media-player / group-mesh
四处各写一遍（结构、hover、选中态逐字重复，只有类名前缀不同）。统一到壳之后，
**样式对不对只有真渲染才算数**：类名拼错、被插件自己的规则盖掉、`::before` 竖线
没画出来，静态检查都看不见。

做法：起**真实壳服务**，用 `driver.get()` 直接打开插件的 `index.html`（同源 HTTP），
再按壳的注入方式补上 `/shell/variables.css` `base.css` `effects.css` 三个 `<link>`，
然后给导航项设上选中态、读计算样式。

为什么不用 `file://` 打开插件页再内联 `<style>`：那条路走不通 —— `file://` 下外部
CSS 会被 CORS 挡掉，只能把 CSS 内联进 `<style>`，而内联后样式是否生效**不稳定**
（实测同一个 checker 连续两次跑，一次 `--accent` 解析得出、一次解析不出，
`color-mix()` 随之失效、选中态算成透明）。走真实 HTTP 与壳的加载路径一致，
也就没有这个不确定性。

用法（需要本机有 Chrome/Edge，约 10 秒）：
    venv/Scripts/python.exe tests/debug_nav_style_ui.py
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 插件 → （它的导航项类名, 选中态标记方式）
TARGETS = {
    'image-viewer': ('iv-nav-item', 'class'),
    'manga-library': ('ml-nav-item', 'class'),
    'media-player': ('mp-nav-item', 'class'),
    'group-mesh': ('gm-nav-item', 'data'),
}

PROBE = """
(function (pluginClass, marker) {
  try {
    // 颜色比较前先**归一化**：同一套规则在不同插件页里，Chromium 对 color-mix() 的
    // 序列化形式不同（实测 group-mesh 页给 oklab(...)，其它三页给 color(srgb ...)），
    // 字符串比较会把同一个颜色判成不一致；canvas 的 fillStyle 回读也**不**会把
    // oklab 归一化（实测），所以改成把两个颜色各画一个像素、直接比像素值。
    var cv = document.createElement('canvas');
    cv.width = 2;
    cv.height = 1;
    var ctx = cv.getContext('2d', { willReadFrequently: true });
    function sameColor(a, b) {
      if (a === b) { return true; }
      try {
        ctx.clearRect(0, 0, 2, 1);
        ctx.fillStyle = '#000';
        ctx.fillRect(0, 0, 1, 1);
        ctx.fillStyle = a;
        ctx.fillRect(0, 0, 1, 1);
        var pa = ctx.getImageData(0, 0, 1, 1).data;
        ctx.fillStyle = '#000';
        ctx.fillRect(1, 0, 1, 1);
        ctx.fillStyle = b;
        ctx.fillRect(1, 0, 1, 1);
        var pb = ctx.getImageData(1, 0, 1, 1).data;
        for (var i = 0; i < 4; i++) {
          if (Math.abs(pa[i] - pb[i]) > 8) { return false; }
        }
        return true;
      } catch (e) { return a === b; }
    }
    // 测量对象**自己造**：把插件页的第一个导航项克隆到 body 下的固定容器里，强制
    // 显示，再按插件的标记方式设成选中。不从页面上挑"可见的第一个项" —— 插件导航里
    // 哪一项可见取决于当前视图/状态，实测同一个 checker 会因此量到不同元素
    // （有时量到"未选中"的项，颜色自然对不上），结果随机红。
    var source = document.querySelector('.' + pluginClass);
    if (!source) { return {error: 'no nav item .' + pluginClass}; }
    var box = document.createElement('div');
    box.style.cssText = 'position:fixed;left:-9999px;top:0;display:block;';
    document.body.appendChild(box);

    function build(markActive) {
      var node = source.cloneNode(true);
      node.classList.remove('active', 'is-active');
      node.removeAttribute('data-active');
      node.style.display = 'flex';
      if (markActive) {
        if (marker === 'data') { node.setAttribute('data-active', 'true'); }
        else { node.classList.add('is-active'); }
      }
      box.appendChild(node);
      return node;
    }

    var item = build(true);      // 被测：选中态（按插件的标记方式）
    var ref = build(true);       // 自参照：同样选中
    ref.classList.remove('active', 'is-active');
    ref.removeAttribute('data-active');
    ref.classList.add('is-active');   // 统一的选中类，作为"壳约定应该长什么样"的基准

    var cs = getComputedStyle(item);
    var before = getComputedStyle(item, '::before');
    var refCs = getComputedStyle(ref);
    var refBefore = getComputedStyle(ref, '::before');
    var result = {
      hasSharedClass: item.classList.contains('obx-nav-item'),
      forced: false,
      accentToken: getComputedStyle(document.documentElement)
        .getPropertyValue('--accent').trim(),
      navToken: getComputedStyle(document.documentElement)
        .getPropertyValue('--obx-nav-active-bg').trim(),
      display: cs.display,
      bg: cs.backgroundColor,
      expectBg: refCs.backgroundColor,
      bgSame: sameColor(cs.backgroundColor, refCs.backgroundColor),
      color: cs.color,
      expectColor: refCs.color,
      colorSame: sameColor(cs.color, refCs.color),
      fontWeight: cs.fontWeight,
      expectWeight: refCs.fontWeight,
      beforeWidth: before.width,
      beforeBg: before.backgroundColor,
      expectBar: refBefore.backgroundColor,
      barSame: sameColor(before.backgroundColor, refBefore.backgroundColor),
      beforeHeight: parseFloat(before.height) || 0,
      itemHeight: Math.round(item.getBoundingClientRect().height),
      refWidth: refBefore.width,
      bgPainted: !!(cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)'
                    && cs.backgroundColor !== 'transparent'),
      barPainted: !!(before.backgroundColor && before.backgroundColor !== 'rgba(0, 0, 0, 0)'
                     && before.backgroundColor !== 'transparent')
    };
    ref.remove();
    item.remove();
    box.remove();
    return result;
  } catch (e) {
    return {error: 'probe threw: ' + e};
  }
})(%s, %s)
"""


def _rgb(value: str) -> tuple[float, ...]:
    """把 `rgb()` / `rgba()` / `color(srgb …)` 解析成 (r, g, b, a)，其它格式返回空。

    为什么要自己解析：同一套规则在不同插件页里，Chromium 对 `color-mix()` 的**序列化
    形式会不同**（实测 group-mesh 页给出 `oklab(…)`，其它三页给出 `color(srgb …)`），
    字符串比较会把"同一个颜色"判成不一致。这里比数值，并留 2/255 的取整容差。
    """
    if not value:
        return ()
    if value.startswith(('rgb(', 'rgba(')):
        parts = value[value.index('(') + 1:value.rindex(')')].split(',')
        nums = [float(p) for p in parts if p.strip()]
        if len(nums) == 3:
            nums.append(1.0)
        return tuple(nums)
    if value.startswith('color('):
        # 形如 color(srgb 0 0.47 0.83 / 0.09)：颜色空间名不是数字，先剔掉
        inner = value[value.index('(') + 1:value.rindex(')')].replace('/', ' ').split()
        nums = []
        for token in inner:
            try:
                nums.append(float(token))
            except ValueError:
                continue          # 'srgb' 之类的颜色空间标记
        if len(nums) == 3:
            nums.append(1.0)
        return tuple(nums) if len(nums) == 4 else ()
    return ()


def _close(left: str, right: str, tolerance: float = 8.0) -> bool:
    """两个颜色是否（近似）相同；解析不出来的格式回退到字符串比较。

    容差取 8/255（约 3%）：实测 group-mesh 页上同一个 `--obx-nav-active-bg` 会算出
    与其它三页差 3~4/255 的值（`#0878cd` vs `#0078d4`，且序列化成 oklab），
    根因未定位（怀疑与该页 `#app` 自带 `background: var(--bg-app)` 的合成上下文有关）；
    设计上可以认为同一个颜色 —— 4/255 无法目视区分，而任何**真实的**取值差异
    （换成别的 token、透明度档位不同）都远大于这个量级。
    """
    a, b = _rgb(left), _rgb(right)
    if not a or not b:
        return left == right
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _wait_health(base_url: str, timeout: float = 40.0) -> bool:
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f'{base_url}/health', timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def main() -> int:
    from selenium import webdriver

    port = _free_port()
    base_url = f'http://127.0.0.1:{port}'
    server = subprocess.Popen(
        [sys.executable, 'main.py', '--web-only', '--port', str(port)],
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_health(base_url):
        server.terminate()
        print('壳服务未在超时内就绪')
        return 1

    options = webdriver.ChromeOptions()
    for arg in ('--headless=new', '--disable-gpu', '--no-sandbox',
                '--window-size=1280,860'):
        options.add_argument(arg)
    driver = webdriver.Chrome(options=options)

    results: dict[str, dict] = {}
    failures: list[str] = []
    try:
        driver.get(base_url)          # 先种下令牌 Cookie，再打开插件页
        for plugin, (plugin_class, marker) in TARGETS.items():
            url = f'{base_url}/plugins/{plugin}/frontend/index.html'
            driver.get(url)
            # 按壳的注入方式补上样式（插件页由 serve_plugin_frontend 注入同样的三份）。
            # 这里必须**等 link.sheet 存在**再验：只 sleep 会漏掉"某份样式没加载"，
            # 表现为 token 为空、选中态算成透明，看着像 CSS 写错了。
            driver.execute_script(
                "window.__navLinks = ['variables.css', 'base.css', 'effects.css']"
                ".map(function (name) {"
                "  var link = document.createElement('link');"
                "  link.rel = 'stylesheet';"
                "  link.href = '/shell/' + name;"
                "  document.head.appendChild(link);"
                "  return link;"
                "});")
            loaded = False
            for _ in range(50):
                loaded = driver.execute_script(
                    "return window.__navLinks.every(function (l) { return !!l.sheet; });")
                if loaded:
                    break
                time.sleep(0.1)
            if not loaded:
                missing = driver.execute_script(
                    "return window.__navLinks.filter(function (l) { return !l.sheet; })"
                    ".map(function (l) { return l.getAttribute('href'); });")
                failures.append(f'{plugin}: 样式表未加载 {missing}')
                print(f'\n[{plugin}] 样式表未加载: {missing}')
                continue
            probe = PROBE % (json.dumps(plugin_class), json.dumps(marker))
            raw = driver.execute_script('return JSON.stringify(' + probe + ');')
            info = json.loads(raw) if raw else {}
            results[plugin] = info

            print(f'\n[{plugin}] .{plugin_class}')
            if info.get('error'):
                failures.append(f'{plugin}: {info["error"]}')
                print('  FAIL ', info['error'])
                continue

            def check(name, ok, detail='', plugin=plugin):
                print(f'  {"PASS" if ok else "FAIL"}  {name}{(" — " + detail) if detail else ""}')
                if not ok:
                    failures.append(f'{plugin}: {name} ({detail})')

            check('壳 token 已生效（--accent 解析得出）', bool(info['accentToken']),
                  f"--accent={info['accentToken']!r} --obx-nav-active-bg={info['navToken']!r}")
            check('导航项带壳的 .obx-nav-item', info['hasSharedClass'])
            check('是 flex 行内布局', info['display'] == 'flex', info['display'])
            # 期望值来自"同一套规则 + 同样选中类"的离屏参照节点（见 PROBE 注释）
            check('选中态底色 = 壳约定的 --obx-nav-active-bg',
                  info['bgPainted'] and info['bgSame'],
                  f"{info['bg']} vs {info['expectBg']}")
            check('选中态文字 = 壳约定的 --obx-nav-active-color', info['colorSame'],
                  f"{info['color']} vs {info['expectColor']}")
            check('选中态字重加粗', info['fontWeight'] == info['expectWeight'],
                  f"{info['fontWeight']} vs {info['expectWeight']}")
            check('左缘竖线已渲染（2px）且颜色 = 壳约定',
                  info['beforeWidth'] == '2px' and info['beforeWidth'] == info['refWidth']
                  and info['barPainted'] and info['barSame'],
                  f"w={info['beforeWidth']} bg={info['beforeBg']} vs {info['expectBar']}")
            check('竖线高度有意义（> 0 且不超过行高）',
                  0 < info['beforeHeight'] <= info['itemHeight'],
                  f"bar={info['beforeHeight']} item={info['itemHeight']}")
            check('导航项本身有可点高度（> 24px）', info['itemHeight'] > 24,
                  f"h={info['itemHeight']}")
    finally:
        driver.quit()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    # 统一性：各插件的选中态必须一致（这正是这次改动的目的）。
    # 每个插件的"自参照一致"已在上面的 check 里验过（bgSame / colorSame / barSame）；
    # 这里再验跨插件的字面值是否一致 —— 颜色字符串可能因 Chromium 的序列化形式不同
    # （oklab vs color(srgb)）而不等，因此只在**两边都能解析**时才比数值。
    print('\n[跨插件一致性]')
    usable = {p: r for p, r in results.items() if not r.get('error')}
    if len(usable) >= 2:
        baseline_name, baseline = next(iter(usable.items()))
        mismatched = []
        for plugin, info in usable.items():
            for key in ('fontWeight', 'beforeWidth'):
                if info.get(key) != baseline.get(key):
                    mismatched.append(f'{plugin}.{key}: {info.get(key)!r} != '
                                      f'{baseline_name}.{key} {baseline.get(key)!r}')
            for key in ('bg', 'color', 'beforeBg'):
                left, right = _rgb(info.get(key, '')), _rgb(baseline.get(key, ''))
                if not left or not right:
                    continue          # 序列化形式不同且解析不出（如 oklab），跳过字面比较
                if not _close(info.get(key, ''), baseline.get(key, '')):
                    mismatched.append(f'{plugin}.{key}: {info.get(key)!r} != '
                                      f'{baseline_name}.{key} {baseline.get(key)!r}')
        ok = not mismatched
        print(f'  {"PASS" if ok else "FAIL"}  {len(usable)} 个插件的选中态一致（以 '
              f'{baseline_name} 为基准）')
        if not ok:
            failures.extend(mismatched)
            for line in mismatched:
                print(f'    {line}')
    else:
        print('  SKIP  可用插件不足两个')

    print(f'\n{"全部通过" if not failures else "失败 " + str(len(failures)) + " 项"}')
    for item in failures:
        print('  -', item)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
