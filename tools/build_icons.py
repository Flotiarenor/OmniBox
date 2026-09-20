"""由 `tools/icon_data.json` 生成 `shell/frontend/public/shell/icons.svg`。

产物是**一个 sprite**：每个图标一个 `<symbol id="名字">`，页面里用
`<svg class="obx-icon"><use href="/shell/icons.svg#名字"></use></svg>` 引用。
选 sprite 而不是图标字体或逐处内联的理由（顺带决定了下面每个属性的写法）：

1. 走 `<use>` + 同源 URL，命中 `img-src 'self'`，**不需要动 CSP**（图标字体要先把
   `font-src` 从 Report-Only 提进强执行档）；
2. 没有字体加载前的图标闪烁；
3. 同一份 sprite 被壳页面与所有插件 iframe 共享，浏览器只下载一次；
4. `<symbol>` 是 shadow tree，外部无法继承页面的 fill/stroke，所以描边属性必须写在
   sprite 自己身上 —— 且必须写 `currentColor`，否则图标不跟随主题与用户自定义颜色。

生成物进提交（`public/shell/` 目录本身就是要进提交的），运行时零依赖、完全离线。
改完 `public/shell/*` 必须 `npm --prefix shell/frontend run build`，否则 `/shell/<file>`
路由优先发的仍是 dist 里的旧副本（见 AGENTS.md）。

用法
----
    venv/Scripts/python tools/build_icons.py           # 生成 sprite
    venv/Scripts/python tools/build_icons.py --check   # 只校验，不写文件（门禁用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = PROJECT_ROOT / 'tools' / 'icon_data.json'
SPRITE_FILE = PROJECT_ROOT / 'shell' / 'frontend' / 'public' / 'shell' / 'icons.svg'

# 引用点：Vue 模板里的静态字面量、JS/模板字符串里的动态插值、manifest 的 icon: 值。
# 三者的写法都要认，否则"引用了未冻结的图标"这条校验会对真实代码视而不见：
#   <Icon name="icon:sun" />              → 静态
#   :name="`icon:${s.icon}`" / 'icon:' + x → 动态（校验 s.icon 那份短名表）
#   "icon": "icon:book-open"              → manifest
STATIC_ICON_RE = re.compile(r'icon:([a-z0-9]+(?:-[a-z0-9]+)*)')
DYNAMIC_ICON_RE = re.compile(r'icon:\$\{')
# SECTIONS 这类"id → 短名"表：图标名以带引号的短字符串出现，靠上下文约束而不是逐条匹配
SHORT_NAME_RE = re.compile(r"icon:\s*'([a-z0-9]+(?:-[a-z0-9]+)*)'")
MANIFEST_USE_RE = re.compile(r'["\']icon:([a-z0-9-]+)["\']')

ICON_NAME_RE = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')

HEADER = """<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<!--
  OmniBox 图标 sprite —— 由 tools/build_icons.py 生成，请勿手工编辑。
  图标来自 Lucide（{package}），授权 {license}（https://lucide.dev/license）。
  增删图标：tools/fetch_lucide_icons.py --add <名字>，再跑 tools/build_icons.py。
  改完本文件必须重新构建：npm --prefix shell/frontend run build
-->
<svg xmlns="http://www.w3.org/2000/svg" style="display:none">
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

    content = render(icons, data)
    if args.check:
        current = SPRITE_FILE.read_text(encoding='utf-8') if SPRITE_FILE.is_file() else ''
        if current != content:
            print(
                f'[error] {SPRITE_FILE.relative_to(PROJECT_ROOT).as_posix()} 与 icon_data.json 不一致：'
                f'运行 venv/Scripts/python tools/build_icons.py',
                file=sys.stderr,
            )
            return 1
        print(f'icons: OK（{len(icons)} 个图标，sprite 与源数据一致）')
        return 0

    SPRITE_FILE.write_text(content, encoding='utf-8')
    print(
        f'icons: 已生成 {SPRITE_FILE.relative_to(PROJECT_ROOT).as_posix()}'
        f'（{len(icons)} 个图标，引用点 {len(refs)} 个）'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
