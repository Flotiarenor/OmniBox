#!/usr/bin/env python3
"""插件后端分层门禁：挡住"把类切成 mixin 文件"和"靠兄弟模块互相 import 拼起来"。

为什么需要（这是一个已经发生过两次的架构退化）
------------------------------------------------
插件后端由 `PluginManager` 用 importlib 直接加载**入口文件**（manifest 的
`backend.entry`），插件目录不是包、也不在 `sys.path` 上。于是"从一个后端文件引用另一个
后端文件"没有正常的 import 路径，历史上演化出两种绕法：

1. **把方法体拆到若干 mixin 文件里**，入口类同时继承它们，`self` 仍是同一个对象。
   文件行数下来了，耦合一点没降：所有分片共享一块由入口 `__init__` 持有的状态，
   彼此的调用关系没有任何声明。代价是静态检查整体失效 —— 每个分片里的 `self.x`
   在类型检查器看来都不存在（实测 image-viewer 与 group-mesh 两个插件的 pyright
   输出里 91% 的报错是这类伪影），真错误被噪声淹没；另外 `super()` 会命中 mixin
   而不是基类，曾导致新装用户整插件加载失败。
2. **兄弟模块自己 `load_sibling`** 去拿共享常量与纯函数。它确实能跑，但依赖方向
   变成了一张与文件布局无关的网：谁 import 谁只能靠在 20 个文件里搜名字。

目标形态与迁移配方见 `docs/plugin-backend-layout.md`：入口是唯一的装配点，其余模块
是**显式接收依赖的组件**，组件之间不互相 import，共享的无状态工具放进
`shell/backend/`（那里有真正的包 import，且已被 `HIDDEN_IMPORTS` 覆盖）。

三条规则
--------
R1 后端模块不得定义 Mixin 类，也不得写成「继承某个 Mixin」的入口类。
   —— 判据是类名或以 `Mixin` 结尾的基类名：这正是"用 mixin 做文件切分"的形状。
R2 后端单个 `.py` 文件 ≤ 400 行；manifest 声明的入口文件 ≤ 250 行。
   —— 入口只放 schema、API 表、壳钩子与装配，超过 250 行说明业务漏回来了。
R3 只有入口文件可以调用 `load_sibling`；非入口模块不得 import 兄弟模块。
   —— 非入口模块之间的依赖必须由入口注入（构造参数），这既是分层也是静态可查的前提。

豁免（允许清单）
--------------
迁移之前，现有插件必然违反上面三条。豁免按**插件**粒度放在
`tools/layering_allowlist/<plugin>.txt`：文件存在即整条规则对该插件不生效，并在输出里
留一条 warning，让"还欠着一次迁移"始终可见。这样多个智能体并行迁移时，每个插件只删
自己那一份文件，不会改到同一个共享清单。

豁免文件**过期是错误**：一个插件已经不再违反任何规则、却还留着豁免文件，说明迁移
完成了但没把钥匙交回去 —— 那会让后来的人误以为它还没迁完，也会让门禁永久对这插件
失效。删掉它是迁移的最后一步，见迁移配方的第 8 步。

用法：
    python tools/check_plugin_layering.py
    python tools/check_plugin_layering.py --plugins-dir DIR --allowlist-dir DIR
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Iterable, NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLUGINS_DIR = PROJECT_ROOT / 'plugins'
DEFAULT_ALLOWLIST_DIR = PROJECT_ROOT / 'tools' / 'layering_allowlist'

#: Mixin 类的判据：类名或以它结尾的基类名。不用 `issubclass` 探针，也不读运行期
#: 对象 —— 这些模块在 import 时会去 `load_sibling` 拉兄弟文件，静态门禁必须能在
#: 不执行插件代码的前提下给出结论（否则一条 import 副作用就能让门禁自己失败）。
MIXIN_SUFFIX = 'Mixin'
#: 单个后端文件的行数上限。定在 400 是因为它已经比现状里最长的一批组件文件
#: （400–700 行）低一档：越过它通常意味着"又往一个文件里塞了两种职责"。
MAX_FILE_LINES = 400
#: 入口文件的行数上限：入口只做装配，放不下业务。
MAX_ENTRY_LINES = 250
#: 不参与分层检查的目录名。`libs` 是插件自带第三方库（pixiv-sync 的 vendored 依赖），
#: 不是本仓库实现的代码，不该被本仓库的行数与风格规则约束。
SKIP_DIR_NAMES = frozenset({'libs', '__pycache__'})


class Violation(NamedTuple):
    """一条违规：规则、位置（仓库相对路径）、说明。"""

    rule: str
    where: str
    message: str


def _plugin_names(plugins_dir: Path) -> list[str]:
    """有 manifest 的插件目录名（与 check_plugins / plugin_ci 的判定一致）。"""
    if not plugins_dir.is_dir():
        return []
    names = []
    for path in sorted(plugins_dir.iterdir(), key=lambda item: item.name):
        if path.is_dir() and (path / 'manifest.json').is_file():
            names.append(path.name)
    return names


def _backend_files(plugin_dir: Path) -> list[Path]:
    """插件的后端 `.py` 文件（跳过 vendored 库与缓存目录）。"""
    backend = plugin_dir / 'backend'
    if not backend.is_dir():
        return []
    files = []
    for path in sorted(backend.rglob('*.py')):
        rel_parts = path.relative_to(backend).parts
        if any(part in SKIP_DIR_NAMES or part.startswith('.') for part in rel_parts[:-1]):
            continue
        files.append(path)
    return files


def _rel(path: Path, root: Path) -> str:
    """消息里一律用仓库相对路径（可 grep、与提交对得上）。"""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _parse(path: Path) -> ast.Module | None:
    """解析一个后端文件；语法错误交给 ruff / pyright，不在这里报。"""
    try:
        return ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _check_mixins(tree: ast.Module, where: str) -> list[Violation]:
    """R1：类名或以 `Mixin` 结尾的基类名 → 违规。"""
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name.endswith(MIXIN_SUFFIX):
            problems.append(Violation(
                'R1', where,
                f'定义了 Mixin 类 {node.name}：把类切成 mixin 文件后每个分片的 `self.x` '
                f'在静态检查里都不存在，且 `super()` 会命中 mixin 而不是基类。'
                f'按 docs/plugin-backend-layout.md 改成显式组件 + 入口注入'))
            continue
        bases = [ast.unparse(base).split('.')[-1] for base in node.bases]
        inherited = [base for base in bases if base.endswith(MIXIN_SUFFIX)]
        if inherited:
            problems.append(Violation(
                'R1', where,
                f'类 {node.name} 继承了 Mixin {", ".join(inherited)}：入口类只应继承 '
                f'PluginBase，能力靠组合注入而不是多重继承'))
    return problems


def _check_line_limits(path: Path, where: str, *, is_entry: bool) -> list[Violation]:
    """R2：文件行数上限（入口更严）。"""
    try:
        lines = len(path.read_text(encoding='utf-8').splitlines())
    except (OSError, UnicodeDecodeError):
        return []
    limit = MAX_ENTRY_LINES if is_entry else MAX_FILE_LINES
    if lines <= limit:
        return []
    role = '入口文件' if is_entry else '后端文件'
    hint = ('入口只放 settings_schema、register_api、壳按名调用的钩子与装配；'
            '业务挪进组件') if is_entry else '按职责拆成组件文件'
    return [Violation('R2', where, f'{role} {lines} 行，超过上限 {limit} 行：{hint}')]


def _check_sibling_usage(
    tree: ast.Module,
    where: str,
    *,
    is_entry: bool,
    local_stems: set[str],
) -> list[Violation]:
    """R3：非入口模块不得 `load_sibling`，也不得 import 兄弟模块。"""
    if is_entry:
        return []
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else '')
            if name == 'load_sibling':
                problems.append(Violation(
                    'R3', where,
                    f'第 {node.lineno} 行调用了 load_sibling：只有入口可以加载兄弟模块，'
                    f'组件需要的依赖由入口通过构造参数注入'))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split('.')[0]
                if top in local_stems:
                    problems.append(Violation(
                        'R3', where,
                        f'第 {node.lineno} 行 import 了兄弟模块 {alias.name}：插件后端由 '
                        f'importlib 按入口文件加载，目录不在 sys.path 上，这个 import '
                        f'在运行期会 ModuleNotFoundError'))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split('.')[0]
            if top in local_stems:
                problems.append(Violation(
                    'R3', where,
                    f'第 {node.lineno} 行 from {node.module} import …：插件后端由 importlib '
                    f'按入口文件加载，目录不在 sys.path 上，这个 import 在运行期会 '
                    f'ModuleNotFoundError；纯工具请放进 shell.backend 共享基建'))
    return problems


def _read_manifest(plugin_dir: Path) -> dict | None:
    """读取 manifest；坏 JSON 由 check_plugins 报，这里返回 None 表示跳过。"""
    try:
        data = json.loads((plugin_dir / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _local_module_names(plugin_dir: Path, files: list[Path]) -> set[str]:
    """插件自己的后端模块名与子目录名：非入口模块 import 它们就是"import 兄弟模块"。"""
    stems = {path.stem for path in files if path.stem != '__init__'}
    backend = plugin_dir / 'backend'
    if backend.is_dir():
        stems |= {path.name for path in backend.iterdir()
                  if path.is_dir() and not path.name.startswith('.')
                  and path.name not in SKIP_DIR_NAMES}
    return stems


def check_plugins(
    plugins_dir: Path = DEFAULT_PLUGINS_DIR,
    allowlist_dir: Path = DEFAULT_ALLOWLIST_DIR,
) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)；纯函数，供用例构造正反例。"""
    errors: list[str] = []
    warnings: list[str] = []
    plugins_dir = Path(plugins_dir)
    allowlist_dir = Path(allowlist_dir)
    if not plugins_dir.is_dir():
        return [f'插件目录不存在: {plugins_dir}'], warnings

    known: dict[str, list[Violation]] = {}
    for name in _plugin_names(plugins_dir):
        plugin_dir = plugins_dir / name
        data = _read_manifest(plugin_dir) or {}
        backend = data.get('backend') or {}
        entry_rel = backend.get('entry') if isinstance(backend, dict) else None
        entry = (plugin_dir / entry_rel).resolve() if isinstance(entry_rel, str) else None

        files = _backend_files(plugin_dir)
        local_stems = _local_module_names(plugin_dir, files)

        violations: list[Violation] = []
        if entry is None:
            warnings.append(f'[{name}] manifest 没有可用的 backend.entry，跳过 R2/R3 的入口判定')
        for path in files:
            tree = _parse(path)
            where = _rel(path, PROJECT_ROOT)
            is_entry = entry is not None and path.resolve() == entry
            if tree is not None:
                violations.extend(_check_mixins(tree, where))
                violations.extend(_check_sibling_usage(
                    tree, where, is_entry=is_entry, local_stems=local_stems))
            violations.extend(_check_line_limits(path, where, is_entry=is_entry))
        known[name] = violations

    # 豁免按插件生效：文件存在 = 该插件按长期豁免处理，整条规则跳过，但留 warning。
    for name, violations in sorted(known.items()):
        waiver = allowlist_dir / f'{name}.txt'
        if waiver.is_file():
            if not violations:
                errors.append(
                    f'[{name}] 分层豁免文件 {_rel(waiver, PROJECT_ROOT)} 已过期：'
                    f'该插件已不再违反任何分层规则，删掉这份文件')
                continue
            kinds = sorted({v.rule for v in violations})
            warnings.append(
                f'[{name}] 分层豁免生效中（{_rel(waiver, PROJECT_ROOT)}，'
                f'覆盖 {", ".join(kinds)}）：非本轮迁移范围，下次改到该插件时顺手拆并删除本文件')
            continue
        for violation in violations:
            errors.append(f'[{name}] {violation.rule} {violation.where}：{violation.message}')

    # 孤儿豁免文件：插件被删或改名后留下的文件，会让规则对"同名新插件"意外失效。
    if allowlist_dir.is_dir():
        for waiver in sorted(allowlist_dir.glob('*.txt')):
            if waiver.stem not in known:
                errors.append(
                    f'分层豁免文件 {_rel(waiver, PROJECT_ROOT)} 没有对应的插件'
                    f'（插件已删或改名）：把它删掉')

    return errors, warnings


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='校验插件后端是否按分层目标形态组织')
    parser.add_argument('--plugins-dir', type=Path, default=DEFAULT_PLUGINS_DIR)
    parser.add_argument('--allowlist-dir', type=Path, default=DEFAULT_ALLOWLIST_DIR)
    args = parser.parse_args(list(argv) if argv is not None else None)

    errors, warnings = check_plugins(args.plugins_dir, args.allowlist_dir)
    for warning in warnings:
        print(f'[warning] {warning}')
    for error in errors:
        print(f'[error] {error}')
    if errors:
        print(f'check_plugin_layering: {len(errors)} 个错误, {len(warnings)} 个警告')
        return 1
    print(f'check_plugin_layering: OK ({len(warnings)} 个警告)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
