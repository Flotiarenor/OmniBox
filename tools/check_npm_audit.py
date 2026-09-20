"""前端依赖漏洞门禁（`npm audit`）。

为什么需要它
------------
本机与镜像源都**看不到漏洞**：`npm config get registry` 指向 npmmirror，而它不实现
audit 端点，直接跑 `npm audit` 得到的是：

    npm error audit endpoint returned an error
    { error: '[NOT_IMPLEMENTED] /-/npm/v1/security/* not implemented yet' }

于是这个仓库从来没有跑过 audit —— 实装当天用它查出 5 条（4 high / 1 moderate），
其中 vite 那条唯一的修复路径是跨大版本。这类"工具本身不报错、只是不工作"的缺口，
正是门禁该补的位置。所以本脚本**显式指定官方 registry**，不依赖使用者的 npm 配置。

例外必须带理由且会自动过期
--------------------------
有的漏洞在当前版本范围内修不掉（只能跨大版本），这时把它登记进 ACCEPTED，
连同"为什么接受"与"升级到哪个版本才能消"。脚本会**核对登记项确实还在报告里**：
一旦上游修好、或依赖被升级，登记项失效就报错要求删除 —— 否则例外会悄悄烂成永久豁免
（与 tools/check_plugins.py 的 manifest 读取方登记表同一个思路）。

用法
----
    venv/Scripts/python tools/check_npm_audit.py            # 门禁
    venv/Scripts/python tools/check_npm_audit.py --list     # 打印完整报告
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = PROJECT_ROOT / 'shell' / 'frontend'

# 必须显式指定：本机默认 registry 是 npmmirror，它不实现 audit 端点（见文件头）
REGISTRY = 'https://registry.npmjs.org'

# 当前接受、且只能在同一层级修掉的漏洞：advisory 的 GHSA 编号 → (包名, 接受理由, 何时能消)
ACCEPTED: dict = {
    'GHSA-4w7w-66w2-5vf9': (
        'vite', '仅 vite dev server 的 .map 路径穿越受影，打包产物不含 dev server', 'vite 8'),
    'GHSA-v6wh-96g9-6wx3': (
        'vite', 'launch-editor 经 UNC 路径泄露 NTLMv2 哈希，仅 vite dev server', 'vite 8'),
    'GHSA-fx2h-pf6j-xcff': (
        'vite', 'server.fs.deny 在 Windows 备用路径上被绕过，仅 vite dev server', 'vite 8'),
    'GHSA-67mh-4wv8-2f99': (
        'esbuild', 'vite 传递依赖；同样的 dev server 场景，随 vite 一并解决', 'vite 8'),
}


def _run_audit() -> dict | None:
    """跑 npm audit，返回 JSON；registry 不可达时返回 None（跳过而不是判失败）。"""
    try:
        # shell=True 时必须传字符串：POSIX 下 `[unix_shell, '-c'] + list` 只会把
        # 列表第一个元素当命令，其余变成位置参数 —— 结果是执行 `npm`（无参数）打印
        # usage、拿不到 JSON。写成字符串后，Linux 走 sh -c、Windows 走 cmd /c，
        # 两边行为一致（npm 在 Windows 是 npm.cmd，不经 shell 起不来）。
        proc = subprocess.run(
            f'npm audit --json --registry={REGISTRY}',
            cwd=str(FRONTEND_DIR),
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            shell=True,
        )
    except OSError as exc:
        print(f'跳过：无法执行 npm（{exc}）', file=sys.stderr)
        return None

    raw = proc.stdout or ''
    start = raw.find('{')
    if start < 0:
        # 网络/registry 故障时 npm 的 stdout 可能为空，这时按"跳过"处理
        detail = (proc.stderr or raw).strip().splitlines()
        head = detail[0] if detail else '无输出'
        if 'not implemented' in head or 'ENOTFOUND' in head or 'ECONN' in head or '404' in head:
            print(f'跳过：audit 端点不可用（{head}）', file=sys.stderr)
            return None
        print(f'[error] npm audit 未返回 JSON：{head}', file=sys.stderr)
        return {'vulnerabilities': {}, '_error': head}
    return json.loads(raw[start:])


def collect(report: dict) -> list:
    """把 npm audit 的 JSON 展开成 [(包名, 严重度, ghsa, 标题)]。

    只保留"直接给的公告"（`via` 里的对象）；纯字符串的 `via` 是"由别的包传入"，
    不单独计，随来源包一起处理。
    """
    rows = []
    for name, vuln in report.get('vulnerabilities', {}).items():
        advisories = [item for item in vuln.get('via', []) if isinstance(item, dict)]
        for item in advisories:
            url = item.get('url') or ''
            ghsa = url.rsplit('/', 1)[-1] if '/advisories/' in url else url
            rows.append((name, vuln.get('severity', '?'), ghsa, item.get('title', '')))
    return rows


def judge(rows: list, accepted: dict | None = None) -> list:
    """纯判定：返回错误文本列表（空 = 通过）。

    两侧都要管：报告里有未登记的公告 → 报错；登记表里有报告里已不存在的公告 →
    也报错（该漏洞已修复或依赖已升级，例外必须删掉）。
    """
    accepted = ACCEPTED if accepted is None else accepted
    errors = []
    seen = set()
    for name, severity, ghsa, title in sorted(rows):
        seen.add(ghsa)
        if ghsa in accepted:
            continue
        errors.append(f'{name}（{severity}，{ghsa}）：{title}；升级依赖或登记进 ACCEPTED 并写明理由')

    for ghsa, (name, _reason, fixed_in) in sorted(accepted.items()):
        if ghsa not in seen:
            errors.append(
                f'ACCEPTED 里的 {ghsa}（{name}）已不在 audit 报告里：该项已修复或依赖已升级到 {fixed_in}，'
                f'请从 tools/check_npm_audit.py 的 ACCEPTED 删除这条登记'
            )
    return errors


def accepted_summary() -> str:
    """例外清单的一句话摘要（按包聚合，供通过时的输出）。"""
    packages = sorted({name for name, _r, _f in ACCEPTED.values()})
    return '、'.join(packages)


def main() -> int:
    parser = argparse.ArgumentParser(description='npm audit 门禁（含显式例外清单）')
    parser.add_argument('--list', action='store_true', help='打印完整报告，不做判定')
    args = parser.parse_args()

    if not (FRONTEND_DIR / 'package-lock.json').is_file():
        print('[error] 找不到 shell/frontend/package-lock.json', file=sys.stderr)
        return 1

    report = _run_audit()
    if report is None:
        return 0
    if report.get('_error'):
        return 1

    rows = collect(report)

    if args.list:
        for name, severity, ghsa, title in sorted(rows):
            mark = 'ACCEPTED' if ghsa in ACCEPTED else 'NEW     '
            print(f'[{mark}] [{severity:>8}] {name:<18} {ghsa:<26} {title}')
        print(f'共 {len(rows)} 条公告')
        return 0

    errors = judge(rows)
    if errors:
        for error in errors:
            print(f'[error] {error}', file=sys.stderr)
        return 1

    print(
        f'check_npm_audit: OK（无未接受漏洞；已接受 {len(ACCEPTED)} 条，'
        f'均为 {accepted_summary()} 的 dev server 问题，随 vite 8 升级消除）'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
