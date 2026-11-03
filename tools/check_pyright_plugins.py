#!/usr/bin/env python3
"""插件后端的 pyright 棘轮门禁：新代码零错误，历史欠账按插件挂账、只减不增。

为什么需要（这是一次真实的架构退化，代价已经算清）
--------------------------------------------------
插件后端曾用 mixin 分片组织：8 到 12 个文件各有同一个宿主类的一部分方法，`self` 仍是
同一个对象。类型检查器看不出这种"分段类"，分片里的 `self.x` 一律被判成"属性不存在" ——
实测 `pyright plugins` 报 664 条错误，其中 603 条（91%）是这类伪影。噪声盖住了 61 条
真错误（参数类型不匹配、override 不兼容、可能未绑定的变量），于是插件被整体排除在
门禁之外：无论写错什么，CI 都不会红。

组件化之后伪影消失，四个插件降到 0 错误，插件才有资格进门禁。但还有两个插件（不在本轮
迁移范围）留着 10 条历史错误，它们不能被这次收紧卡住，也不能因此让整个 `plugins` 继续
免检。所以这里是棘轮而不是开关：

- **没有豁免文件的插件**（含今后新建的）：pyright 必须 0 错误，否则失败；
- **有豁免文件的插件**：允许带着已知错误，但每次跑都会打印一条 warning 和条数，
  欠账始终可见；
- **豁免文件过期**（插件已经 0 错误却还留着文件）：失败，要求删除 —— 否则棘轮会永久
  对那个插件失效，谁也不再记得它曾经被豁免过；
- **孤儿豁免文件**（没有对应插件）：失败。

为什么不直接让 `pyright main.py shell tools plugins` 当门禁：那会把两个未迁移插件的
10 条历史错误变成提交阻塞，逼着做无关重构；而把这 10 条写进 pyright 的 `exclude`
又会让那两个插件整个不再被检查，等于把它们的新错误也一起藏起来。豁免文件是这两者
之间唯一"既不阻塞、也不隐瞒"的形态，和 `tools/layering_allowlist/` 是同一套思路。

用法：
    python tools/check_pyright_plugins.py
    python tools/check_pyright_plugins.py --plugins-dir DIR --allowlist-dir DIR
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLUGINS_DIR = PROJECT_ROOT / 'plugins'
DEFAULT_ALLOWLIST_DIR = PROJECT_ROOT / 'tools' / 'pyright_allowlist'

#: pyright 一次分析 plugins 约 20 秒；给十分钟上限只为避免 CI 上偶发卡死没有上限。
PYRIGHT_TIMEOUT_SECONDS = 600


class Diagnostic(NamedTuple):
    """一条 pyright 错误，已经归到某个插件名下。"""

    plugin: str
    where: str
    rule: str
    message: str


def run_pyright(plugins_dir: Path) -> tuple[int, str, str]:
    """跑一次 pyright，返回 (返回码, stdout, stderr)。

    返回码 1 在 pyright 里表示"有错误"，是正常结果而不是执行失败，由调用方按 JSON 判断；
    执行失败（没装、崩溃）会体现在 stdout 不是 JSON 上。
    """
    command = [sys.executable, '-m', 'pyright', str(plugins_dir), '--outputjson']
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=PYRIGHT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, '', f'无法执行 pyright: {exc}'
    return completed.returncode, completed.stdout, completed.stderr


def _plugin_of(path: Path, plugins_dir: Path) -> str | None:
    """诊断所属的插件名；不在 plugins/<name>/ 下时返回 None。"""
    try:
        rel = path.resolve().relative_to(plugins_dir.resolve())
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def parse_diagnostics(
    payload: dict[str, Any],
    plugins_dir: Path,
) -> list[Diagnostic]:
    """把 pyright 的 JSON 输出转成按插件归属的诊断列表（只取 severity=error）。"""
    diagnostics: list[Diagnostic] = []
    for item in payload.get('generalDiagnostics') or []:
        if not isinstance(item, dict) or item.get('severity') != 'error':
            continue
        raw_file = item.get('file')
        if not isinstance(raw_file, str):
            continue
        path = Path(raw_file)
        plugin = _plugin_of(path, plugins_dir)
        if plugin is None:
            continue
        start = (item.get('range') or {}).get('start') or {}
        line = int(start.get('line', 0)) + 1
        column = int(start.get('character', 0)) + 1
        message = str(item.get('message') or '').strip().replace('\n', ' ')
        diagnostics.append(Diagnostic(
            plugin=plugin,
            where=f'{_rel(path)}:{line}:{column}',
            rule=str(item.get('rule') or ''),
            message=message,
        ))
    return diagnostics


def group_by_plugin(diagnostics: Iterable[Diagnostic]) -> dict[str, list[Diagnostic]]:
    grouped: dict[str, list[Diagnostic]] = {}
    for diagnostic in diagnostics:
        grouped.setdefault(diagnostic.plugin, []).append(diagnostic)
    return grouped


def _plugin_names(plugins_dir: Path) -> list[str]:
    """有 manifest 的插件目录名（与 check_plugins / plugin_ci 的判定一致）。"""
    if not plugins_dir.is_dir():
        return []
    return sorted(
        path.name for path in plugins_dir.iterdir()
        if path.is_dir() and (path / 'manifest.json').is_file()
    )


def check(
    by_plugin: dict[str, list[Diagnostic]],
    allowlist_dir: Path = DEFAULT_ALLOWLIST_DIR,
    *,
    known_plugins: Iterable[str] | None = None,
) -> tuple[list[str], list[str]]:
    """按插件比较诊断与豁免文件，返回 (errors, warnings)。纯函数，供用例构造正反例。"""
    errors: list[str] = []
    warnings: list[str] = []
    allowlist_dir = Path(allowlist_dir)
    plugins = set(known_plugins) if known_plugins is not None else set(by_plugin)

    waived = 0
    waived_count = 0
    for plugin in sorted(set(by_plugin) | plugins):
        diagnostics = by_plugin.get(plugin, [])
        waiver = allowlist_dir / f'{plugin}.txt'
        if waiver.is_file():
            if not diagnostics:
                errors.append(
                    f'[{plugin}] pyright 豁免文件 {_rel(waiver)} 已过期：该插件现在是 0 错误，'
                    f'删掉这份文件（棘轮的最后一步）')
                continue
            waived += 1
            waived_count += len(diagnostics)
            warnings.append(
                f'[{plugin}] pyright 豁免生效中（{_rel(waiver)}，{len(diagnostics)} 条已知错误）：'
                f'修完这些错误后删掉这份文件')
            continue
        for diagnostic in diagnostics:
            rule = f' [{diagnostic.rule}]' if diagnostic.rule else ''
            errors.append(f'[{plugin}] {diagnostic.where}：{diagnostic.message}{rule}')

    if allowlist_dir.is_dir():
        for waiver in sorted(allowlist_dir.glob('*.txt')):
            if waiver.stem not in set(by_plugin) | plugins:
                errors.append(
                    f'pyright 豁免文件 {_rel(waiver)} 没有对应的插件（插件已删或改名）：把它删掉')

    if waived:
        warnings.append(f'合计 {waived} 个插件、{waived_count} 条已知 pyright 错误仍被豁免')
    return errors, warnings


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='校验插件后端的 pyright 错误（按插件豁免的棘轮）')
    parser.add_argument('--plugins-dir', type=Path, default=DEFAULT_PLUGINS_DIR)
    parser.add_argument('--allowlist-dir', type=Path, default=DEFAULT_ALLOWLIST_DIR)
    args = parser.parse_args(list(argv) if argv is not None else None)

    plugins_dir = args.plugins_dir
    if not plugins_dir.is_dir():
        print(f'[error] 插件目录不存在: {plugins_dir}')
        print('check_pyright_plugins: 1 个错误, 0 个警告')
        return 1

    returncode, stdout, stderr = run_pyright(plugins_dir)
    try:
        payload = json.loads(stdout)
    except ValueError:
        print('[error] pyright 没有返回可解析的 JSON：门禁不能静默通过')
        print(f'  返回码: {returncode}')
        if stderr.strip():
            print(f'  stderr: {stderr.strip().splitlines()[-1]}')
        else:
            print(f'  stdout: {(stdout.strip() or "<空>").splitlines()[-1]}')
        print('check_pyright_plugins: 1 个错误, 0 个警告')
        return 1

    diagnostics = parse_diagnostics(payload, plugins_dir)
    errors, warnings = check(
        group_by_plugin(diagnostics),
        args.allowlist_dir,
        known_plugins=_plugin_names(plugins_dir),
    )
    for warning in warnings:
        print(f'[warning] {warning}')
    for error in errors:
        print(f'[error] {error}')
    if errors:
        print(f'check_pyright_plugins: {len(errors)} 个错误, {len(warnings)} 个警告')
        return 1
    print(f'check_pyright_plugins: OK ({len(warnings)} 个警告)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
