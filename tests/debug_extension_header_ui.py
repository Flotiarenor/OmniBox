"""检查 image-viewer 扩展面板头部与主工具栏、以及「内嵌插件挂上来的按钮」的视觉参数。

背景（三轮，都是用户实测反馈）：
1. "渲染的上边栏宽度/字体大小不一样"——宿主扩展头原来是给 `14px/600` 标题 + 一个
   `.btn.btn-sm`（12px/3px）设计的薄条，把插件工具栏的按钮（13px）挂上来之后两者不同族。
2. "两个附属插件的上边栏比图片浏览器略低"——扩展头只有一行标题（实测 45px），
   主工具栏的标题行是「15px 主标题 + 11px 说明」两行（48px），进出扩展视图会矮一档。
3. "按钮部分也是不一样的 css，包括间隙、高度"——挂上来的是 `.btn.btn-sm`（12px/3px，
   28px 高），主工具栏那一排是 `.btn`（13px/6px 14px），同一个面板里两套按钮。

本脚本用无头浏览器读真实计算值，逐项与主工具栏的 `.btn` 比对：头部高度、标题行字号、
按钮的字号 / 内边距 / 高度 / 渲染方式 / 图标尺寸 / 图标与文字间距 / 按钮间隙。
头部不再自带「返回相册」，因此头部里的按钮必须全部是插件挂上来的（`.obx-host-action`）。

用法：venv/Scripts/python.exe tests/debug_extension_header_ui.py
缺少 selenium 或 Chrome 时跳过（退出码 0）；Chrome 定位统一走
`tests/harness/browser_binary.py`（本机只有 Edge 时不能喂给 `webdriver.Chrome`）。
"""
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 主工具栏与扩展头并存的最小页面：扩展头各项的对照组就是上面那条 `.view-toolbar`
PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<link rel="stylesheet" href="/shell/variables.css">
<link rel="stylesheet" href="/shell/base.css">
<link rel="stylesheet" href="__IV_CSS__">
</head><body>
<div id="app">
  <div class="view-body">
    <div class="view-toolbar iv-toolbar" id="main-toolbar">
      <div class="toolbar-group iv-view-heading">
        <div>
          <div class="iv-view-title" id="iv-view-title">最近添加</div>
          <div class="iv-view-sub" id="iv-view-sub">最新更新的相册排在最前</div>
        </div>
      </div>
      <div class="toolbar-group" id="main-actions" style="margin-left:auto;">
        <button class="btn" id="main-refresh"><svg class="obx-icon"><use href="#refresh-cw"></use></svg> 刷新</button>
        <button class="btn" id="main-rebuild"><svg class="obx-icon"><use href="#brush-cleaning"></use></svg> 全量重建</button>
        <button class="btn" id="main-settings"><svg class="obx-icon"><use href="#settings"></use></svg> 设置</button>
      </div>
    </div>
    <div class="view-content iv-content" id="iv-content"></div>
    <div id="extension-view" class="extension-view">
      <div class="extension-view-header" id="extension-header">
        <div class="extension-view-heading">
          <div class="iv-view-title" id="extension-view-title">相册清理</div>
          <div class="iv-view-sub" id="extension-view-sub">扫描全部相册中的重复 / 相似图片</div>
        </div>
        <div class="extension-view-actions" id="extension-view-actions"></div>
      </div>
      <div class="extension-view-body"></div>
    </div>
  </div>
</div>
<script>
// 模拟 HostChannel 挂上来的两个按钮：类名与 innerHTML 与 base.js 的 mountButtons 一致，
// 按钮定义与 image-cleaner 声明的一致（icon:refresh-cw / icon:settings）
setTimeout(function () {
  function iconTextGap(el, svg) {
    // 图标右缘到标签文字左缘的距离：主工具栏那种 inline 写法靠一个空格，
    // 挂上来的按钮若另写 flex + gap，这里就会对不上
    if (!svg) return null;
    var node = null;
    for (var i = 0; i < el.childNodes.length; i++) {
      var n = el.childNodes[i];
      if (n.nodeType === 3 && n.textContent.trim()) { node = n; break; }
    }
    if (!node) return null;
    // 从第一个非空白字符量起：inline 写法里图标后面那个空格也在文本节点里，
    // 直接量整个节点会把空格算进间距（两种写法都变成 0，看不出差别）
    var start = node.textContent.search(/\\S/);
    var range = document.createRange();
    range.setStart(node, start < 0 ? 0 : start);
    range.setEnd(node, start < 0 ? node.textContent.length : start + 1);
    return Math.round((range.getBoundingClientRect().left - svg.getBoundingClientRect().right) * 10) / 10;
  }
  function cs(el) {
    var s = getComputedStyle(el);
    var svg = el.querySelector('svg');
    var ir = svg ? svg.getBoundingClientRect() : null;
    return {
      fontSize: s.fontSize,
      display: s.display,
      height: Math.round(el.getBoundingClientRect().height * 10) / 10,
      padding: s.paddingTop + ' ' + s.paddingRight,
      icon: ir ? [Math.round(ir.width * 10) / 10, Math.round(ir.height * 10) / 10] : null,
      iconTextGap: iconTextGap(el, svg),
    };
  }
  function boxGap(container) {
    var kids = container.querySelectorAll('.btn');
    if (kids.length < 2) return null;
    return Math.round((kids[1].getBoundingClientRect().left
      - kids[0].getBoundingClientRect().right) * 10) / 10;
  }
  function boxHeight(container) {
    var kids = container.querySelectorAll('.btn');
    return kids.length ? Math.round(kids[0].getBoundingClientRect().height * 10) / 10 : null;
  }

  var box = document.getElementById('extension-view-actions');
  [['refresh-cw', '重新扫描'], ['settings', '设置']].forEach(function (spec) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-sm obx-host-action';
    b.innerHTML = '<svg class="obx-icon" aria-hidden="true"><use href="#' + spec[0] + '"></use></svg> '
      + spec[1];
    box.appendChild(b);
  });

  var header = document.getElementById('extension-header');
  var toolbar = document.getElementById('main-toolbar');
  var mainActions = document.getElementById('main-actions');
  var mounted = box.querySelectorAll('button');
  window.__m = {
    toolbarH: Math.round(toolbar.getBoundingClientRect().height),
    headerH: Math.round(header.getBoundingClientRect().height),
    // 两者在同一列里上下叠着：扩展头上边缘应当与主工具栏上边缘齐平
    headerTop: Math.round(header.getBoundingClientRect().top - toolbar.getBoundingClientRect().top),
    title: cs(document.getElementById('extension-view-title')),
    sub: cs(document.getElementById('extension-view-sub')),
    subText: document.getElementById('extension-view-sub').textContent.trim(),
    mainTitle: cs(document.getElementById('iv-view-title')),
    mainSub: cs(document.getElementById('iv-view-sub')),
    // 对照组：主工具栏里带图标的「刷新」，以及同组相邻两个按钮的实际间隙
    mainBtn: cs(document.getElementById('main-refresh')),
    mainGap: boxGap(mainActions),
    mainBtnH: boxHeight(mainActions),
    mountedBtn: cs(mounted[0]),
    mountedGap: boxGap(box),
    mountedBtnH: boxHeight(box),
    mountedCount: mounted.length,
    hostOwnButtons: document.querySelectorAll('.extension-view-header .btn:not(.obx-host-action)').length,
    wrapRight: Math.round(header.getBoundingClientRect().right - header.lastElementChild.getBoundingClientRect().right),
    headerWrapped: header.getBoundingClientRect().height > 60,
  };
}, 60);
</script>
</body></html>
"""


def main() -> int:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        print('未安装 selenium，跳过')
        return 0

    from tests.harness.browser_binary import browser_binary

    binary = browser_binary()
    if binary is None:
        print('未检测到 Chrome，跳过')
        return 0

    tmp = Path(tempfile.mkdtemp(prefix='ext_header_'))
    page = tmp / 'index.html'

    # 用 file:// 打开时 /shell/* 与 /plugins/* 取不到，改成注入绝对路径的 file URL
    html = PAGE.replace('href="/shell/variables.css"',
                        f'href="{(PROJECT_ROOT / "shell/frontend/public/shell/variables.css").as_uri()}"')
    html = html.replace('href="/shell/base.css"',
                        f'href="{(PROJECT_ROOT / "shell/frontend/public/shell/base.css").as_uri()}"')
    html = html.replace('href="__IV_CSS__"',
                        f'href="{(PROJECT_ROOT / "plugins/image-viewer/frontend/image-viewer.css").as_uri()}"')
    page.write_text(html, encoding='utf-8')

    options = Options()
    options.binary_location = binary
    for arg in ('--headless=new', '--disable-gpu', '--no-sandbox', '--window-size=1280,860'):
        options.add_argument(arg)
    try:
        driver = webdriver.Chrome(options=options)
    except Exception as exc:   # 驱动下载失败 / 浏览器不匹配一样算环境不具备
        print(f'无法启动 WebDriver，跳过：{type(exc).__name__}: {exc}')
        return 0
    failures = []
    try:
        driver.get(page.as_uri())
        import time
        time.sleep(0.4)
        m = driver.execute_script('return window.__m;')
        print(json.dumps(m, ensure_ascii=False, indent=2))

        def check(name, ok, detail=''):
            print(f'  {"PASS" if ok else "FAIL"}  {name}{(" — " + detail) if detail else ""}')
            if not ok:
                failures.append(name)

        # ---- 头部整体 ----
        check('扩展头与主工具栏逐像素同高',
              m['headerH'] == m['toolbarH'], f"header={m['headerH']} toolbar={m['toolbarH']}")
        check('两者都是壳的工具栏高度 48px',
              m['headerH'] == 48 and m['toolbarH'] == 48, f"{m['headerH']} / {m['toolbarH']}")
        check('扩展头上边缘与主工具栏上边缘齐平',
              m['headerTop'] == 0, str(m['headerTop']))
        check('头部没有换行（高度未被撑到两行）',
              not m['headerWrapped'], str(m['headerH']))
        check('右侧整组贴边（与头部右内边距齐平）',
              0 <= m['wrapRight'] <= 20, str(m['wrapRight']))
        check('头部里没有宿主自带的按钮（「返回相册」已去掉）',
              m['hostOwnButtons'] == 0, f"hostOwn={m['hostOwnButtons']}")

        # ---- 标题行 ----
        check('扩展头主标题字号与主工具栏一致（15px）',
              m['title']['fontSize'] == m['mainTitle']['fontSize'] == '15px',
              f"header={m['title']['fontSize']} toolbar={m['mainTitle']['fontSize']}")
        check('扩展头副标题字号与主工具栏一致（11px）',
              m['sub']['fontSize'] == m['mainSub']['fontSize'] == '11px',
              f"header={m['sub']['fontSize']} toolbar={m['mainSub']['fontSize']}")
        check('扩展头副标题有内容且占位（不是零高空行）',
              bool(m['subText']) and m['sub']['height'] > 0,
              f"text={m['subText']!r} height={m['sub']['height']}")

        # ---- 挂上来的按钮 vs 主工具栏的 .btn ----
        check('挂上来的按钮与主工具栏按钮同字号',
              m['mountedBtn']['fontSize'] == m['mainBtn']['fontSize'],
              f"mounted={m['mountedBtn']['fontSize']} toolbar={m['mainBtn']['fontSize']}")
        check('挂上来的按钮与主工具栏按钮同高',
              m['mountedBtnH'] == m['mainBtnH'],
              f"mounted={m['mountedBtnH']} toolbar={m['mainBtnH']}")
        check('挂上来的按钮与主工具栏按钮同内边距',
              m['mountedBtn']['padding'] == m['mainBtn']['padding'],
              f"mounted={m['mountedBtn']['padding']} toolbar={m['mainBtn']['padding']}")
        check('挂上来的按钮与主工具栏按钮同渲染方式（不是另一套 flex）',
              m['mountedBtn']['display'] == m['mainBtn']['display'],
              f"mounted={m['mountedBtn']['display']} toolbar={m['mainBtn']['display']}")
        check('图标尺寸一致（都走 .obx-icon 的 1em）',
              m['mountedBtn']['icon'] == m['mainBtn']['icon'],
              f"mounted={m['mountedBtn']['icon']} toolbar={m['mainBtn']['icon']}")
        check('图标与文字的间距一致',
              m['mountedBtn']['iconTextGap'] == m['mainBtn']['iconTextGap'],
              f"mounted={m['mountedBtn']['iconTextGap']} toolbar={m['mainBtn']['iconTextGap']}")
        check('按钮之间的间隙一致（挂载组 vs 主工具栏操作组）',
              m['mountedGap'] == m['mainGap'],
              f"mounted={m['mountedGap']} toolbar={m['mainGap']}")
        check('两个按钮都挂上来了（image-cleaner 声明的重新扫描 / 设置）',
              m['mountedCount'] == 2, str(m['mountedCount']))
    finally:
        driver.quit()
    return 1 if failures else 0


if __name__ == '__main__':   # pragma: no cover
    sys.exit(main())
