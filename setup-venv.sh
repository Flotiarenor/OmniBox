#!/usr/bin/env bash
#
# Cubby Linux 环境统一入口
#
# 功能：
#   - 确保项目 venv 存在（不存在则用 python3 创建）
#   - 校验 Python 版本落在 3.11–3.15（上界由 requirements.txt 的 pythonnet 决定）
#   - 升级 pip 并从 requirements.txt 安装运行时依赖（幂等）
#   - 带 --dev 时再装 requirements-dev.txt（ruff / pyright / pyinstaller）
#
# 供 build-release.sh 调用，避免各脚本各自管理环境。
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_ROOT/venv"
PY="$VENV_DIR/bin/python"
REQ="$PROJECT_ROOT/requirements.txt"
REQ_DEV="$PROJECT_ROOT/requirements-dev.txt"

# 与 pyproject.toml 的 requires-python、setup-venv.ps1 的版本门禁保持一致。
# requirements.txt 的 pythonnet 声明 Requires-Python >=3.11,<3.16，版本不合时在
# 装依赖之前给出可读提示，而不是让 pip 在解析 pythonnet 时报"找不到发行版"。
PY_MIN="3.11"
PY_MAX_EXCLUSIVE="3.16"

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

require_python_version() {
    local exe="$1"
    local got
    got=$("$exe" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || true)
    if ! PY_MIN="$PY_MIN" PY_MAX_EXCLUSIVE="$PY_MAX_EXCLUSIVE" "$exe" -c '
import os
import sys

low = tuple(int(part) for part in os.environ["PY_MIN"].split("."))
high = tuple(int(part) for part in os.environ["PY_MAX_EXCLUSIVE"].split("."))
sys.exit(0 if low <= sys.version_info[:2] < high else 1)
' 2>/dev/null; then
        error "需要 Python ${PY_MIN} 至 ${PY_MAX_EXCLUSIVE}（不含上界），当前 ${got:-未知}（$exe）。该区间由 requirements.txt 的 pythonnet 与 pyproject.toml 共同决定。"
        exit 1
    fi
    info "Python 版本: $got"
}

if ! command -v python3 >/dev/null 2>&1; then
    error "未找到 python3，请先安装 Python ${PY_MIN} 以上（上限 ${PY_MAX_EXCLUSIVE}，不含）"
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

require_python_version python3

if [ ! -x "$PY" ]; then
    info "未找到 venv，创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
fi

# 已存在的 venv 可能由旧版 Python 创建，版本门禁对 venv 解释器再跑一次。
require_python_version "$PY"

info "升级 pip..."
"$PY" -m pip install --upgrade pip -q

info "安装运行时依赖: $REQ"
"$PY" -m pip install -r "$REQ"

if [ "$WITH_DEV" -eq 1 ]; then
    info "安装开发依赖: $REQ_DEV"
    "$PY" -m pip install -r "$REQ_DEV"
fi

success "Python 环境就绪: $VENV_DIR"
