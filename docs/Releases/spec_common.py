# -*- mode: python ; coding: utf-8 -*-
"""两个 PyInstaller spec 共享的收集逻辑（Windows / Linux）。

为什么单独抽一个文件：omnibox.spec 与 omnibox-linux.spec 原本各自复制了一份
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

from pathlib import Path

# ── 需要手动包含的隐藏模块 ──────────────────────────────────────────
# 插件后端由运行时 importlib 动态加载，静态分析看不到这些依赖，必须显式声明。
HIDDEN_IMPORTS = [
    'shell.backend.paths',
    'shell.backend.plugin_base',
    'shell.backend.plugin_utils',
    'shell.backend.media_catalog',
    'shell.backend.plugin_manager',
    'shell.backend.settings_store',
    'shell.backend.file_server',
    'flask.json.provider',
    'werkzeug.serving',
    'jinja2.ext',              # Flask 内部依赖
    'markupsafe._native',
    'yaml.cyaml',
    # 插件动态加载的第三方依赖
    'mutagen',
    'PIL',
    'natsort',
    'chardet',
    'jmcomic',
    'common',
    'curl_cffi',
    'requests',
    'Crypto',
    'concurrent.futures',
    'concurrent.futures.thread',
    'sqlite3',
]

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

    # ── 插件目录（保持 plugins/<name>/... 结构）──
    plugins_dir = root / 'plugins'
    if not plugins_dir.is_dir():
        raise SystemExit(f'[spec] 插件目录不存在: {plugins_dir}')
    for src, rel in _iter_files(plugins_dir, root):
        datas.append((str(src), str(rel.parent)))

    if not datas:
        raise SystemExit('[spec] datas 为空，打包结果将不可用')
    return datas
