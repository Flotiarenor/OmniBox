# -*- mode: python ; coding: utf-8 -*-
"""两个 PyInstaller spec 共享的收集逻辑（Windows / Linux）。

为什么单独抽一个文件：cubby.spec 与 cubby-linux.spec 原本各自复制了一份
`collect_data_files()` / `HIDDEN_IMPORTS`，任何一处改动都容易只改一半（实际已经
出现过：两份的插件收集逻辑都缺过滤）。抽出来后规则只有一份。

spec 文件顶部只要：

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(SPECPATH).resolve()))
    from spec_common import HIDDEN_IMPORTS, collect_data_files, project_root

设计约束（都是踩过的坑）：
  - **前端产物必须存在**：以前 `if frontend_dist.exists():` 会在没有前端时静默跳过，
    产出一个打开白屏的 exe 而构建过程全绿。现在改为直接抛异常，强制失败。
  - **不打 __pycache__/.pyc**：以前 `rglob('*')` 会把插件的字节码缓存一起打进包。
  - **不打运行时数据**：`data/`、`.config/`、`logs/` 属于用户数据，不进发行包。
"""

import fnmatch
import importlib.util
import sys
from pathlib import Path

# ── 需要手动包含的隐藏模块 ──────────────────────────────────────────
# 插件后端由运行时 importlib 动态加载，静态分析看不到这些依赖，必须显式声明。
#
# 规则：插件源码里出现的每一个 shell.backend.* 与第三方模块都要在这里有一份 ——
# 漏掉的表现是"装好后那个插件整个不出现"（运行时 ModuleNotFoundError），构建日志
# 却是全绿的。真实事故：shell.backend.tasks / shell.backend.thumb_cache 漏在这里，
# 只有 image-viewer 与 media-player 用它们，于是用户装完只剩漫画和文档两个插件。
# tools/check_packaging.py 现在会把这条规则当作门禁检查。
HIDDEN_IMPORTS = [
    'shell.backend.paths',
    'shell.backend.plugin_base',
    'shell.backend.plugin_utils',
    'shell.backend.media_catalog',
    'shell.backend.plugin_manager',
    'shell.backend.settings_store',
    # 插件共享基建：只有 image-viewer / media-player 会 import
    'shell.backend.tasks',
    'shell.backend.thumb_cache',
    # 组件化之后插件后端的共用底座：组件只 import shell.backend（不再互相 load_sibling），
    # 因此这些模块必须显式声明，否则冻结后组件 import 失败、插件整体不加载。
    # 迁移配方见 docs/plugin-backend-layout.md。
    'shell.backend.json_cache',
    'shell.backend.ports',
    # 统一刷新基建（同步 / 校验）：插件声明 freshness_spec() 由壳侧驱动，
    # 但插件自己的兼容入口会 import 它取引擎（image-viewer 的 refresh()），
    # 将来插件直接声明 FsSource 也要能 import
    'shell.backend.freshness',
    'shell.backend.file_server',
    # ── group-mesh 协议内核（shell/groupmesh）──
    'shell.groupmesh',
    'shell.groupmesh.crypto_prims',
    'shell.groupmesh.records',
    'shell.groupmesh.identity',
    'shell.groupmesh.roster',
    'shell.groupmesh.roster_history',
    'shell.groupmesh.shares',
    'shell.groupmesh.registry',
    'shell.groupmesh.noise',
    'shell.groupmesh.secret_store',
    'shell.groupmesh.transport',
    'shell.groupmesh.node',
    'shell.groupmesh.client',
    'flask.json.provider',
    'werkzeug.serving',
    'jinja2.ext',              # Flask 内部依赖
    'markupsafe._native',
    # 插件动态加载的第三方依赖
    'mutagen',
    'PIL',
    'natsort',
    'chardet',
    # document-reader 的 Markdown 渲染（requirements.txt 里与 mdurl 一起声明）
    'markdown_it',
    # document-reader 的正文抽取（documents.py：from html.parser import HTMLParser）
    # 与元数据解析（urllib.parse / xml.etree.ElementTree）。
    # 标准库子模块也要声明：插件源码是作为数据文件随包分发的，PyInstaller 的静态分析
    # 完全看不到它；而"父包 html 进了包"不等于"子模块 html.parser 进了包"。
    # 后两条眼下虽被主程序图顺带带入，但那是**偶然**（requests 用了 urllib.parse），
    # 依赖一旦变化就会静默失效 —— html.parser 正是这么漏的（漏掉的表现是
    # document-reader 整个不加载：No module named 'html.parser'）。
    'html.parser',
    'urllib.parse',
    'xml.etree.ElementTree',
    # document-reader 的朗读引擎：插件后端 importlib 动态加载，静态分析看不到；
    # 漏在这里的表现是"冻结后点朗读报没有可用引擎"（tts_engine 里是函数内 import）
    'edge_tts',
    # manga-library 的禁漫接口客户端（插件内自带实现）
    'requests',
    'Crypto',
    'Crypto.PublicKey._point',
    # group-mesh：Noise 实现与私钥保护
    'noise',
    'noise.backends.default',
    'cryptography',
    'keyring',
    'concurrent.futures',
    'concurrent.futures.thread',
    'sqlite3',
]

# ── Linux 桌面窗口：PyGObject 绑定（gi + _gi）────────────────────────
# 窗口由 pywebview 的 GTK 后端提供，它要 import gi（Python 绑定）。PyInstaller 的
# 静态分析看不到窗口后端的选择：pywebview 在 guilib.initialize() 里逐个 try 导入
# 平台模块，Linux 分支才轮到 GTK。因此 Linux 构建必须显式声明 gi；漏掉的表现是
# 产物能起 --web-only，开窗时报 "GTK cannot be loaded"。
#
# 只在构建环境真的装了 PyGObject 时才声明：Windows 构建机不装它（Windows 走
# edgechromium 后端），这里返回空列表，Windows 产物不引入绑定。Linux 构建在
# debian:12 容器里先 pip 装 PyGObject，_gi 扩展就是对容器里那份 CPython 编译的，
# 与冻结进产物的解释器 ABI 一致。
#
# GI_OVERRIDE_MODULES 与 GI_REPOSITORY_EXCLUDES 要成对理解：前者是 gi 包里的
# 纯 Python 便捷封装（gi 钩子本来会按 typelib 依赖自动补上，排除钩子后得自己登记），
# 后者是运行时才由系统 typelib 解析的命名空间；系统库靠排除 + 过滤两条规则挡在
# 产物外。
GI_OVERRIDE_MODULES = (
    'gi.overrides.GObject',
    'gi.overrides.Gio',
    'gi.overrides.GLib',
    'gi.overrides.Gdk',
    'gi.overrides.Gtk',
    'gi.overrides.Pango',
    'gi.overrides.GdkPixbuf',
)


def gi_hidden_imports() -> list:
    """构建环境有 PyGObject 时，PyInstaller 需要显式收集的模块。"""
    if importlib.util.find_spec('gi') is None:
        return []
    return ['gi', 'gi.repository', 'gi.overrides', *GI_OVERRIDE_MODULES]


# 分析阶段就排除的 GI 命名空间。它们一旦进入依赖图，PyInstaller 的 gi 钩子会按
# typelib 里记录的 shared-library 把 libgtk-3、libwebkit2gtk 连同整条依赖链
# （pango / harfbuzz / X11 …）收进产物，还会把构建容器里的 typelib 一并打进去。
# 本项目的约束是 GTK / WebKitGTK / glib / girepository 一律由系统提供，产物里只放
# Python 绑定，因此这些命名空间只允许在**运行时**由 gi 的 DynamicImporter 从系统
# typelib 加载，分析阶段不收集 —— 排除它们就等于让 gi 钩子没有触发点。
# 覆盖的是 pywebview GTK 后端导入的命名空间；窗口后端换成别的库时这里要同步。
GI_REPOSITORY_EXCLUDES = (
    'gi.repository.Gtk',
    'gi.repository.Gdk',
    'gi.repository.GLib',
    'gi.repository.Gio',
    'gi.repository.GObject',
    'gi.repository.WebKit2',
    'gi.repository.Soup',
    # gi.overrides.Gdk 里有一条老分支会 import 它（Typelib 里没有 Gdk.Rectangle
    # 时才真正执行，但静态分析看得到）。不排除的话 PyInstaller 会按 cairo 的
    # typelib 把 libcairo.so.2 / libcairo-gobject 与构建机的 cairo-1.0.typelib
    # 一起收进产物。
    'gi.repository.cairo',
)

# 绝不能随产物分发的数据目录前缀（按产物内相对路径匹配）。
# gi 钩子把 typelib 收集到 gi_typelibs/，而 typelib 必须与它描述的库版本匹配：
# 构建容器里的那份落到版本不同的系统上，取 API 时会错位。产物只带 Python 绑定，
# typelib 由用户系统的 girepository 默认搜索路径提供。
SYSTEM_DATA_PREFIXES = ('gi_typelibs/',)

# 必须由系统 dlopen、绝不能进产物的共享库（按产物内 basename 通配）。
# 前 7 个是 glib 家族：同一进程里出现两份 glib 时，GType 与信号注册表互相不认识，
# 表现为开窗后随机崩溃或白屏，GLib 明确不支持这种用法。GTK / WebKitGTK 同理必须
# 与用户系统里的那份一致；把它们一并列出，是为了在 PyInstaller 的 gi 钩子行为变化
# （重新按 typelib 收集共享库）时产物仍然守住"只带 Python 绑定"的边界。
SYSTEM_LIBRARY_PATTERNS = (
    'libgirepository*',        # _gi 的链接依赖：GI 运行时
    'libglib-2.0*',
    'libgobject-2.0*',
    'libgio-2.0*',
    'libgmodule-2.0*',
    'libgthread-2.0*',
    'libffi*',                 # libgirepository 的依赖
    'libgtk-3*',
    'libgdk-3*',
    'libwebkit2gtk-*',
    'libjavascriptcoregtk-*',
    'libsoup-*',
)


def filter_system_libraries(binaries: list) -> list:
    """从 PyInstaller 的 binaries TOC 里剔除 SYSTEM_LIBRARY_PATTERNS 命中的库。

    TOC 每项的第一个元素是产物内相对路径（PyInstaller 6 的三元组），按 basename
    匹配即可，不需要关心它是平铺在根目录还是收在子目录里。返回新的列表，不就地改。
    """
    return [
        entry for entry in binaries
        if not any(
            fnmatch.fnmatch(_basename(str(entry[0])), pattern)
            for pattern in SYSTEM_LIBRARY_PATTERNS
        )
    ]


def _basename(dest: str) -> str:
    """产物内路径的 basename；PyInstaller 在 Windows 上可能给反斜杠。"""
    return dest.replace('\\', '/').rsplit('/', 1)[-1]


def filter_system_data(datas: list) -> list:
    """剔除 SYSTEM_DATA_PREFIXES 命中的数据（gi 钩子收集进来的 typelib）。"""
    return [
        entry for entry in datas
        if not str(entry[0]).replace('\\', '/').startswith(SYSTEM_DATA_PREFIXES)
    ]


# ── 发布包里的「只跑 HTTP 服务」启动器 ───────────────────────────────
# 主程序默认会尝试开桌面窗口；有些场景（NAS / 无头服务器 / 只用浏览器访问）需要
# 一个"只起服务"的入口。两个脚本只做一件事：exec 主程序并透传调用方的参数 +
# --web-only。刻意不加 cd、不探测、不复制降级逻辑 —— 降级判定属于 main.py，
# 抄一份到这里就会与主程序漂移；也不改 cwd，便携模式靠 exe 旁的 portable.txt
# 判定，cwd 必须保持调用方的语义。
# 只随对应平台的产物分发：Windows 给 .cmd，Linux 给 .sh。路径按本文件位置算，
# 不依赖 SPECPATH，两个 spec 调用时不需要传参。
_WEB_ONLY_DIR = Path(__file__).resolve().parent / 'web-only'


def web_only_launchers(platform_name: str | None = None) -> list:
    """当前平台要随包分发的启动器，PyInstaller datas 格式：[(源文件, '.')]。

    目标目录必须是 '.'：PyInstaller 6 用 `.` 表示"产物根目录"，空串会被直接拒绝
    （`Invalid (SRC, DEST_DIR) tuple ... Empty DEST_DIR is not allowed`）—— 这条是
    真实构建里暴露出来的，不是风格选择。放到根目录是为了让启动器与可执行文件同目录：
    `Cubby-Web.cmd` 靠 `%~dp0`、`cubby-web.sh` 靠 `dirname $0` 找到主程序。
    platform_name 仅供门禁脚本验证另一平台的分支，构建时不要传。
    """
    name = sys.platform if platform_name is None else platform_name
    launcher = 'Cubby-Web.cmd' if name.startswith('win') else 'cubby-web.sh'
    return [(str(_WEB_ONLY_DIR / launcher), '.')]


# 收集时要跳过的目录名 / 后缀（构建噪声与用户数据）
_SKIP_DIR_NAMES = {'__pycache__', '.git', '.cache', '.pytest_cache', '.ruff_cache', '.mypy_cache', '.dsh'}
_SKIP_SUFFIXES = {'.pyc', '.pyo', '.pyd', '.log', '.tmp', '.orig', '.rej'}


def project_root(specpath) -> Path:
    """SPECPATH 指向 docs/Releases，项目根是它往上两级。"""
    return Path(specpath).resolve().parent.parent


def _iter_files(base: Path, root: Path):
    """递归产出 base 下应当打包的文件（相对 root 的路径）。

    跳过 __pycache__ 等缓存目录与 .pyc/.log 等噪声文件；跳过以 . 开头的隐藏路径。
    """
    for path in sorted(base.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if any(part in _SKIP_DIR_NAMES or part.startswith('.') for part in rel.parts[:-1]):
            continue
        if rel.name.startswith('.') or path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        yield path, path.relative_to(root)


def collect_data_files(root: Path):
    """收集需要随包分发的前端产物与插件目录。

    root 为项目根目录。返回 PyInstaller 的 datas 列表 [(源文件, 目标相对目录)]。
    """
    root = Path(root).resolve()
    datas = []

    # ── 前端构建产物（必须存在，否则产出的 exe 无 UI）──
    frontend_dist = root / 'shell' / 'frontend' / 'dist'
    index_html = frontend_dist / 'index.html'
    if not index_html.is_file():
        raise SystemExit(
            f'[spec] 前端产物缺失: {index_html}\n'
            f'       必须先构建前端（npm ci && npm run build）再打包；\n'
            f'       否则会静默产出一个打开即白屏的安装包。'
        )
    for src, rel in _iter_files(frontend_dist, root):
        datas.append((str(src), str(rel.parent)))

    # ── 仓库级共享资源（res/：目前是图标 sprite）──
    # 与 /shell/* 不同：这批不被 Vite 处理，只由 file_server.py 的 /res/* 路由发布，
    # 所以必须单独收集，否则冻结后图标一律 404（界面只剩文字）。
    res_dir = root / 'res'
    if res_dir.is_dir():
        for src, rel in _iter_files(res_dir, root):
            datas.append((str(src), str(rel.parent)))

    # ── 插件目录（保持 plugins/<name>/... 结构）──
    plugins_dir = root / 'plugins'
    if not plugins_dir.is_dir():
        raise SystemExit(f'[spec] 插件目录不存在: {plugins_dir}')
    for src, rel in _iter_files(plugins_dir, root):
        datas.append((str(src), str(rel.parent)))

    if not datas:
        raise SystemExit('[spec] datas 为空，打包结果将不可用')
    return datas
