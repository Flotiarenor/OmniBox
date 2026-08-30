# -*- mode: python ; coding: utf-8 -*-

"""
OmniBox PyInstaller Spec
========================
策略：--onedir（目录模式）+ 模块排除 + 延迟导入 + UPX 压缩，最后用 7z 打包为便携压缩包。
"""
# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from pathlib import Path
# ── 路径（使用 PyInstaller 注入的 SPECPATH） ─────────────────────────
SPEC_DIR = Path(SPECPATH).resolve()          # spec 文件所在目录：docs/Releases
PROJECT_ROOT = SPEC_DIR.parent.parent        # 项目根目录
DIST_DIR     = PROJECT_ROOT / 'docs' / 'Releases'
BUILD_DIR    = PROJECT_ROOT / '.build'

# ── 排除完全用不到的模块 ──────────────────────────────────────────────
EXCLUDES = []

# ── 需要手动包含的隐藏模块 ──────────────────────────────────────────
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
# 插件动态加载的第三方依赖（插件 backend 由运行时 importlib 加载，需显式声明）
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

# ── 数据文件 ──────────────────────────────────────────────────────────
def collect_data_files():
    datas = []

    # 注意：不要把开发机的 .config/app.yaml 打进去。
    # frozen 运行时配置位于 <exe旁>/.config（或 APPDATA/OmniBox/.config），
    # 首次启动由 main.load_config() 生成默认 app.yaml。

    # 前端构建产物 -- 以 PROJECT_ROOT 为基准，保持完整相对路径
    frontend_dist = PROJECT_ROOT / 'shell' / 'frontend' / 'dist'
    if frontend_dist.exists():
        for f in frontend_dist.rglob('*'):
            if f.is_file():
                # shell/frontend/dist/xxx _internal/shell/frontend/dist/xxx
                rel = f.relative_to(PROJECT_ROOT)
                datas += [(str(f), str(rel.parent))]

    # 插件目录
    plugins_dir = PROJECT_ROOT / 'plugins'
    if plugins_dir.exists():
        for plugin_dir in plugins_dir.iterdir():
            if plugin_dir.is_dir():
                for f in plugin_dir.rglob('*'):
                    if f.is_file():
                        # 保持插件目录结构，例如 plugins/xxx/... → plugins/xxx/...
                        rel = f.relative_to(PROJECT_ROOT)
                        datas += [(str(f), str(rel.parent))]

    return datas

# ══════════════════════════════════════════════════════════════════════
a = Analysis(
    [str(PROJECT_ROOT / 'main.py')],
    pathex=[],
    binaries=[],
    datas=collect_data_files(),
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    excludes=EXCLUDES,
    runtime_hooks=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,          # 只包含启动脚本
    [],
    [],
    name='OmniBox.bin',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,           
    upx=True,
    upx_exclude=['python*.dll', '*.pyd'],  # 不 UPX Python DLL/PYD
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory='',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,          # 关键：不剥离 python312.dll 等二进制
    upx=True,
    upx_exclude=['python*.dll', '*.pyd'],  # 不 UPX Python DLL/PYD
    name='OmniBox',
)