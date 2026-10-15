"""版本一致性检查：pyproject.toml / package.json / package-lock.json / git tag 必须同源。

为什么需要：自动打包与发布（GitHub Actions）要凭一个版本号决定产物名与 Release
标签。此前版本号散落在多处且各自漂移（`shell/frontend/package.json` 曾被误写成
3.0.0 —— 那是"Vue 3"的意思，不是版本号 —— 而 README 与 tag 是 v1.1.2），一旦
不一致就会出现"tag 是 v1.2.0、包名却是别的"。

`shell/frontend/package-lock.json` 里还有两份同源副本：根 `version` 与
`packages[""].version`。**这两份此前无人看管**：`npm ci` 校验的是依赖是否与锁文件
一致，根 `version` 不在其中 —— 实测 npm 11.13.0 在 package.json=1.0.0、锁文件
=9.9.9 时 `npm ci --offline` 退出码 0 且不回写。也就是说它只会静静地漂移，直到
下一次有人改锁文件时顺手抄错。

用法：
    python tools/check_version.py            # 检查三个文件是否同源
    python tools/check_version.py v1.2.0     # 额外校验给定 tag 与之一致（CI 用）
    python tools/check_version.py --print    # 只打印版本号（CI 拼产物名用）

退出码 0 = 一致，1 = 不一致。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

for _stream in (sys.stdout, sys.stderr):
    # reconfigure 在 Python 3.7+ 的 TextIOWrapper 上存在，但静态类型 TextIO 未声明；
    # 用 getattr 取值而不是 hasattr+直接调用，既避开类型报错也保持同一语义。
    _reconfigure = getattr(_stream, 'reconfigure', None)
    if callable(_reconfigure):
        _reconfigure(encoding='utf-8', errors='replace')

PYPROJECT = PROJECT_ROOT / 'pyproject.toml'
PACKAGE_JSON = PROJECT_ROOT / 'shell' / 'frontend' / 'package.json'
PACKAGE_LOCK = PROJECT_ROOT / 'shell' / 'frontend' / 'package-lock.json'


def read_pyproject_version(errors: list) -> str | None:
    if not PYPROJECT.is_file():
        errors.append(f'缺少 {PYPROJECT.relative_to(PROJECT_ROOT)}')
        return None
    text = PYPROJECT.read_text(encoding='utf-8')
    # 只认 [project] 段里的 version，避免误取 [tool.*] 下的同名字段
    m = re.search(r'^\[project\]\s*$(.*?)(?=^\[|\Z)', text, re.MULTILINE | re.DOTALL)
    if not m:
        errors.append('pyproject.toml 缺少 [project] 段')
        return None
    vm = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', m.group(1), re.MULTILINE)
    if not vm:
        errors.append('pyproject.toml 的 [project] 段缺少 version')
        return None
    return vm.group(1)


def read_package_version(errors: list) -> str | None:
    if not PACKAGE_JSON.is_file():
        errors.append(f'缺少 {PACKAGE_JSON.relative_to(PROJECT_ROOT)}')
        return None
    try:
        data = json.loads(PACKAGE_JSON.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        errors.append(f'package.json 解析失败: {e}')
        return None
    version = data.get('version')
    if not isinstance(version, str) or not version.strip():
        errors.append('package.json 缺少 version')
        return None
    return version


def read_lock_versions(errors: list) -> list:
    """读出 package-lock.json 里所有版本号副本，返回 [(JSON 路径, 值)]。

    两份副本都要查：npm 自己会同时写这两处，但只校验依赖，不校验它们是否与
    package.json 一致（见模块 docstring 的实测）。
    """
    if not PACKAGE_LOCK.is_file():
        errors.append(f'缺少 {PACKAGE_LOCK.relative_to(PROJECT_ROOT)}')
        return []
    try:
        data = json.loads(PACKAGE_LOCK.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        errors.append(f'package-lock.json 解析失败: {e}')
        return []

    packages = data.get('packages')
    root_entry = packages.get('') if isinstance(packages, dict) else None
    nested_version = root_entry.get('version') if isinstance(root_entry, dict) else None

    found: list = []
    for json_path, value in (('version', data.get('version')),
                             ('packages[""].version', nested_version)):
        if isinstance(value, str) and value.strip():
            found.append((json_path, value))
        else:
            errors.append(f'package-lock.json 缺少 {json_path}')
    return found


def normalize(tag: str) -> str:
    return tag.removeprefix('v')


def main(argv: list) -> int:
    args = [a for a in argv[1:] if a != '--print']
    print_only = '--print' in argv

    errors: list = []
    py_version = read_pyproject_version(errors)
    pkg_version = read_package_version(errors)
    lock_versions = read_lock_versions(errors)

    if py_version and pkg_version and py_version != pkg_version:
        errors.append(
            f'版本不一致: pyproject.toml = {py_version}, '
            f'shell/frontend/package.json = {pkg_version}'
        )

    if py_version:
        for json_path, value in lock_versions:
            if value != py_version:
                errors.append(
                    f'版本不一致: pyproject.toml = {py_version}, '
                    f'shell/frontend/package-lock.json 的 {json_path} = {value}'
                )

    if args:
        tag = args[0].strip()
        if not tag:
            errors.append('传入的 tag 为空')
        else:
            tag_version = normalize(tag)
            if py_version and tag_version != py_version:
                errors.append(
                    f'tag {tag} 与 pyproject.toml 的 version {py_version} 不一致'
                )

    if errors:
        print('check_version: FAILED', file=sys.stderr)
        for e in errors:
            print(f'  - {e}', file=sys.stderr)
        return 1

    if print_only:
        # 只输出裸版本号：CI 里用 $(...) 直接取值拼产物名
        print(py_version)
        return 0

    print(f'check_version: OK（version = {py_version}）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
