"""把 Lucide 图标冻结进仓库（**构建期工具，运行时不联网**）。

为什么要有这一步
----------------
OmniBox 是本地桌面应用，运行期不能依赖 CDN，也不该为了几个图标把图标包加进
`shell/frontend/package.json` —— sprite 是运行时静态资源，不参与 JS 构建，加依赖只会
连带影响打包收集规则（`docs/Releases/spec_common.py`、`tools/check_packaging.py`）。

所以采用"取一次、冻结进仓库"：本脚本把用到的图标从 `lucide-static` 的官方 SVG
抽成 `res/icons/icon_data.json`（只留图形本体与 viewBox，去掉许可注释以外的冗余属性）。
`tools/build_icons.py` 再据此生成 `res/icons/icons.svg`。
两个脚本都不参与常规构建，只有增删图标时才需要手动运行本脚本。

用法
----
    venv/Scripts/python tools/fetch_lucide_icons.py            # 按 icon_data.json 里已有的名字刷新
    venv/Scripts/python tools/fetch_lucide_icons.py --list     # 列出已冻结的图标
    venv/Scripts/python tools/fetch_lucide_icons.py --add bell # 新增一个再整表刷新

授权：Lucide 为 ISC 许可（https://lucide.dev/license）；每个图标源文件顶部自带
`@license lucide-static vX.Y.Z - ISC` 注释，本脚本把该注释保留到 icon_data.json 的
`source` 字段，生成 sprite 时一并写出。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 与生成物同目录：res/icons/ 是图标这件事的唯一落点（源数据、授权、产物都在这里）
DATA_FILE = PROJECT_ROOT / 'res' / 'icons' / 'icon_data.json'
CDN = 'https://unpkg.com/lucide-static@{version}/icons/{name}.svg'
REGISTRY = 'https://registry.npmjs.org/lucide-static'

SVG_OPEN_RE = re.compile(r'<svg\b[^>]*>', re.IGNORECASE)
SVG_CLOSE_RE = re.compile(r'</svg>', re.IGNORECASE)
VIEWBOX_RE = re.compile(r'viewBox="([^"]+)"', re.IGNORECASE)
# 行尾可能是 `ISC -->`（注释结束符），捕获时先把它剥掉
LICENSE_RE = re.compile(r'@license\s+([^\r\n]+?)\s*(?:-->)?\s*(?:\r?\n|$)')
WHITESPACE_RE = re.compile(r'\s+')


def latest_version() -> str:
    with urllib.request.urlopen(REGISTRY, timeout=30) as resp:
        meta = json.load(resp)
    return meta['dist-tags']['latest']


def fetch_icon(name: str, version: str, attempts: int = 3) -> dict:
    """取一个图标并抽出图形本体。网络抖动重试若干次（unpkg 偶发超时/404 抖动）。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_icon_once(name, version)
        except Exception as exc:                       # 网络异常种类多（超时/404 抖动），统一重试
            last_error = exc
            if attempt < attempts:
                print(f'  {name}: 第 {attempt} 次失败（{type(exc).__name__}），重试…', file=sys.stderr)
                time.sleep(1.5 * attempt)
    raise SystemExit(f'{name}: 取回失败（{attempts} 次）：{last_error}')


def _fetch_icon_once(name: str, version: str) -> dict:
    url = CDN.format(version=version, name=name)
    with urllib.request.urlopen(url, timeout=30) as resp:
        raw = resp.read().decode('utf-8')

    open_tag = SVG_OPEN_RE.search(raw)
    close_tag = SVG_CLOSE_RE.search(raw)
    if not open_tag or not close_tag:
        raise SystemExit(f'{name}: 取回的 SVG 结构不符合预期（缺少 <svg> 或 </svg>）：{url}')

    view_box = VIEWBOX_RE.search(open_tag.group(0))
    if not view_box:
        raise SystemExit(f'{name}: <svg> 上没有 viewBox：{url}')

    inner = raw[open_tag.end():close_tag.start()].strip()
    inner = WHITESPACE_RE.sub(' ', inner)
    if not inner:
        raise SystemExit(f'{name}: 图形本体为空：{url}')

    license_match = LICENSE_RE.search(raw)
    return {
        'viewBox': view_box.group(1),
        'inner': inner,
        'source': license_match.group(1).strip() if license_match else f'lucide-static@{version}',
    }


def load_data() -> dict:
    if not DATA_FILE.is_file():
        return {'library': 'lucide', 'license': 'ISC', 'icons': {}}
    return json.loads(DATA_FILE.read_text(encoding='utf-8'))


def main() -> int:
    parser = argparse.ArgumentParser(description='冻结 Lucide 图标到 res/icons/icon_data.json')
    parser.add_argument('--add', action='append', default=[], metavar='NAME',
                        help='新增一个图标名（kebab-case，可重复）')
    parser.add_argument('--list', action='store_true', help='只列出已冻结的图标')
    parser.add_argument('--version', default='', help='指定 lucide-static 版本（默认取 latest）')
    args = parser.parse_args()

    data = load_data()
    icons: dict = data.setdefault('icons', {})

    if args.list:
        for name in sorted(icons):
            print(f'{name:<16} {icons[name]["source"]}')
        print(f'共 {len(icons)} 个图标（{DATA_FILE.relative_to(PROJECT_ROOT).as_posix()}）')
        return 0

    for name in args.add:
        icons.setdefault(name, {})

    if not icons:
        print('icon_data.json 里还没有图标名：先用 --add 指定要冻结的图标', file=sys.stderr)
        return 1

    version = args.version or latest_version()
    print(f'lucide-static {version}')
    data['library'] = 'lucide'
    data['license'] = 'ISC'
    data['package'] = f'lucide-static@{version}'

    # 逐个落盘：一次抓几十个图标时，中途一次网络超时不该让整批白跑
    # （实测踩到过：unpkg 偶发 read timeout，末尾统一写文件等于全部丢失）
    skipped: list[str] = []
    for name in sorted(icons):
        if icons[name].get('inner'):
            skipped.append(name)          # 已冻结且图形完好：不必重新下载
            continue
        icons[name] = fetch_icon(name, version)
        DATA_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
            encoding='utf-8',
        )
        print(f'  fetched {name}')

    if skipped:
        print(f'跳过（已存在）{len(skipped)} 个')
    print(f'已写入 {DATA_FILE.relative_to(PROJECT_ROOT).as_posix()}（{len(icons)} 个图标）')
    print('下一步：venv/Scripts/python tools/build_icons.py')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
