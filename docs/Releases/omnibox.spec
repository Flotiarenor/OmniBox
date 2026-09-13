# -*- mode: python ; coding: utf-8 -*-
"""OmniBox PyInstaller spec（Windows）

策略：--onedir（目录模式）+ 隐藏导入声明 + 可选 UPX，最后用 7z/zip 打包为便携压缩包。
收集规则见同目录 spec_common.py（两个平台共用一份，避免规则漂移）。
"""

import sys
from pathlib import Path

# spec 目录（docs/Releases）加入 sys.path，才能 import 同目录的 spec_common
_SPEC_DIR = Path(SPECPATH).resolve()
if str(_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(_SPEC_DIR))

from spec_common import HIDDEN_IMPORTS, collect_data_files, project_root

PROJECT_ROOT = project_root(SPECPATH)
DIST_DIR = PROJECT_ROOT / 'docs' / 'Releases'
BUILD_DIR = PROJECT_ROOT / '.build'

# 完全用不到的模块（留空：目前没有可靠证据表明能安全排除谁）
EXCLUDES = []

a = Analysis(
    [str(PROJECT_ROOT / 'main.py')],
    pathex=[],
    binaries=[],
    datas=collect_data_files(PROJECT_ROOT),
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
    a.scripts,              # 只包含启动脚本
    [],
    [],
    name='OmniBox',
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
    strip=False,            # 关键：不剥离 python312.dll 等二进制
    upx=True,
    upx_exclude=['python*.dll', '*.pyd'],
    name='OmniBox',
)
