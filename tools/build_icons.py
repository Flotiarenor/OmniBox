"""由 `res/icons/icon_data.json` 生成图标资源与注入函数。

产出三份（同一份图形，分别给不同地方用）：

1. `res/icons/icons.svg` —— sprite 本体（**源与调试用**，也可直接 URL 打开看全部图标）；
2. `toolbox/shell/icons.generated.js` —— 供插件页与壳共用的一次性注入函数；
3. `shell/frontend/src/core/icons.generated.ts` —— 壳（Vue）导入同一份函数。

为什么不是"页面里用 `<use href="/res/icons/icons.svg#名字">` 引用外部文件"
--------------------------------------------------------------------------
**外部文件的 `<use>` 在 pywebview 的 WebView2 里不渲染。** 实机实测（三层对照）：

| 形态 | 结果 |
| --- | --- |
| `<use href="#同文档 symbol">`（同一个 `<svg>`） | 正常 |
| `<use href="#同文档 symbol">`（同文档的另一个 `<svg>`） | 正常 |
| `<use href="外部.svg#symbol">` | **空白**（包围盒 0） |
| `<use xlink:href="外部.svg#symbol">` | **空白** |
| `<use href="http://host/外部.svg#symbol">` | **空白** |
| 形状直接内联 | 正常 |

Chrome 会渲染外部引用，所以这类缺陷在浏览器里测不出来 —— 只有真的用 `main.py`
打开窗口才会暴露"图标全空、DOM 却正确"。因此选择把 sprite **内联进文档**：
同文档引用在两个内核里都正常。`res/icons/icons.svg` 仍然生成（可读、可 diff、
可用 URL 直接查看），但**不是加载路径**。

用法
----
    venv/Scripts/python tools/build_icons.py           # 生成
    venv/Scripts/python tools/build_icons.py --check   # 只校验，不写文件（门禁用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 源数据与产物同目录：`res/icons/` 是图标这件事的唯一落点。生成器放在 tools/ 是因为
# 它属于"开发期脚本"（与 check_*.py 同类）；数据与授权属于资产，不该埋在脚本目录里。
ICON_DIR = PROJECT_ROOT / 'res' / 'icons'
DATA_FILE = ICON_DIR / 'icon_data.json'
SPRITE_FILE = ICON_DIR / 'icons.svg'
# 注入函数：插件页与壳各一份（一个 ES module，一个可直接 <script> 的 IIFE）
JS_MODULE = PROJECT_ROOT / 'shell' / 'frontend' / 'public' / 'shell' / 'icons.generated.js'
TS_MODULE = PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'core' / 'icons.generated.ts'

# 引用点：Vue 模板里的静态字面量、JS/模板字符串里的动态插值、manifest 的 icon: 值。
# 三者的写法都要认，否则"引用了未冻结的图标"这条校验会对真实代码视而不见：
#   <Icon name="icon:sun" />              → 静态
#   :name="`icon:${s.icon}`" / 'icon:' + x → 动态（校验 s.icon 那份短名表）
#   "icon": "icon:book-open"              → manifest
STATIC_ICON_RE = re.compile(r'icon:([a-z0-9]+(?:-[a-z0-9]+)*)')
DYNAMIC_ICON_RE = re.compile(r'icon:\$\{')
# SECTIONS 这类"id → 短名"表：图标名以带引号的短字符串出现，靠上下文约束而不是逐条匹配
SHORT_NAME_RE = re.compile(r"icon:\s*'([a-z0-9]+(?:-[a-z0-9]+)*)'")
# manifest 的 `"icon": "icon:x"` 与 Python 里的 `'icon:x'` 都要认
MANIFEST_USE_RE = re.compile(r'["\']icon:([a-z0-9-]+)["\']')

ICON_NAME_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')

HEADER = """<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<!--
  OmniBox 图标 sprite —— 由 tools/build_icons.py 生成，请勿手工编辑。
  图标来自 Lucide（{package}），授权 {license}（https://lucide.dev/license）。
  增删图标：tools/fetch_lucide_icons.py --add <名字>，再跑 tools/build_icons.py。

  注意：本文件是**源与调试用**，不是加载路径。页面里引用外部文件的 <use> 在
  pywebview 的 WebView2 里不渲染（实测包围盒恒为 0），所以运行时由
  toolbox 注入函数把这份 sprite 内联进文档，再用同文档的 <use href="#名字"> 引用。
  详见 shell/frontend/public/shell/icons.generated.js。
-->
<svg xmlns="http://www.w3.org/2000/svg" style="display:none">
"""

# 注入函数：一份 IIFE（插件页 <script>）与一份 ES module（壳 Vue 导入）。
# 两份内容同源，正文用占位符替换，避免"改了一份忘另一份"。
_INJECTOR_TEMPLATE = """/**
 * 图标 sprite 的注入与取名函数 —— 由 tools/build_icons.py 生成，请勿手工编辑。
 *
 * 为什么图标要内联进文档，而不是 <use href="/res/icons/icons.svg#名字"> 引用外部文件：
 * **外部文件的 <use> 在 pywebview 的 WebView2 里不渲染**。实机三层对照（包围盒宽度）：
 *   同文档 symbol + use（同一个 <svg>）→ 16px   ✅
 *   同文档 symbol + use（另一个 <svg>）→ 16px   ✅
 *   外部文件 href / xlink:href / 全 URL  → 0px   ❌
 *   形状直接内联                        → 16px   ✅
 * Chrome 会渲染外部引用，所以这个缺陷在浏览器里完全测不出来：DOM 正确、类名正确、
 * 控制台无报错，只是窗口里一片空白。因此运行时把 sprite 内联进文档一次，
 * 之后页面里一律写 <svg class="obx-icon"><use href="#名字"></use></svg>。
 *
 * 描边属性写在 <symbol> 自己身上（shadow tree，外部样式进不去），且必须是
 * currentColor，否则图标不跟随主题与用户自定义颜色。
 */
%(names)s
/** 内联 sprite。幂等：重复调用不会重复插入。返回是否成功插入。 */
export function ensureIcons() {
%(body)s
}

/**
 * 图标名 → SVG 标记。给"必须拼字符串"的场景用（模板字符串、`innerHTML`）：
 *
 *     grid.innerHTML = Icons.html('icon:images', 'empty-state-icon')
 *     badges.push(Icons.html('icon:pin') + ' 已提升')
 *
 * 名字未冻结时返回空串并 `console.warn` —— 静默返回空串会让"图标不显示"变成一个
 * 毫无线索的现象（本项目已经因为这类"不报错、只是空白"的缺陷吃过一次亏）。
 * 非 `icon:` 前缀的值（旧插件的 emoji）原样返回，保证旧插件不坏。
 */
export function iconHtml(name, className) {
  if (typeof name !== 'string' || name.slice(0, 5) !== 'icon:') {
    return name || '';
  }
  var id = name.slice(5);
  if (!KNOWN[id]) {
    if (typeof console !== 'undefined') {
      console.warn('[icons] 未冻结的图标名：' + id + '（先跑 tools/fetch_lucide_icons.py --add ' + id + '）');
    }
    return '';
  }
  var cls = 'obx-icon' + (className ? ' ' + className : '');
  return '<svg class="' + cls + '" aria-hidden="true"><use href="#' + id + '"></use></svg>';
}
"""

_SPRITE_MARKER = 'obx-icons'


def _sprite_markup(icons: dict, meta: dict) -> str:
    """sprite 的完整标记（含 XML 注释头），注入时直接 innerHTML 用。"""
    return render(icons, meta)


def _render_ts(icons: dict, meta: dict) -> str:
    markup = _sprite_markup(icons, meta)
    body = (
        f"  if (document.getElementById('{_SPRITE_MARKER}')) return false;\n"
        f"  var container = document.createElement('div');\n"
        f"  container.id = '{_SPRITE_MARKER}';\n"
        f"  container.setAttribute('aria-hidden', 'true');\n"
        f"  container.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden';\n"
        f"  container.innerHTML = SPRITE;\n"
        f"  document.body.appendChild(container);\n"
        f"  return true;\n"
    )
    # 名字表：iconHtml() 用它把"未冻结的名字"变成一条明确的 console.warn，
    # 而不是静默返回空串（那正是本项目吃过一次亏的失效形态）
    names = 'const KNOWN = {\n' + ''.join(
        f"  '{name}': true,\n" for name in sorted(icons)
    ) + '};\n\n'
    const = 'const SPRITE = ' + json.dumps(markup, ensure_ascii=False) + ';\n\n'
    return const + _INJECTOR_TEMPLATE % {'names': names, 'body': body}


def _render_js_iife(icons: dict, meta: dict) -> str:
    """插件页用的 IIFE 版本：<script src="/shell/icons.generated.js"> 后直接生效。"""
    body = _render_ts(icons, meta)
    # 与 TS 版本同源：去掉 export 关键字，并把两个函数挂到 window 上供插件页面调用
    body = body.replace('export function', 'function')
    return body + """

window.Icons = { ensure: ensureIcons, html: iconHtml };

// 自动注入：插件页在 <head> 里同步加载本脚本，此时还没有 body，
// 因此挂在 DOMContentLoaded 上（早于任何插件代码渲染图标）。
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', ensureIcons);
} else {
  ensureIcons();
}
"""


def load_data() -> dict:
    if not DATA_FILE.is_file():
        raise SystemExit(f'缺少图标源数据：{DATA_FILE.relative_to(PROJECT_ROOT).as_posix()}')
    return json.loads(DATA_FILE.read_text(encoding='utf-8'))


def collect_references() -> dict:
    """扫描仓库里真实引用到的图标名 → 引用它的文件（用于反向校验）。

    动态插值（`icon:${s.icon}`）扫不出具体名字，但它一定读的是同一文件里的
    SECTIONS 表，所以对 `.vue` 额外把 `icon: '短名'` 这种表项也收进来 ——
    否则设置页那 6 个图标会全部漏检。
    """
    refs: dict = {}
    sources = list((PROJECT_ROOT / 'plugins').glob('*/manifest.json'))
    sources += list((PROJECT_ROOT / 'shell' / 'frontend' / 'src').rglob('*.vue'))
    # 后端也会给出图标（manifest 缺失 icon 字段时的默认值），一并算作引用点
    sources += [PROJECT_ROOT / 'shell' / 'backend' / 'plugin_manager.py']

    for path in sources:
        text = path.read_text(encoding='utf-8')
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if path.suffix == '.vue':
            # 静态字面量 + `icon:${...}` 动态插值对应的短名表
            names = set(STATIC_ICON_RE.findall(text))
            if DYNAMIC_ICON_RE.search(text):
                names |= set(SHORT_NAME_RE.findall(text))
        else:
            names = set(MANIFEST_USE_RE.findall(text))
        for name in names:
            refs.setdefault(name, []).append(rel)
    return refs


def render(icons: dict, meta: dict) -> str:
    parts = [HEADER.format(package=meta.get('package', 'lucide-static'), license=meta.get('license', 'ISC'))]
    for name in sorted(icons):
        icon = icons[name]
        parts.append(
            f'  <symbol id="{name}" viewBox="{icon["viewBox"]}" '
            f'fill="none" stroke="currentColor" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round">\n'
            f'    {icon["inner"]}\n'
            f'  </symbol>\n'
        )
    parts.append('</svg>\n')
    return ''.join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description='生成 shell 的图标 sprite')
    parser.add_argument('--check', action='store_true', help='只校验不写文件（门禁用）')
    args = parser.parse_args()

    data = load_data()
    icons: dict = data.get('icons', {})
    if not icons:
        raise SystemExit('icon_data.json 里没有任何图标')

    errors = []
    for name in icons:
        if not ICON_NAME_RE.match(name):
            errors.append(f'图标名 {name!r} 不是 kebab-case')

    refs = collect_references()
    for name in sorted(refs):
        if name not in icons:
            errors.append(
                f'引用了未冻结的图标 {name}（{"、".join(sorted(set(refs[name])))}）：'
                f'先运行 tools/fetch_lucide_icons.py --add {name}'
            )

    unused = sorted(set(icons) - set(refs))
    if unused:
        print(f'提示：已冻结但当前无引用（供后续阶段使用）：{"、".join(unused)}', file=sys.stderr)

    if errors:
        for error in errors:
            print(f'[error] {error}', file=sys.stderr)
        return 1

    outputs = {
        SPRITE_FILE: render(icons, data),
        TS_MODULE: _render_ts(icons, data),
        JS_MODULE: _render_js_iife(icons, data),
    }

    if args.check:
        stale = []
        for path, expected in outputs.items():
            current = path.read_text(encoding='utf-8') if path.is_file() else ''
            if current != expected:
                stale.append(path.relative_to(PROJECT_ROOT).as_posix())
        if stale:
            for rel in stale:
                print(f'[error] {rel} 与 res/icons/icon_data.json 不一致', file=sys.stderr)
            print('        运行 venv/Scripts/python tools/build_icons.py 重新生成', file=sys.stderr)
            return 1
        print(f'icons: OK（{len(icons)} 个图标，sprite 与注入函数均与源数据一致）')
        return 0

    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    print(f'icons: 已生成 {len(outputs)} 份产物（{len(icons)} 个图标，引用点 {len(refs)} 个）')
    for path in outputs:
        print(f'  {path.relative_to(PROJECT_ROOT).as_posix()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
