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
import importlib.util
import re
import sys
import tempfile
from importlib.metadata import packages_distributions
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = PROJECT_ROOT / 'docs' / 'Releases'

for _stream in (sys.stdout, sys.stderr):
    # reconfigure 在 Python 3.7+ 的 TextIOWrapper 上存在，但静态类型 TextIO 未声明；
    # 用 getattr 取值而不是 hasattr+直接调用，既避开类型报错也保持同一语义。
    _reconfigure = getattr(_stream, 'reconfigure', None)
    if callable(_reconfigure):
        _reconfigure(encoding='utf-8', errors='replace')

# 两份 spec 的当前文件名。仓库里已没有旧名，这里只登记新名：check_specs_exist
# 对缺失文件直接判失败，spec 文件名再变时必须同步，否则门禁必红。
SPECS = ['cubby.spec', 'cubby-linux.spec']

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


def _spec_common():
    """按文件路径加载 docs/Releases/spec_common.py。

    `docs/Releases` 是构建暂存目录，不是源码树的一部分（`pyrightconfig.json` 的
    extraPaths 不再包含它）。用文件路径显式加载，让"目录语义"单一：spec_common 是
    构建脚本的共享模块，靠路径找到，不靠"某个目录恰好也是源码根"。
    """
    path = SPEC_DIR / 'spec_common.py'
    spec = importlib.util.spec_from_file_location('spec_common', path)
    if spec is None or spec.loader is None:
        raise ImportError(f'无法为 {path} 创建加载器')
    module = importlib.util.module_from_spec(spec)
    sys.modules['spec_common'] = module
    spec.loader.exec_module(module)
    return module


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
            spec_module = _spec_common()
        except Exception as e:
            _fail(errors, f'无法导入 spec_common: {e}')
            continue
        for nm in sorted(imported):
            if not hasattr(spec_module, nm):
                _fail(errors, f'{name} 从 spec_common 导入了不存在的名字: {nm}')

        # spec 必须真的把收集结果用上：datas 里既有 collect_data_files(...)，
        # 也有本平台的 web-only 启动器（少了它发布包里就没有"只跑 HTTP 服务"的入口）
        datas_value = next(
            (node.value for node in ast.walk(tree)
             if isinstance(node, ast.keyword) and node.arg == 'datas'),
            None,
        )
        if datas_value is None:
            _fail(errors, f'{name} 的 Analysis 没有 datas')
        elif not _contains_call(datas_value, 'collect_data_files'):
            _fail(errors, f'{name} 未把 collect_data_files() 作为 Analysis 的 datas')
        elif not _contains_call(datas_value, 'web_only_launchers'):
            _fail(errors, f'{name} 的 datas 没有加 web_only_launchers()'
                          f'（发布包里会缺"只跑 HTTP 服务"的启动器）')


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
        collect_data_files = _spec_common().collect_data_files
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
    """返回 {被 import 的模块全名: 出现位置}，已滤掉插件自己的本地模块。

    标准库**不**在这里滤掉：插件源码是作为数据文件随包分发的，PyInstaller 的静态
    分析看不到它们，因此"插件用到的标准库子模块到底进没进包"必须单独判
    （见 check_hidden_imports 的第三条规则）。
    """
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
                    if top in local or top in _NON_THIRD_PARTY_TOP:
                        continue
                    found.setdefault(full, rel)
    return found


def _always_importable(mod: str) -> bool:
    """模块是否由解释器自身提供（内建 / frozen）：这类恒可 import，无需收集。

    判据用 find_spec 的 origin：`sys`/`errno` 是 built-in，`os`/`io`/`importlib.util`
    在冻结产物里是 frozen —— 都会以 'built-in' / 'frozen' 作为 origin，而不是 .py 路径。
    """
    if mod.split('.')[0] in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, ValueError):
        return False
    return spec is not None and spec.origin in ('built-in', 'frozen')


def check_hidden_imports(errors: list) -> None:
    """插件用到的模块必须在 HIDDEN_IMPORTS 里，且提供它的包必须在 requirements.txt 里。"""
    HIDDEN_IMPORTS = _spec_common().HIDDEN_IMPORTS

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
        if mod.split('.')[0] in _STDLIB:
            continue
        if mod.split('.')[0] not in hidden_top:
            _fail(errors, f'{where} 导入了第三方模块 {mod}，但 HIDDEN_IMPORTS 里没有它'
                          f'（插件是动态加载的，静态分析看不到，冻结后 import 会失败）')

    # 标准库子模块：父包进了包 ≠ 子模块进了包。真实事故（v1.3.0 前）：
    # document-reader 的 documents.py 里 `from html.parser import HTMLParser`，
    # HIDDEN_IMPORTS 没有这条，冻结产物里该插件整个不加载
    # （No module named 'html.parser'）—— 而 ruff / 单测 / 构建 / check_build_tree
    # 全部通过，因为主程序的 import 图里根本没有 html.parser。
    # 只判"带点的标准库模块"：顶层标准库模块（os / re / json…）由主程序图带入，
    # 且数量大、逐个声明没有意义。**顶层标准库与第三方子模块这一层仍未覆盖**：
    # 它们同样只靠"恰好被主程序图带进来"活着，要真正兜住需要按产物判定
    # （解析产物 PYZ + base_library.zip + 落地的 C 扩展，逐模块核对插件用到的
    # import 是否都在里面），属独立的后续改动。
    for mod, where in sorted(imports.items()):
        if mod.split('.')[0] not in _STDLIB or '.' not in mod:
            continue
        if _always_importable(mod):
            continue
        if mod not in hidden:
            _fail(errors, f'{where} 导入了标准库子模块 {mod}，但 spec_common.HIDDEN_IMPORTS '
                          f'里没有它（冻结后该插件会 ModuleNotFoundError，整个插件不加载）')

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


# ── Linux 桌面窗口的打包规则 ────────────────────────────────────────
# 背景：Linux 产物要能开 GTK 窗口，必须把 Python 绑定 gi 冻结进去；而 GTK /
# WebKitGTK / glib / girepository 一律由系统提供。PyInstaller 的 gi 钩子一旦被
# gi.repository.Gtk 之类的节点触发，就会按 typelib 里的 shared-library 收集
# libgtk-3 / libwebkit2gtk 与整条依赖链（pango / harfbuzz / X11 …），还会把构建
# 容器里的 typelib 一起打进产物 —— 既越过"只带绑定"的边界，也会让构建机的 typelib
# 去驱动用户系统里版本不同的库。下面把这件事固化成断言：Linux spec 必须用
# gi_hidden_imports() 声明绑定、用 GI_REPOSITORY_EXCLUDES 挡住 gi.repository.*
# 节点、用 filter_system_libraries() 剔掉系统库；Windows spec 一条都不引入
# （Windows 走 edgechromium 后端，产物维持原样）。
# 这些库必须从产物里剔除：打进去会与系统 WebKitGTK 依赖的那份同时进进程。
SYSTEM_LIBS_MUST_GO = [
    'libgirepository-1.0.so.1',
    'libglib-2.0.so.0',
    'libgobject-2.0.so.0',
    'libgio-2.0.so.0',
    'libgmodule-2.0.so.0',
    'libgthread-2.0.so.0',
    'libffi.so.8',
    'libgtk-3.so.0',
    'libgdk-3.so.0',
    'libwebkit2gtk-4.1.so.0',
    'libjavascriptcoregtk-4.1.so.0',
    'libsoup-3.0.so.0',
]
# 这些必须留在产物里：过滤规则只能命中系统 GUI 库，不能误伤解释器与加密库。
SYSTEM_LIBS_MUST_STAY = [
    '_gi.cpython-311-x86_64-linux-gnu.so',
    'libcairo.so.2',
    'libpython3.11.so.1.0',
    'libssl.so.3',
]
# gi.repository.* 里至少这些命名空间要被排除（pywebview GTK 后端导入的，
# 以及 gi.overrides.Gdk 老分支会碰到、会把 cairo typelib 与 libcairo 带进来的）
GI_EXCLUDES_REQUIRED = [
    'gi.repository.Gtk',
    'gi.repository.WebKit2',
    'gi.repository.Soup',
    'gi.repository.cairo',
]
GI_BINDING_MODULES = ['gi', 'gi.repository', 'gi.overrides']


def _contains_call(node: ast.AST, func_name: str) -> bool:
    """AST 子树里是否调用了 func_name()（不关心调用点写在哪一层表达式里）。"""
    return any(
        isinstance(inner, ast.Call) and getattr(inner.func, 'id', '') == func_name
        for inner in ast.walk(node)
    )


def check_linux_window_packaging(errors: list) -> None:
    """Linux 窗口绑定进包、系统 GTK/glib 库不进包，且 Windows spec 不受影响。"""
    try:
        spec_module = _spec_common()
    except Exception as e:  # pragma: no cover - 导入失败就是硬错误
        _fail(errors, f'无法导入 spec_common（Linux 窗口规则）: {e}')
        return

    for name in ('gi_hidden_imports', 'filter_system_libraries', 'filter_system_data',
                 'GI_REPOSITORY_EXCLUDES'):
        if not hasattr(spec_module, name):
            _fail(errors, f'spec_common 缺少 Linux 桌面窗口规则所需的 {name}')
    if errors:
        return

    # 过滤行为：系统库全剔，其它二进制原样保留
    toc = [
        (name, f'/usr/lib/x86_64-linux-gnu/{name}', 'BINARY')
        for name in SYSTEM_LIBS_MUST_GO + SYSTEM_LIBS_MUST_STAY
    ]
    kept = {str(entry[0]) for entry in spec_module.filter_system_libraries(toc)}
    for name in SYSTEM_LIBS_MUST_GO:
        if name in kept:
            _fail(errors, f'spec_common.filter_system_libraries 没有剔除系统库 {name}'
                          f'（它必须由系统提供，否则会与系统那份 glib 同时进进程）')
    for name in SYSTEM_LIBS_MUST_STAY:
        if name not in kept:
            _fail(errors, f'spec_common.filter_system_libraries 误删了 {name}（它必须随产物分发）')

    # typelib 数据：gi 钩子收集进来的 gi_typelibs/ 必须剔除，其他数据原样保留
    datas = [
        ('gi_typelibs/cairo-1.0.typelib', '/usr/lib/girepository-1.0/cairo-1.0.typelib', 'DATA'),
        ('shell/frontend/dist/index.html',
         '/src/shell/frontend/dist/index.html', 'DATA'),
    ]
    kept_data = [str(entry[0]) for entry in spec_module.filter_system_data(datas)]
    if any(name.startswith('gi_typelibs/') for name in kept_data):
        _fail(errors, 'spec_common.filter_system_data 没有剔除 gi_typelibs/ 下的 typelib'
                      '（构建机的 typelib 会去驱动用户系统里版本不同的库）')
    if 'shell/frontend/dist/index.html' not in kept_data:
        _fail(errors, 'spec_common.filter_system_data 误删了前端产物')

    # 绑定只在构建环境真的装了 PyGObject 时才声明
    binding_modules = spec_module.gi_hidden_imports()
    if importlib.util.find_spec('gi') is None:
        if binding_modules:
            _fail(errors, f'本机没有 PyGObject 时 gi_hidden_imports() 应为空，实际 {binding_modules}')
    else:
        for name in GI_BINDING_MODULES:
            if name not in binding_modules:
                _fail(errors, f'gi_hidden_imports() 缺少 {name}')

    excludes = list(spec_module.GI_REPOSITORY_EXCLUDES)
    for name in GI_EXCLUDES_REQUIRED:
        if name not in excludes:
            _fail(errors, f'GI_REPOSITORY_EXCLUDES 缺少 {name}：它的 gi 钩子会把 GTK/WebKitGTK '
                          f'的 typelib 与共享库链打进产物')

    # Linux spec 必须真的用上这三条规则（改了 spec 却没接入会静默失效）
    linux_path = SPEC_DIR / 'cubby-linux.spec'
    windows_path = SPEC_DIR / 'cubby.spec'
    if linux_path.is_file():
        linux_source = linux_path.read_text(encoding='utf-8')
        try:
            linux_tree = ast.parse(linux_source)
        except SyntaxError as e:
            _fail(errors, f'cubby-linux.spec 语法错误: {e}')
            linux_tree = None
        if linux_tree is not None:
            hiddenimports_ok = any(
                isinstance(node, ast.keyword) and node.arg == 'hiddenimports'
                and _contains_call(node.value, 'gi_hidden_imports')
                for node in ast.walk(linux_tree)
            )
            if not hiddenimports_ok:
                _fail(errors, 'cubby-linux.spec 的 hiddenimports 没有接入 gi_hidden_imports()'
                              '（Linux 产物会开不了窗口）')
            filter_ok = any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute) and target.attr == 'binaries'
                    and isinstance(target.value, ast.Name) and target.value.id == 'a'
                    for target in node.targets
                )
                and _contains_call(node.value, 'filter_system_libraries')
                for node in ast.walk(linux_tree)
            )
            if not filter_ok:
                _fail(errors, 'cubby-linux.spec 没有用 filter_system_libraries() 过滤 a.binaries'
                              '（系统 glib/GTK 会被打进产物）')
            datas_ok = any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute) and target.attr == 'datas'
                    and isinstance(target.value, ast.Name) and target.value.id == 'a'
                    for target in node.targets
                )
                and _contains_call(node.value, 'filter_system_data')
                for node in ast.walk(linux_tree)
            )
            if not datas_ok:
                _fail(errors, 'cubby-linux.spec 没有用 filter_system_data() 过滤 a.datas'
                              '（构建机的 typelib 会被打进产物）')
        if 'GI_REPOSITORY_EXCLUDES' not in linux_source:
            _fail(errors, 'cubby-linux.spec 没有用 GI_REPOSITORY_EXCLUDES 作为 excludes'
                          '（gi 钩子会把 GTK/WebKitGTK 的 typelib 与共享库链打进产物）')
    if windows_path.is_file():
        windows_source = windows_path.read_text(encoding='utf-8')
        for name in ('gi_hidden_imports', 'filter_system_libraries', 'filter_system_data',
                     'GI_REPOSITORY_EXCLUDES'):
            if name in windows_source:
                _fail(errors, f'cubby.spec（Windows）引入了 {name}：Windows 走 edgechromium，'
                              f'产物必须保持不引入 GTK 绑定')


# ── 发布包里的 web-only 启动器 ──────────────────────────────────────
# `Cubby-Web.cmd` / `cubby-web.sh` 是"只跑 HTTP 服务"的入口，内容只有一行 exec，
# 不在运行时被 Python import —— 构建链漏收它们不会有任何测试变红（只是用户找不到
# 无头入口）。所以这里把存在性、平台归属、行尾与"只透传参数"的边界固化成断言。
WEB_ONLY_DIR = SPEC_DIR / 'web-only'
WEB_ONLY_CMD = WEB_ONLY_DIR / 'Cubby-Web.cmd'
WEB_ONLY_SH = WEB_ONLY_DIR / 'cubby-web.sh'


def check_web_only_launchers(errors: list) -> None:
    """两个启动器存在、行尾正确、只 exec 主程序，且 spec_common 按平台给出正确那份。"""
    for path in (WEB_ONLY_CMD, WEB_ONLY_SH):
        if not path.is_file():
            _fail(errors, f'缺少 web-only 启动器: {path.relative_to(PROJECT_ROOT)}')
    if not (WEB_ONLY_CMD.is_file() and WEB_ONLY_SH.is_file()):
        return

    cmd_bytes = WEB_ONLY_CMD.read_bytes()
    sh_bytes = WEB_ONLY_SH.read_bytes()
    cmd_text = cmd_bytes.decode('utf-8')
    sh_text = sh_bytes.decode('utf-8')

    # 行尾：cmd.exe 只可靠处理 CRLF；sh 带 CR 会在 Linux 上报 "not found"
    if b'\r\n' not in cmd_bytes or cmd_bytes.replace(b'\r\n', b'').count(b'\n'):
        _fail(errors, 'Cubby-Web.cmd 不是纯 CRLF 行尾')
    if b'\r' in sh_bytes:
        _fail(errors, 'cubby-web.sh 含 CR（CRLF 的 shell 脚本在 Linux 上无法执行）')

    # 边界：只允许"找同目录的主程序 + 透传参数 + --web-only"，不复制探测/降级逻辑
    for needle in ('--web-only', '%~dp0'):
        if needle not in cmd_text:
            _fail(errors, f'Cubby-Web.cmd 缺少 {needle}（要用脚本所在目录找 Cubby.exe）')
    if not sh_text.startswith('#!/bin/sh\n'):
        _fail(errors, "cubby-web.sh 首行必须是 '#!/bin/sh'")
    for needle in ('exec', '--web-only', 'dirname'):
        if needle not in sh_text:
            _fail(errors, f'cubby-web.sh 缺少 {needle}')
    for text, name in ((cmd_text, 'Cubby-Web.cmd'), (sh_text, 'cubby-web.sh')):
        # 便携模式靠可执行文件旁的 portable.txt 判定，cwd 必须保持调用方语义
        if 'cd ' in text:
            _fail(errors, f'{name} 里出现 cd：会改变调用方的 cwd（便携模式靠 exe 旁的 portable.txt）')

    # spec_common.web_only_launchers() 必须按平台给出对应的那一份
    try:
        getter = _spec_common().web_only_launchers
    except Exception as e:  # pragma: no cover - 导入/属性缺失都是硬错误
        _fail(errors, f'spec_common 缺少 web_only_launchers(): {e}')
        return

    current = WEB_ONLY_CMD if sys.platform.startswith('win') else WEB_ONLY_SH
    for platform_name, expected in (('win32', WEB_ONLY_CMD), ('linux', WEB_ONLY_SH),
                                    (None, current)):
        try:
            entries = getter(platform_name) if platform_name is not None else getter()
        except Exception as e:
            _fail(errors, f'web_only_launchers({platform_name!r}) 调用失败: {e}')
            continue
        # 目标目录必须是 '.'：PyInstaller 6 用 `.` 表示产物根目录，空串会被
        # `Invalid (SRC, DEST_DIR) tuple` 直接拒绝（真实构建里踩过）。
        if len(entries) != 1 or str(entries[0][1]) != '.':
            _fail(errors, f'web_only_launchers({platform_name!r}) 应只返回一项且目标目录为 "."，'
                          f'实际 {entries}')
            continue
        source = Path(str(entries[0][0]))
        if not source.is_file() or source.resolve() != expected.resolve():
            _fail(errors, f'web_only_launchers({platform_name!r}) 应指向 '
                          f'{expected.relative_to(PROJECT_ROOT)}，实际 {entries[0][0]}')


def main() -> int:
    errors: list = []
    check_specs_exist(errors)
    check_specs_parse_and_share_rules(errors)
    check_collect_data_files(errors)
    check_hidden_imports(errors)
    check_linux_window_packaging(errors)
    check_web_only_launchers(errors)

    if errors:
        print('check_packaging: FAILED')
        for e in errors:
            print(f'  - {e}')
        return 1
    print(f'check_packaging: OK（{len(SPECS)} 份 spec + 收集规则 + HIDDEN_IMPORTS 覆盖 + '
          f'Linux 窗口绑定/系统库规则 + web-only 启动器）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
