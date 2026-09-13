# -*- mode: python ; coding: utf-8 -*-
"""OmniBox PyInstaller spec（Linux）

与 omnibox.spec 的唯一实质差异是可执行文件名（OmniBox.bin，由 build-release.sh
重命名为 OmniBox）。收集规则与隐藏导入都来自同目录 spec_common.py。
"""

import sys
from pathlib import Path

_SPEC_DIR = Path(SPECPATH).resolve()
if str(_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(_SPEC_DIR))

from spec_common import HIDDEN_IMPORTS, collect_data_files, project_root

PROJECT_ROOT = project_root(SPECPATH)
DIST_DIR = PROJECT_ROOT / 'docs' / 'Releases'
BUILD_DIR = PROJECT_ROOT / '.build-linux'

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
    a.scripts,
    [],
    [],
    name='OmniBox.bin',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['python*.dll', '*.pyd'],
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
    strip=False,
    upx=True,
    upx_exclude=['python*.dll', '*.pyd'],
    name='OmniBox',
)
