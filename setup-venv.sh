#!/usr/bin/env bash
#
# OmniBox Linux 环境统一入口
#
# 功能：
#   - 确保项目 venv 存在（不存在则用 python3 创建）
#   - 升级 pip 并从 requirements.txt 安装运行时依赖（幂等）
#   - 带 --dev 时再装 requirements-dev.txt（ruff / pyright / pyinstaller）
#
# 供 deploy / build-release.sh / CI 调用，避免各脚本各自管理环境。
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/venv"
PY="$VENV_DIR/bin/python"
REQ="$PROJECT_ROOT/requirements.txt"
REQ_DEV="$PROJECT_ROOT/requirements-dev.txt"

WITH_DEV=0
for arg in "$@"; do
    case "$arg" in
        --dev|-d) WITH_DEV=1 ;;
        *) ;;
    esac
done

ColorInfo="\033[0;36m"
ColorSuccess="\033[0;32m"
ColorWarning="\033[0;33m"
ColorError="\033[0;31m"
ColorReset="\033[0m"

info()    { echo -e "${ColorInfo}[INFO]${ColorReset} $*"; }
success() { echo -e "${ColorSuccess}[OK]${ColorReset} $*"; }
error()   { echo -e "${ColorError}[ERROR]${ColorReset} $*" >&2; }

if ! command -v python3 >/dev/null 2>&1; then
    error "未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

if [ ! -f "$REQ" ]; then
    error "未找到依赖文件: $REQ"
    exit 1
fi

if [ "$WITH_DEV" -eq 1 ] && [ ! -f "$REQ_DEV" ]; then
    error "未找到开发依赖文件: $REQ_DEV"
    exit 1
fi

if [ ! -x "$PY" ]; then
    info "未找到 venv，创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
fi

info "升级 pip..."
"$PY" -m pip install --upgrade pip -q

info "安装运行时依赖: $REQ"
"$PY" -m pip install -r "$REQ"

if [ "$WITH_DEV" -eq 1 ]; then
    info "安装开发依赖: $REQ_DEV"
    "$PY" -m pip install -r "$REQ_DEV"
fi

success "Python 环境就绪: $VENV_DIR"
