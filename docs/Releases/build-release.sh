#!/usr/bin/env bash
#
# OmniBox Linux 一键发布构建脚本
#
# 功能：
#   1. 构建 Vue 前端
#   2. 使用 PyInstaller 打包 Linux 可执行文件
#   3. 压缩为 tar.gz 便携包
#
# 用法：
#   bash docs/Releases/build-release.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SPEC_FILE="$SCRIPT_DIR/omnibox-linux.spec"
BUILD_DIR="$PROJECT_ROOT/.build-linux"
DIST_DIR="$SCRIPT_DIR"
VENV_DIR="$PROJECT_ROOT/venv"

ColorInfo="\033[0;36m"
ColorSuccess="\033[0;32m"
ColorWarning="\033[0;33m"
ColorError="\033[0;31m"
ColorReset="\033[0m"

info()    { echo -e "${ColorInfo}[INFO]${ColorReset} $*"; }
success() { echo -e "${ColorSuccess}[OK]${ColorReset} $*"; }
warn()    { echo -e "${ColorWarning}[WARN]${ColorReset} $*"; }
error()   { echo -e "${ColorError}[ERROR]${ColorReset} $*" >&2; }

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        error "未找到 $1，请先安装：$2"
        exit 1
    fi
}

require_cmd node "Node.js 18+ (https://nodejs.org)"
require_cmd npm  "npm 9+"
require_cmd python3 "Python 3.10+"

# ── 1. 构建 Vue 前端 ──────────────────────────────────────────────
info "构建 Vue 前端..."
if [ ! -f "$PROJECT_ROOT/shell/frontend/package.json" ]; then
    error "前端目录不存在: $PROJECT_ROOT/shell/frontend"
    exit 1
fi
(
    cd "$PROJECT_ROOT/shell/frontend"
    npm install --silent
    npm run build
)
if [ ! -f "$PROJECT_ROOT/shell/frontend/dist/index.html" ]; then
    error "前端构建失败：dist/index.html 不存在"
    exit 1
fi
success "前端构建完成"

# ── 2. 准备 Python 虚拟环境 ───────────────────────────────────────
if [ ! -x "$VENV_DIR/bin/python" ]; then
    info "未找到 venv，创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
fi
PY="$VENV_DIR/bin/python"

if ! "$PY" -c "import flask, yaml, PyInstaller" >/dev/null 2>&1; then
    info "安装 Python 依赖（首次构建需要较长时间）..."
    "$PY" -m pip install --upgrade pip
    "$PY" -m pip install -r "$PROJECT_ROOT/requirements.txt"
fi
success "Python 环境就绪"

# ── 3. PyInstaller 打包 ───────────────────────────────────────────
info "PyInstaller 打包 Linux 版本..."
rm -rf "$BUILD_DIR" "$DIST_DIR/OmniBox"
"$PY" -m PyInstaller \
    --workpath "$BUILD_DIR" \
    --distpath "$DIST_DIR" \
    --noconfirm \
    --log-level WARN \
    "$SPEC_FILE"

BIN_TMP="$DIST_DIR/OmniBox/OmniBox.bin"
BIN="$DIST_DIR/OmniBox/OmniBox"
if [ ! -f "$BIN_TMP" ]; then
    error "PyInstaller 输出不存在: $BIN_TMP"
    exit 1
fi
mv "$BIN_TMP" "$BIN"
chmod +x "$BIN"
# PyInstaller 会在 COLLECT 外留下一个多余的可执行文件，清理掉
rm -f "$DIST_DIR/OmniBox.bin"

SIZE_MB=$(du -sm "$DIST_DIR/OmniBox" | cut -f1)
success "PyInstaller 打包完成：$DIST_DIR/OmniBox/ (${SIZE_MB}MB)"

# ── 4. 压缩为 tar.gz ──────────────────────────────────────────────
info "创建 tar.gz 压缩包..."
DATE=$(date +%Y%m%d)
ARCHIVE="$DIST_DIR/OmniBox_${DATE}.tar.gz"
rm -f "$ARCHIVE"
tar -C "$DIST_DIR" -czf "$ARCHIVE" OmniBox

ARCHIVE_MB=$(du -sm "$ARCHIVE" | cut -f1)
success "压缩完成：$ARCHIVE (${ARCHIVE_MB}MB)"

echo
echo -e "${ColorSuccess}==============================================${ColorReset}"
echo -e "${ColorSuccess}  BUILD COMPLETE${ColorReset}"
echo -e "${ColorSuccess}==============================================${ColorReset}"
echo "输出目录: $DIST_DIR/OmniBox/"
echo "压缩包  : $ARCHIVE"
echo
