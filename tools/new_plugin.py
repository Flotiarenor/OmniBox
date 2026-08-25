#!/usr/bin/env python3
"""OmniBox 插件脚手架。

从模板 tools/examples/hello-world 生成一个可运行的新插件，
免去手写 manifest.json 与骨架的仪式。

用法：
    python tools/new_plugin.py my-tool
    python tools/new_plugin.py my-tool --route /custom --display "我的工具"
    python tools/new_plugin.py             # 不带参数进入交互模式，逐项提问
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLUGINS_DIR = PROJECT_ROOT / 'plugins'
TEMPLATE_DIR = PROJECT_ROOT / 'tools' / 'examples' / 'hello-world'

SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-_]*$')
RESERVED_ROUTES = {'/', '/settings'}
RENDER_SUFFIXES = {'.json', '.py', '.html'}


def to_class_name(slug: str) -> str:
    parts = [p for p in re.split(r'[-_]+', slug) if p]
    return ''.join(p[:1].upper() + p[1:] for p in parts) + 'Plugin'


def to_display_name(slug: str) -> str:
    return ' '.join(p[:1].upper() + p[1:] for p in re.split(r'[-_]+', slug) if p)


def render(text: str, name: str, class_name: str, route: str, display: str) -> str:
    return (text
            .replace('__CLASS__', class_name)
            .replace('__ROUTE__', route)
            .replace('__DISPLAY__', display)
            .replace('__NAME__', name))


def new_plugin(name: str, route: str | None = None, display: str | None = None,
               plugins_dir: str | Path | None = None) -> int:
    plugins_dir = Path(plugins_dir or DEFAULT_PLUGINS_DIR)
    if not SLUG_RE.fullmatch(name):
        print(f'[new_plugin] 插件名 {name!r} 必须是 kebab-case slug（小写字母/数字/连字符）')
        return 1

    route = route or f'/{name}'
    if not route.startswith('/'):
        route = '/' + route
    if route in RESERVED_ROUTES:
        print(f'[new_plugin] 路由 {route} 是保留路由，请换一个')
        return 1

    display = display or to_display_name(name)
    class_name = to_class_name(name)

    target = plugins_dir / name
    if target.exists():
        print(f'[new_plugin] 目标已存在，跳过: {target}')
        return 1
    if not TEMPLATE_DIR.is_dir():
        print(f'[new_plugin] 模板目录不存在: {TEMPLATE_DIR}')
        return 1

    plugins_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(TEMPLATE_DIR.rglob('*')):
        if not src.is_file():
            continue
        rel = src.relative_to(TEMPLATE_DIR)
        if '__pycache__' in rel.parts or src.suffix == '.pyc':
            continue
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix in RENDER_SUFFIXES:
            content = render(src.read_text(encoding='utf-8'), name, class_name, route, display)
            dst.write_text(content, encoding='utf-8')
        else:
            shutil.copy2(src, dst)

    print(f'[new_plugin] 已生成插件: {target}')
    print(f'[new_plugin] 路由: {route}  后端类: {class_name}  显示名: {display}')
    print('[new_plugin] 重启 OmniBox 后即可在导航栏看到它。')
    return 0


def interactive(plugins_dir: str | Path | None = None) -> int:
    print('OmniBox 插件脚手架（交互模式）\n')
    while True:
        name = input('插件名（kebab-case slug，如 my-tool）: ').strip()
        if not name:
            print('已取消。')
            return 1
        if not SLUG_RE.fullmatch(name):
            print(f'  插件名必须是 kebab-case slug（小写字母/数字/连字符），收到: {name!r}\n')
            continue
        break

    default_route = f'/{name}'
    route = input(f'前端路由（默认 {default_route}）: ').strip() or default_route

    default_display = to_display_name(name)
    display = input(f'显示名（默认 {default_display}）: ').strip() or default_display

    print()
    return new_plugin(name, route, display, plugins_dir)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='生成 OmniBox 插件骨架')
    parser.add_argument('name', nargs='?', help='插件名（kebab-case slug，如 my-tool）；缺省进入交互模式')
    parser.add_argument('--route', help='前端路由，默认 /<name>')
    parser.add_argument('--display', help='导航栏显示名，默认由 name 推导')
    parser.add_argument('--plugins-dir', type=Path, default=DEFAULT_PLUGINS_DIR,
                        help='插件目录，默认项目 plugins/')
    args = parser.parse_args(argv)
    if args.name is None:
        return interactive(args.plugins_dir)
    return new_plugin(args.name, args.route, args.display, args.plugins_dir)


if __name__ == '__main__':
    sys.exit(main())
