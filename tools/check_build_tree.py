"""校验 PyInstaller 产物目录的内容是否可发布。

配套 tools/check_packaging.py：那个检查的是"收集规则对不对"，这个检查的是
"真正跑完 PyInstaller 之后，产物里到底有没有该有的东西"。

用法：
    python tools/check_build_tree.py <产物目录> [--expect-exe OmniBox.exe]

退出码 0 = 通过，1 = 失败。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    # reconfigure 在 Python 3.7+ 的 TextIOWrapper 上存在，但静态类型 TextIO 未声明；
    # 用 getattr 取值而不是 hasattr+直接调用，既避开类型报错也保持同一语义。
    _reconfigure = getattr(_stream, 'reconfigure', None)
    if callable(_reconfigure):
        _reconfigure(encoding='utf-8', errors='replace')

# 产物中必须存在的内容（相对产物根目录，用 / 分隔）
#
# 注意：内核的 Python 源码（shell/backend/*.py）**不会**以源码形式出现 ——
# PyInstaller 把它编译进可执行文件内的 PYZ 归档（配合 base_library.zip）。
# 因此这里只断言"以数据文件形式分发"的东西：前端产物 + 插件目录 + Python 运行时。
REQUIRED = [
    'shell/frontend/dist/index.html',
    'shell/frontend/dist/shell/base.js',
    'plugins/media-player/manifest.json',
    'plugins/media-player/backend/main.py',
    'base_library.zip',
]

# 产物中绝不允许出现的内容
FORBIDDEN_DIR_NAMES = {'__pycache__', 'node_modules', '.git', '.pytest_cache'}
FORBIDDEN_SUFFIXES = {'.pyc', '.pyo'}
# 用户数据/密钥不得进包
FORBIDDEN_RELATIVE = {'data', '.config'}


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(description='校验 PyInstaller 产物目录')
    parser.add_argument('dist_dir', help='PyInstaller 产物目录（COLLECT 输出）')
    parser.add_argument('--expect-exe', default=None, help='期望存在的可执行文件名')
    args = parser.parse_args(argv[1:])

    dist = Path(args.dist_dir)
    errors: list = []

    if not dist.is_dir():
        print(f'check_build_tree: FAILED\n  - 产物目录不存在: {dist}')
        return 1

    for rel in REQUIRED:
        if not (dist / rel).is_file():
            errors.append(f'产物缺少必需文件: {rel}')

    if args.expect_exe:
        exe = dist / args.expect_exe
        if not exe.is_file():
            errors.append(f'产物缺少可执行文件: {args.expect_exe}')

    for path in dist.rglob('*'):
        rel_parts = path.relative_to(dist).parts
        if path.is_dir():
            if path.name in FORBIDDEN_DIR_NAMES:
                errors.append(f'产物包含不该有的目录: {path.relative_to(dist)}')
        elif path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f'产物包含字节码文件: {path.relative_to(dist)}')
        if rel_parts and rel_parts[0] in FORBIDDEN_RELATIVE:
            errors.append(f'产物包含用户数据目录: {path.relative_to(dist)}')

    # 去重（同一个目录名可能在多个层级命中）
    errors = sorted(set(errors))

    if errors:
        print('check_build_tree: FAILED')
        for e in errors[:40]:
            print(f'  - {e}')
        if len(errors) > 40:
            print(f'  ... 另有 {len(errors) - 40} 条')
        return 1

    total_files = sum(1 for p in dist.rglob('*') if p.is_file())
    size_mb = sum(p.stat().st_size for p in dist.rglob('*') if p.is_file()) / (1024 * 1024)
    print(f'check_build_tree: OK（{total_files} 个文件，{size_mb:.1f} MB）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
