"""检查 image-viewer 扩展面板头部与「内嵌插件挂上来的按钮」的视觉参数是否一致。

背景：用户实测反馈"渲染的上边栏宽度/字体大小不一样"——宿主扩展头原来是给
`14px/600` 标题 + 一个 `.btn.btn-sm`（12px/3px）设计的薄条，把插件工具栏的按钮
（13px）挂上来之后，两者不同族。这个脚本用无头浏览器读真实计算值，避免又靠肉眼估。

用法：venv/Scripts/python.exe tests/debug_extension_header_ui.py
缺少 selenium 或 chrome 时跳过（退出码 0）。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<link rel="stylesheet" href="/shell/variables.css">
<link rel="stylesheet" href="/shell/base.css">
<link rel="stylesheet" href="{iv_css}">
</head><body>
<div id="extension-view" class="extension-view">
  <div class="extension-view-header">
    <span id="extension-view-title">相册清理</span>
    <div class="extension-view-actions">
      <div id="extension-view-actions"></div>
      <button class="btn btn-sm" id="extension-view-close">返回相册</button>
    </div>
  </div>
  <div class="extension-view-body"></div>
</div>
<script>
// 模拟 HostChannel 挂上来的两个按钮（与 image-cleaner 声明的一致）
setTimeout(function () {{
  var box = document.getElementById('extension-view-actions');
  ['重新扫描', '设置'].forEach(function (text) {{
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn btn-sm obx-host-action';
    b.innerHTML = '<svg class="obx-icon"><use href="#refresh-cw"></use></svg> ' + text;
    box.appendChild(b);
  }});
  var header = document.querySelector('.extension-view-header');
  var close = document.getElementById('extension-view-close');
  var first = box.querySelector('button');
  var title = document.getElementById('extension-view-title');
  var cs = function (el) {{ var s = getComputedStyle(el); return {{
    fontSize: s.fontSize, height: Math.round(el.getBoundingClientRect().height),
    padding: s.paddingTop + ' ' + s.paddingRight }};
  }};
  window.__m = {{
    headerH: Math.round(header.getBoundingClientRect().height),
    title: cs(title), close: cs(close), mounted: cs(first),
    // 挂载组与「返回相册」的间距：这两个在真实 UI 里是相邻的一排
    gapToClose: Math.round(close.getBoundingClientRect().left - box.getBoundingClientRect().right),
    wrapRight: Math.round(header.getBoundingClientRect().right - header.lastElementChild.getBoundingClientRect().right),
    headerWrapped: header.getBoundingClientRect().height > 60,
  }};
}}, 60);
</script>
</body></html>
"""


def main() -> int:
    if not shutil.which('node'):
        print('未检测到 node，跳过')
        return 0
    try:
        from selenium import webdriver
    except ImportError:
        print('未安装 selenium，跳过')
        return 0

    iv_css = '/plugins/image-viewer/frontend/image-viewer.css'
    tmp = Path(tempfile.mkdtemp(prefix='ext_header_'))
    page = tmp / 'index.html'
    page.write_text(PAGE.format(iv_css=iv_css), encoding='utf-8')

    options = webdriver.ChromeOptions()
    for arg in ('--headless=new', '--disable-gpu', '--no-sandbox', '--window-size=1280,860'):
        options.add_argument(arg)
    driver = webdriver.Chrome(options=options)
    failures = []
    try:
        # 用 file:// 打开时 /shell/* 与 /plugins/* 取不到，改成注入绝对路径的 file URL
        html = page.read_text(encoding='utf-8')
        html = html.replace('href="/shell/variables.css"',
                            f'href="{(PROJECT_ROOT / "shell/frontend/public/shell/variables.css").as_uri()}"')
        html = html.replace('href="/shell/base.css"',
                            f'href="{(PROJECT_ROOT / "shell/frontend/public/shell/base.css").as_uri()}"')
        html = html.replace(f'href="{iv_css}"',
                            f'href="{(PROJECT_ROOT / "plugins/image-viewer/frontend/image-viewer.css").as_uri()}"')
        page.write_text(html, encoding='utf-8')
        driver.get(page.as_uri())
        import time
        time.sleep(0.4)
        m = driver.execute_script('return window.__m;')
        print(json.dumps(m, ensure_ascii=False, indent=2))

        def check(name, ok, detail=''):
            print(f'  {"PASS" if ok else "FAIL"}  {name}{(" — " + detail) if detail else ""}')
            if not ok:
                failures.append(name)

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
        check('扩展头高度在 40-52px 之间（与主工具栏 48px 同族）',
              40 <= m['headerH'] <= 52, str(m['headerH']))
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
