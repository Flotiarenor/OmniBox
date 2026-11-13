# -*- mode: python ; coding: utf-8 -*-
"""Cubby PyInstaller spec（Linux）

与 cubby.spec 的实质差异有两处：
  - 可执行文件名（Cubby.bin，由构建步骤重命名为 Cubby）；
  - 桌面窗口的 GTK 绑定：把 Python 绑定 `gi` + `_gi` 收进产物，GTK / WebKitGTK /
    glib / girepository 的共享库与 typelib 全部留给系统（构建容器里装好 typelib 只为
    让 PyInstaller 的 gi 钩子能解析依赖，钩子本身被排除，见 spec_common）。
收集规则与隐藏导入都来自同目录 spec_common.py。
"""

import sys
from pathlib import Path

_SPEC_DIR = Path(SPECPATH).resolve()
if str(_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(_SPEC_DIR))

from spec_common import (
    GI_REPOSITORY_EXCLUDES,
    HIDDEN_IMPORTS,
    collect_data_files,
    filter_system_data,
    filter_system_libraries,
    gi_hidden_imports,
    project_root,
    web_only_launchers,
)

PROJECT_ROOT = project_root(SPECPATH)
DIST_DIR = PROJECT_ROOT / 'docs' / 'Releases'
BUILD_DIR = PROJECT_ROOT / '.build-linux'

# 这些 GI 命名空间在运行时由系统的 typelib 解析；放进依赖图会让 PyInstaller 的
# gi 钩子把 GTK / WebKitGTK 的 typelib 与共享库链打进产物（理由见 spec_common）。
EXCLUDES = list(GI_REPOSITORY_EXCLUDES)

a = Analysis(
    [str(PROJECT_ROOT / 'main.py')],
    pathex=[],
    binaries=[],
    # 通用收集规则 + 本平台的「只跑 HTTP 服务」启动器（放到可执行文件同目录）
    datas=[*collect_data_files(PROJECT_ROOT), *web_only_launchers()],
    # 绑定只在构建环境装了 PyGObject 时才声明（容器里装了，Windows 构建机没有）
    hiddenimports=[*HIDDEN_IMPORTS, *gi_hidden_imports()],
    hookspath=[],
    hooksconfig={},
    excludes=EXCLUDES,
    runtime_hooks=[],
    noarchive=False,
)

# _gi 扩展的 DT_NEEDED 指向 libgirepository / libglib / libgobject / libgio /
# libgmodule / libgthread / libffi。这些库必须来自系统：产物目录在 RPATH 的最前面，
# 打进产物的一份会与系统 WebKitGTK 依赖的那份同时进进程，两份 glib 互不认识。
# 剔除后由 loader 在系统路径里解析，与系统 GTK 保持一致。
a.binaries = filter_system_libraries(a.binaries)

# gi 钩子即使只被 libcairo 这类外围 typelib 触发，也会把 typelib 收进 gi_typelibs/。
# typelib 必须与系统里的库版本匹配，因此一律不随产物分发。
a.datas = filter_system_data(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    [],
    name='Cubby.bin',
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
    name='Cubby',
)
