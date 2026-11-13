#!/usr/bin/env bash
#
# Cubby Linux 一键发布构建脚本
#
# 功能：
#   1. 构建 Vue 前端
#   2. 使用 PyInstaller 打包 Linux 可执行文件
#   3. 压缩为 tar.gz 便携包
#
# 用法：
#   bash docs/Releases/build-release.sh                # 完整构建（需要 node/npm）
#   bash docs/Releases/build-release.sh --skip-frontend # 跳过前端构建，使用已有 dist
#   bash docs/Releases/build-release.sh --clean-user-data
#       打包前删掉产物目录里的 data/ .config/ logs/。2.0 起默认数据根不在产物目录里
#       （见 shell/backend/paths.py：装好的程序写到用户数据根），保留这项检查是因为
#       两个入口仍会把数据写进产物目录 —— 1.x 旧产物残留、以及便携模式（exe 旁放了
#       portable.txt）。默认不删也不放行：发现残留直接中止打包。
#
set -euo pipefail

SKIP_FRONTEND=0
CLEAN_USER_DATA=0
for arg in "$@"; do
    case "$arg" in
        --skip-frontend) SKIP_FRONTEND=1 ;;
        --clean-user-data) CLEAN_USER_DATA=1 ;;
        *) ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SPEC_FILE="$SCRIPT_DIR/cubby-linux.spec"
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
        npm ci --silent
        npm run build
    )
    if [ ! -f "$PROJECT_ROOT/shell/frontend/dist/index.html" ]; then
        error "前端构建失败：dist/index.html 不存在"
        exit 1
    fi
    success "前端构建完成"
fi

# ── 2. 准备 Python 虚拟环境（统一入口 setup-venv.sh，--dev 含 pyinstaller） ──
info "准备 Python 虚拟环境..."
bash "$PROJECT_ROOT/setup-venv.sh" --dev
PY="$VENV_DIR/bin/python"
success "Python 环境就绪"

# ── 2.4 桌面窗口的 Python 绑定（PyGObject）───────────────────────────
# 窗口由 pywebview 的 GTK 后端提供，它 import 的 gi 是 Python 绑定：绑定必须打进
# 产物，GTK / WebKitGTK / glib / girepository 则由运行时的系统提供（见
# cubby-linux.spec 与 spec_common.py 的剔除规则）。PyGObject 只发源码包，要在本机
# 编译，所以除 pip 之外还要 C 工具链、ninja、gobject-introspection 与 cairo 的开发包。
# 版本要与发布链一致（.github/workflows/release.yml 同一个 pin）：bookworm 的 glib
# 是 2.74、gobject-introspection 是 1.74，只支持到 3.48.x；3.50 起改用
# girepository-2.0（已并入 glib，要求 glib >= 2.80），在那里编不过。
PYGOBJECT_VERSION="3.48.2"

pygobject_system_deps=()
if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1; then
    pygobject_system_deps+=(build-essential)
fi
command -v ninja >/dev/null 2>&1 || pygobject_system_deps+=(ninja-build)
if ! command -v pkg-config >/dev/null 2>&1; then
    # 没有 pkg-config 就无法判断下面两个开发包在不在，一并补上
    pygobject_system_deps+=(pkg-config libgirepository1.0-dev libcairo2-dev)
else
    pkg-config --exists gobject-introspection-1.0 || pygobject_system_deps+=(libgirepository1.0-dev)
    pkg-config --exists cairo || pygobject_system_deps+=(libcairo2-dev)
fi
PY_INCLUDE=$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
if [ ! -f "$PY_INCLUDE/Python.h" ]; then
    pygobject_system_deps+=(python3-dev)
fi

if [ "${#pygobject_system_deps[@]}" -gt 0 ]; then
    if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
        info "安装 PyGObject 的编译依赖: ${pygobject_system_deps[*]}"
        apt-get update -qq
        apt-get install -y -qq "${pygobject_system_deps[@]}" gir1.2-gtk-3.0 gir1.2-webkit2-4.1
    else
        error "缺少 PyGObject 的编译依赖: ${pygobject_system_deps[*]}"
        error "gi 是产物里唯一的窗口绑定，缺它打出来的包在任何机器上都开不了窗。"
        error "Debian/Ubuntu 先执行：sudo apt-get install ${pygobject_system_deps[*]} gir1.2-gtk-3.0 gir1.2-webkit2-4.1"
        error "Fedora/Arch 装同名开发包（gobject-introspection-devel、cairo-devel、gtk3、webkit2gtk-4.1）后重试。"
        exit 1
    fi
fi

info "安装 PyGObject $PYGOBJECT_VERSION（窗口的 Python 绑定）..."
"$PY" -m pip install "PyGObject==$PYGOBJECT_VERSION"
if ! "$PY" -c "import gi" >/dev/null 2>&1; then
    error "PyGObject 安装后仍 import 失败：产物不会含 gi，桌面窗口起不来"
    exit 1
fi
if ! "$PY" -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk" >/dev/null 2>&1; then
    warn "本机缺 GTK typelib（Debian/Ubuntu: gir1.2-gtk-3.0）：产物仍能打出，"
    warn "但只在装了 GTK/WebKitGTK 的系统上开窗（发布产物同样如此）"
fi
success "PyGObject 就绪"

# ── 2.5 记录依赖快照（仅用于审计；requirements.txt 是手写声明，绝不被覆盖） ──
info "生成依赖快照 requirements.lock.txt..."
"$PY" -m pip freeze > "$PROJECT_ROOT/requirements.lock.txt"
success "requirements.lock.txt 已更新"

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
rm -rf "$BUILD_DIR" "$DIST_DIR/Cubby"
"$PY" -m PyInstaller \
    --workpath "$BUILD_DIR" \
    --distpath "$DIST_DIR" \
    --noconfirm \
    --log-level WARN \
    "$SPEC_FILE"

BIN_TMP="$DIST_DIR/Cubby/Cubby.bin"
BIN="$DIST_DIR/Cubby/Cubby"
if [ ! -f "$BIN_TMP" ]; then
    error "PyInstaller 输出不存在: $BIN_TMP"
    exit 1
fi
mv "$BIN_TMP" "$BIN"
chmod +x "$BIN"

# 「只跑 HTTP 服务」的启动器：spec 里由 spec_common.web_only_launchers() 收集，
# datas 拷贝不保留可执行位（与 CI 打包步骤做同一件事），漏收时直接失败而不是发一个
# 找不到无头入口的包。
WEB_LAUNCHER="$DIST_DIR/Cubby/cubby-web.sh"
if [ ! -f "$WEB_LAUNCHER" ]; then
    error "产物里没有 cubby-web.sh：spec_common.web_only_launchers() 没生效"
    exit 1
fi
chmod +x "$WEB_LAUNCHER"
# PyInstaller 会在 COLLECT 外留下一个多余的可执行文件，清理掉
rm -f "$DIST_DIR/Cubby.bin"

SIZE_MB=$(du -sm "$DIST_DIR/Cubby" | cut -f1)
success "PyInstaller 打包完成：$DIST_DIR/Cubby/ (${SIZE_MB}MB)"

# ── 4.5 打包前把关：产物目录里绝不能带用户数据 ─────────────────────
# 2.0 起默认数据根本不在产物目录里（shell/backend/paths.py 把数据放到用户数据根），
# 但两处入口仍会往产物目录写东西：1.x 遗留的 data/ 与 .config/，以及便携模式
# （exe 旁放了 portable.txt）跑过一次之后。这里因此继续把关。
DIST_TREE="$DIST_DIR/Cubby"
if [ "$CLEAN_USER_DATA" -eq 1 ]; then
    for d in .config data logs; do
        if [ -e "$DIST_TREE/$d" ]; then
            warn "清理产物里的用户数据: $DIST_TREE/$d"
            rm -rf "${DIST_TREE:?}/$d"
        fi
    done
fi
leftover=0
for d in .config data logs; do
    if [ -e "$DIST_TREE/$d" ]; then
        error "产物目录里残留了用户数据: $DIST_TREE/$d"
        leftover=1
    fi
done
if [ "$leftover" -eq 1 ]; then
    error "这些目录含你的设置/目录路径/令牌/缓存，绝不能进发行包。"
    error "处理：删掉它们，或加 --clean-user-data 让脚本在打包前自动清理。"
    exit 1
fi
info "产物校验（tools/check_build_tree.py）..."
"$PY" "$PROJECT_ROOT/tools/check_build_tree.py" "$DIST_TREE" --expect-exe Cubby

# ── 5. 压缩为 tar.gz ──────────────────────────────────────────────
info "创建 tar.gz 压缩包..."
DATE=$(date +%Y%m%d)
ARCHIVE="$DIST_DIR/Cubby_${DATE}.tar.gz"
rm -f "$ARCHIVE"
tar -C "$DIST_DIR" -czf "$ARCHIVE" Cubby

ARCHIVE_MB=$(du -sm "$ARCHIVE" | cut -f1)
success "压缩完成：$ARCHIVE (${ARCHIVE_MB}MB)"

echo
echo -e "${ColorSuccess}==============================================${ColorReset}"
echo -e "${ColorSuccess}  BUILD COMPLETE${ColorReset}"
echo -e "${ColorSuccess}==============================================${ColorReset}"
echo "输出目录: $DIST_DIR/Cubby/"
echo "压缩包  : $ARCHIVE"
echo
