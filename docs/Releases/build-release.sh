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
#   bash docs/Releases/build-release.sh                # 完整构建（需要 node/npm）
#   bash docs/Releases/build-release.sh --skip-frontend # 跳过前端构建，使用已有 dist
#
set -euo pipefail

SKIP_FRONTEND=0
for arg in "$@"; do
    case "$arg" in
        --skip-frontend) SKIP_FRONTEND=1 ;;
        *) ;;
    esac
done

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

# ── 1. 构建 Vue 前端 ──────────────────────────────────────────────
if [ "$SKIP_FRONTEND" -eq 1 ]; then
    info "跳过前端构建，使用现有 dist"
    if [ ! -f "$PROJECT_ROOT/shell/frontend/dist/index.html" ]; then
        error "跳过前端构建，但 dist/index.html 不存在"
        exit 1
    fi
else
    # 非交互 SSH 环境可能没有加载 nvm，这里主动补一下 PATH
    if ! command -v node >/dev/null 2>&1; then
        if [ -s "$HOME/.nvm/nvm.sh" ]; then
            # shellcheck disable=SC1090
            . "$HOME/.nvm/nvm.sh"
        fi
    fi
    if ! command -v node >/dev/null 2>&1; then
        for dir in "$HOME"/.nvm/versions/node/*/bin; do
            if [ -x "$dir/node" ]; then
                export PATH="$dir:$PATH"
                break
            fi
        done
    fi

    require_cmd node "Node.js 18+ (https://nodejs.org)"
    require_cmd npm  "npm 9+"

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
fi

# ── 2. 准备 Python 虚拟环境（统一入口 setup-venv.sh） ─────────────
info "准备 Python 虚拟环境..."
bash "$PROJECT_ROOT/setup-venv.sh"
PY="$VENV_DIR/bin/python"
success "Python 环境就绪"

# ── 3. 准备 PyInstaller 依赖 ─────────────────────────────────────
if ! command -v objdump >/dev/null 2>&1; then
    warn "未找到 objdump（binutils），PyInstaller 在 Linux 上需要它"
    if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
        info "检测到 root + apt-get，自动安装 binutils..."
        apt-get update -qq
        apt-get install -y -qq binutils
    else
        error "请先安装 binutils，例如：sudo apt-get install binutils"
        exit 1
    fi
fi
if ! command -v objdump >/dev/null 2>&1; then
    error "binutils 安装后仍找不到 objdump，请检查系统包管理器"
    exit 1
fi

# ── 4. PyInstaller 打包 ───────────────────────────────────────────
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
