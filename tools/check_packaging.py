"""校验 PyInstaller spec 的数据收集规则（无需真正运行 PyInstaller）。

背景：两份 spec 曾经各自内联 `collect_data_files()`，并且用
`if frontend_dist.exists():` 静默跳过前端产物 —— 干净 clone 上打包会产出一个
打开白屏的 exe，而构建日志全绿。现在规则抽到 docs/Releases/spec_common.py，
本脚本把"该失败的必须失败、该带的必须带"固化成断言，作为 CI 门禁。

运行：
    python tools/check_packaging.py
退出码 0 = 通过，1 = 失败。
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
from importlib.metadata import packages_distributions
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = PROJECT_ROOT / 'docs' / 'Releases'
if str(SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(SPEC_DIR))

for _stream in (sys.stdout, sys.stderr):
    # reconfigure 在 Python 3.7+ 的 TextIOWrapper 上存在，但静态类型 TextIO 未声明；
    # 用 getattr 取值而不是 hasattr+直接调用，既避开类型报错也保持同一语义。
    _reconfigure = getattr(_stream, 'reconfigure', None)
    if callable(_reconfigure):
        _reconfigure(encoding='utf-8', errors='replace')

SPECS = ['omnibox.spec', 'omnibox-linux.spec']

# 打包时必须带上的关键路径（相对项目根）
REQUIRED_PAYLOAD = [
    'shell/frontend/dist/index.html',
    'plugins/media-player/manifest.json',
    'plugins/media-player/backend/main.py',
    'shell/backend/file_server.py',
    # res/ 不被 Vite 处理，只由 file_server 的 /res/* 路由发布，必须单独收集；
    # 漏了它冻结后所有图标 404（界面只剩文字），而构建日志全绿。
    'res/icons/icons.svg',
]

# 绝不能进包的东西
FORBIDDEN_FRAGMENTS = [
    '__pycache__',
    '.pyc',
    '/.git/',
    '/node_modules/',
    '/data/',
    '/.config/',
]


def _fail(errors: list, msg: str) -> None:
    errors.append(msg)


def check_specs_exist(errors: list) -> None:
    for name in SPECS:
        if not (SPEC_DIR / name).is_file():
            _fail(errors, f'缺少 spec 文件: docs/Releases/{name}')


def check_specs_parse_and_share_rules(errors: list) -> None:
    """spec 必须是合法 Python，且都从 spec_common 导入收集规则（不各自内联）。"""
    for name in SPECS:
        path = SPEC_DIR / name
        if not path.is_file():
            continue
        source = path.read_text(encoding='utf-8')
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            _fail(errors, f'{name} 语法错误: {e}')
            continue
        if 'from spec_common import' not in source:
            _fail(errors, f'{name} 未从 spec_common 导入共享规则（规则会漂移）')
            continue
        if 'def collect_data_files' in source:
            _fail(errors, f'{name} 内联了 collect_data_files（应使用 spec_common）')
        # 前端产物缺失必须让构建失败，而不是静默跳过
        if 'frontend_dist.exists()' in source:
            _fail(errors, f'{name} 仍在静默跳过缺失的前端产物（会产出无 UI 的包）')

        # 静态校验 spec 引用的名字确实存在于 spec_common（防止改名后 spec 静默失效）
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == 'spec_common':
                imported |= {alias.name for alias in node.names}
        if not imported:
            _fail(errors, f'{name} 的 spec_common 导入为空')
        try:
            import spec_common
        except Exception as e:
            _fail(errors, f'无法导入 spec_common: {e}')
            continue
        for nm in sorted(imported):
            if not hasattr(spec_common, nm):
                _fail(errors, f'{name} 从 spec_common 导入了不存在的名字: {nm}')

        # spec 必须真的把收集结果用上（datas=collect_data_files(...)）
        uses_datas = any(
            isinstance(node, ast.keyword) and node.arg == 'datas'
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, 'id', '') == 'collect_data_files'
            for node in ast.walk(tree)
        )
        if not uses_datas:
            _fail(errors, f'{name} 未把 collect_data_files() 作为 Analysis 的 datas')


def _copy_minimal_project(dest: Path) -> None:
    """造一个"最小可用"的项目树：前端产物 + 插件 + 若干噪声文件。"""
    (dest / 'shell' / 'frontend' / 'dist' / 'assets').mkdir(parents=True)
    (dest / 'shell' / 'frontend' / 'dist' / 'index.html').write_text('<html></html>', encoding='utf-8')
    (dest / 'shell' / 'frontend' / 'dist' / 'assets' / 'app.js').write_text('// js', encoding='utf-8')

    plugin = dest / 'plugins' / 'demo' / 'backend'
    plugin.mkdir(parents=True)
    (dest / 'plugins' / 'demo' / 'manifest.json').write_text('{}', encoding='utf-8')
    (plugin / 'main.py').write_text('# demo', encoding='utf-8')

    # 仓库级共享资源（不被 Vite 处理，必须由 spec 单独收集）
    (dest / 'res' / 'icons').mkdir(parents=True)
    (dest / 'res' / 'icons' / 'icons.svg').write_text('<svg></svg>', encoding='utf-8')

    # 噪声：缓存目录与字节码不应进包
    cache = plugin / '__pycache__'
    cache.mkdir()
    (cache / 'main.cpython-312.pyc').write_bytes(b'\x00\x01')

    # 用户数据不应进包
    (dest / 'data').mkdir()
    (dest / 'data' / 'secret.json').write_text('{}', encoding='utf-8')
    (dest / '.config').mkdir()
    (dest / '.config' / 'auth_token.txt').write_text('tok', encoding='utf-8')

    # 隐藏文件不应进包
    (dest / 'plugins' / 'demo' / '.DS_Store').write_bytes(b'\x00')


def check_collect_data_files(errors: list) -> None:
    try:
        from spec_common import collect_data_files
    except Exception as e:  # pragma: no cover - 导入失败就是硬错误
        _fail(errors, f'无法导入 spec_common.collect_data_files: {e}')
        return

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _copy_minimal_project(root)

        try:
            datas = collect_data_files(root)
        except SystemExit as e:
            _fail(errors, f'collect_data_files 在合法项目上失败: {e}')
            return

        sources = {Path(src).resolve().as_posix() for src, _ in datas}
        root_resolved = root.resolve()
        rels = {Path(src).resolve().relative_to(root_resolved).as_posix() for src, _ in datas}

        for required in REQUIRED_PAYLOAD:
            probe = (root / required).resolve()
            if not probe.is_file():
                continue  # 最小工程里没有这个文件，跳过
            if probe.as_posix() not in sources:
                _fail(errors, f'未收集必需文件: {required}')

        if 'shell/frontend/dist/index.html' not in rels:
            _fail(errors, '前端 index.html 未被收集')
        if 'plugins/demo/backend/main.py' not in rels:
            _fail(errors, '插件 backend 未被收集')

        for rel in rels:
            for bad in FORBIDDEN_FRAGMENTS:
                if bad in rel:
                    _fail(errors, f'不该进包的文件被收集: {rel}')

        # 目标目录必须保持相对结构（plugins/demo/backend 而不是全部平铺到根）
        # PyInstaller 的 dest 是本地路径风格，统一成 / 再比较
        targets = {str(target).replace('\\', '/') for _, target in datas}
        if not any(t.endswith('plugins/demo/backend') for t in targets):
            _fail(errors, f'插件目标目录结构不对: {sorted(targets)[:5]}')

    # 缺少前端产物时必须硬失败
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / 'plugins' / 'demo').mkdir(parents=True)
        try:
            collect_data_files(root)
        except SystemExit:
            pass
        else:
            _fail(errors, '前端产物缺失时 collect_data_files 没有失败（会产出白屏包）')

    # 缺少插件目录时必须硬失败
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / 'shell' / 'frontend' / 'dist').mkdir(parents=True)
        (root / 'shell' / 'frontend' / 'dist' / 'index.html').write_text('x', encoding='utf-8')
        try:
            collect_data_files(root)
        except SystemExit:
            pass
        else:
            _fail(errors, '插件目录缺失时 collect_data_files 没有失败')


# ── HIDDEN_IMPORTS 覆盖检查 ─────────────────────────────────────────
# 背景（真实事故，v1.2.0 前）：image-viewer 与 media-player 在运行时 import
# shell.backend.tasks / shell.backend.thumb_cache，但这两个模块没写进
# HIDDEN_IMPORTS。插件后端是 importlib 动态加载的，PyInstaller 的静态分析看不到
# 它们的 import —— 冻结后的包里没有这两个模块，用户装完只剩"漫画/小说"两个插件，
# 图片浏览与媒体播放器整个不出现，而构建日志全绿。同一类坑还有：HIDDEN_IMPORTS
# 里写了某个包，但 requirements.txt 没声明 → 干净环境里装不出来，PyInstaller 只会
# 打一条 warning 然后跳过（chardet 就是这样）。
_STDLIB = set(sys.stdlib_module_names)
_NON_THIRD_PARTY_TOP = {'plugins', 'tools', 'tests', 'main'}


def _declared_distributions() -> set:
    """requirements.txt 里声明的发行包名（小写、下划线归一为连字符）。"""
    declared = set()
    for line in (PROJECT_ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^([A-Za-z0-9_.\-]+)', line)
        if m:
            declared.add(m.group(1).lower().replace('_', '-'))
    return declared


def _plugin_imports() -> dict:
    """返回 {被 import 的模块全名: 出现位置}，已滤掉标准库与插件自己的本地模块。"""
    found: dict = {}
    plugins_dir = PROJECT_ROOT / 'plugins'
    if not plugins_dir.is_dir():
        return found
    for plugin_dir in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
        local = {p.stem for p in plugin_dir.rglob('*.py')}
        local |= {p.name for p in plugin_dir.rglob('*') if p.is_dir()}
        for py in sorted(plugin_dir.rglob('*.py')):
            try:
                tree = ast.parse(py.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = py.relative_to(PROJECT_ROOT)
            for node in ast.walk(tree):
                names: list = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for full in names:
                    top = full.split('.')[0]
                    if top in _STDLIB or top in local or top in _NON_THIRD_PARTY_TOP:
                        continue
                    found.setdefault(full, rel)
    return found


def check_hidden_imports(errors: list) -> None:
    """插件用到的模块必须在 HIDDEN_IMPORTS 里，且提供它的包必须在 requirements.txt 里。"""
    from spec_common import HIDDEN_IMPORTS

    hidden = set(HIDDEN_IMPORTS)
    hidden_top = {h.split('.')[0] for h in hidden}
    imports = _plugin_imports()

    for mod, where in sorted(imports.items()):
        if not mod.startswith('shell.backend.'):
            continue
        if mod not in hidden:
            _fail(errors, f'{where} 导入了 {mod}，但 spec_common.HIDDEN_IMPORTS 里没有它'
                          f'（冻结后该插件会 ModuleNotFoundError，整个插件不加载）')

    for mod, where in sorted(imports.items()):
        if mod.startswith('shell.'):
            continue
        if mod.split('.')[0] not in hidden_top:
            _fail(errors, f'{where} 导入了第三方模块 {mod}，但 HIDDEN_IMPORTS 里没有它'
                          f'（插件是动态加载的，静态分析看不到，冻结后 import 会失败）')

    declared = _declared_distributions()
    provided = packages_distributions()
    for mod in sorted({m.split('.')[0] for m in imports} | {h.split('.')[0] for h in hidden}):
        if mod in _STDLIB:
            continue
        dists = [d.lower().replace('_', '-') for d in provided.get(mod, [])]
        if not dists:
            continue  # 本地模块 / 命名空间包：无从判断归属，交给上面的两条规则
        if not (set(dists) & declared):
            _fail(errors, f'{mod}（由 {dists[0]} 提供）没有被 requirements.txt 声明：'
                          f'干净环境里装不出来，PyInstaller 只会打 warning 然后跳过这个 hidden import')


def main() -> int:
    errors: list = []
    check_specs_exist(errors)
    check_specs_parse_and_share_rules(errors)
    check_collect_data_files(errors)
    check_hidden_imports(errors)

    if errors:
        print('check_packaging: FAILED')
        for e in errors:
            print(f'  - {e}')
        return 1
    print(f'check_packaging: OK（{len(SPECS)} 份 spec + 收集规则 + HIDDEN_IMPORTS 覆盖）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
