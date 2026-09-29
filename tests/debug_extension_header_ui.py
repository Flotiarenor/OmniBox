"""检查 image-viewer 扩展面板头部与主工具栏、以及「内嵌插件挂上来的按钮」的视觉参数。

背景（两轮，都是用户实测反馈）：
1. "渲染的上边栏宽度/字体大小不一样"——宿主扩展头原来是给 `14px/600` 标题 + 一个
   `.btn.btn-sm`（12px/3px）设计的薄条，把插件工具栏的按钮（13px）挂上来之后两者不同族。
2. "两个附属插件的上边栏比图片浏览器略低"——扩展头只有一行标题（实测 45px），
   主工具栏的标题行是「15px 主标题 + 11px 说明」两行（48px），进出扩展视图会矮一档。

本脚本用无头浏览器读真实计算值，避免又靠肉眼估：扩展头高度必须与主工具栏相等，
标题行结构与字号与主工具栏一致。

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

# 主工具栏与扩展头并存的最小页面：扩展头的对照组就是上面那条 `.view-toolbar`
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
      <div class="toolbar-group" style="margin-left:auto;">
        <button class="btn" id="iv-refresh">刷新</button>
        <button class="btn" id="iv-settings">设置</button>
      </div>
    </div>
    <div class="view-content iv-content" id="iv-content"></div>
    <div id="extension-view" class="extension-view">
      <div class="extension-view-header" id="extension-header">
        <div class="extension-view-heading">
          <div class="iv-view-title" id="extension-view-title">相册清理</div>
          <div class="iv-view-sub" id="extension-view-sub">扫描全部相册中的重复 / 相似图片</div>
        </div>
        <div class="extension-view-actions">
          <div id="extension-view-actions"></div>
          <button class="btn btn-sm" id="extension-view-close">返回相册</button>
        </div>
      </div>
      <div class="extension-view-body"></div>
    </div>
  </div>
</div>
<script>
// 模拟 HostChannel 挂上来的两个按钮（与 image-cleaner 声明的一致）
setTimeout(function () {
  var box = document.getElementById('extension-view-actions');
  ['重新扫描', '设置'].forEach(function (text) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-sm obx-host-action';
    b.innerHTML = '<svg class="obx-icon"><use href="#refresh-cw"></use></svg> ' + text;
    box.appendChild(b);
  });
  var header = document.getElementById('extension-header');
  var toolbar = document.getElementById('main-toolbar');
  var close = document.getElementById('extension-view-close');
  var first = box.querySelector('button');
  var cs = function (el) { var s = getComputedStyle(el); return {
    fontSize: s.fontSize, height: Math.round(el.getBoundingClientRect().height),
    padding: s.paddingTop + ' ' + s.paddingRight }};
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
    close: cs(close), mounted: cs(first),
    // 挂载组与「返回相册」的间距：这两个在真实 UI 里是相邻的一排
    gapToClose: Math.round(close.getBoundingClientRect().left - box.getBoundingClientRect().right),
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

        check('扩展头与主工具栏逐像素同高',
              m['headerH'] == m['toolbarH'], f"header={m['headerH']} toolbar={m['toolbarH']}")
        check('两者都是壳的工具栏高度 48px',
              m['headerH'] == 48 and m['toolbarH'] == 48, f"{m['headerH']} / {m['toolbarH']}")
        check('扩展头上边缘与主工具栏上边缘齐平',
              m['headerTop'] == 0, str(m['headerTop']))
        check('扩展头主标题字号与主工具栏一致（15px）',
              m['title']['fontSize'] == m['mainTitle']['fontSize'] == '15px',
              f"header={m['title']['fontSize']} toolbar={m['mainTitle']['fontSize']}")
        check('扩展头副标题字号与主工具栏一致（11px）',
              m['sub']['fontSize'] == m['mainSub']['fontSize'] == '11px',
              f"header={m['sub']['fontSize']} toolbar={m['mainSub']['fontSize']}")
        check('扩展头副标题有内容且占位（不是零高空行）',
              bool(m['subText']) and m['sub']['height'] > 0,
              f"text={m['subText']!r} height={m['sub']['height']}")
        check('挂上来的按钮与「返回相册」字号一致',
              m['mounted']['fontSize'] == m['close']['fontSize'],
              f"mounted={m['mounted']['fontSize']} close={m['close']['fontSize']}")
        check('挂上来的按钮与「返回相册」高度一致',
              m['mounted']['height'] == m['close']['height'],
              f"mounted={m['mounted']['height']} close={m['close']['height']}")
        check('扩展头里的按钮字号是 13px（与插件工具栏常态一致）',
              m['mounted']['fontSize'] == '13px', m['mounted']['fontSize'])
        check('扩展头按钮高度 ≥ 28px（不是被压扁的 12px 小按钮）',
              m['mounted']['height'] >= 28, str(m['mounted']['height']))
        check('挂载组与「返回相册」相邻（间距 ≤ 16px，不是被 space-between 摊到中间）',
              0 <= m['gapToClose'] <= 16, str(m['gapToClose']))
        check('右侧整组贴边（与头部右内边距齐平）',
              0 <= m['wrapRight'] <= 20, str(m['wrapRight']))
        check('头部没有换行（高度未被撑到两行）',
              not m['headerWrapped'], str(m['headerH']))
    finally:
        driver.quit()
    return 1 if failures else 0


if __name__ == '__main__':   # pragma: no cover
    sys.exit(main())
