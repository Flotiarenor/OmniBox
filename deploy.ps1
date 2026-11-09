#Requires -Version 5.1
# OmniBox Windows 一键部署脚本
# 用法：
#   .\deploy.ps1
#   .\deploy.ps1 -NoPause        # 不等待按键（自动化 / CI 调用）
#
# 功能：
#   1. 复用 setup-venv.ps1 -Install 完成 venv 创建、依赖安装与 Python 版本校验
#   2. 构建 Vue 前端壳
#   本脚本针对本仓库环境，不再提供路径 / Python 版本交互。

param(
    [switch]$NoPause
)

$ColorSuccess = "Green"
$ColorWarning = "Yellow"
$ColorError = "Red"
$ColorInfo = "Cyan"
$ColorMenu = "White"

function Assert-CommandVersion {
    param(
        [string]$VersionOutput,
        [int]$MinimumMajor,
        [string]$DisplayName
    )
    if ($VersionOutput -notmatch '^v?(\d+)\.') {
        Write-Host "错误: 无法识别 $DisplayName 版本: $VersionOutput" -ForegroundColor $ColorError
        exit 1
    }
    $major = [int]$Matches[1]
    if ($major -lt $MinimumMajor) {
        Write-Host "错误: $DisplayName 需要 $MinimumMajor+，当前 $VersionOutput" -ForegroundColor $ColorError
        exit 1
    }
    Write-Host "$DisplayName 版本: $VersionOutput" -ForegroundColor $ColorSuccess
}

function Build-VueFrontend {
    Write-Host "`n------ 构建 Vue 前端壳 ------" -ForegroundColor $ColorInfo
    $frontendDir = Join-Path $ProjectRoot "shell/frontend"
    if (-not (Test-Path $frontendDir)) {
        Write-Host "错误: 前端目录不存在: $frontendDir" -ForegroundColor $ColorError
        exit 1
    }

    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Host "错误: 未找到 Node.js。请安装 Node.js 18+ 并添加到 PATH。" -ForegroundColor $ColorError
        exit 1
    }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Host "错误: 未找到 npm。请安装 npm 9+。" -ForegroundColor $ColorError
        exit 1
    }

    Assert-CommandVersion -VersionOutput ((& node -v).Trim()) -MinimumMajor 18 -DisplayName "Node.js"
    Assert-CommandVersion -VersionOutput ((& npm -v).Trim()) -MinimumMajor 9 -DisplayName "npm"

    Push-Location $frontendDir
    try {
        Write-Host "安装前端依赖 (npm ci)..." -ForegroundColor $ColorInfo
        npm ci
        if ($LASTEXITCODE -ne 0) {
            throw "npm ci 失败"
        }

        Write-Host "构建前端项目 (npm run build)..." -ForegroundColor $ColorInfo
        npm run build
        if ($LASTEXITCODE -ne 0) {
            throw "npm run build 失败"
        }
        Write-Host "Vue 前端壳构建完成。" -ForegroundColor $ColorSuccess
    }
    catch {
        Write-Host "前端构建失败: $_" -ForegroundColor $ColorError
        exit 1
    }
    finally {
        Pop-Location
    }
}

# ========== 主流程 ==========

Write-Host "========================================" -ForegroundColor $ColorMenu
Write-Host "    OmniBox 一键部署脚本      " -ForegroundColor $ColorMenu
Write-Host "========================================" -ForegroundColor $ColorMenu

$ProjectRoot = $PSScriptRoot
Write-Host "项目目录: $ProjectRoot" -ForegroundColor $ColorInfo

# 环境与依赖统一交给 setup-venv.ps1 -Install 处理
# -Dev：部署机同时是开发/构建机（需要 ruff / pyright / pyinstaller）
Write-Host "`n------ 准备 Python 环境与依赖 ------" -ForegroundColor $ColorInfo
& (Join-Path $ProjectRoot "setup-venv.ps1") -Install -Dev -ProjectRoot $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    Write-Host "环境准备失败，部署中止。" -ForegroundColor $ColorError
    exit 1
}

# 调用 Vue 前端构建
Build-VueFrontend

# 部署脚本不会、也无法把 venv 激活到调用方的 shell 里，
# 因此这里必须打印 venv 解释器的绝对路径，而不是裸的 python。
$venvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"

Write-Host "`n========================================" -ForegroundColor $ColorSuccess
Write-Host "    部署成功   " -ForegroundColor $ColorSuccess
Write-Host "========================================" -ForegroundColor $ColorSuccess
Write-Host "虚拟环境位置: $(Join-Path $ProjectRoot 'venv')" -ForegroundColor $ColorInfo
Write-Host "启动应用: & '$venvPython' main.py" -ForegroundColor $ColorInfo
Write-Host "或: Web-only 模式 & '$venvPython' main.py --web-only" -ForegroundColor $ColorInfo

# 双击运行时保持窗口打开以便查看结果；-NoPause 或输入被重定向（CI / 管道）时跳过。
if (-not $NoPause -and -not [Console]::IsInputRedirected) {
    Write-Host "`n按任意键退出..."
    try {
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    }
    catch {
        # 无真实控制台（部分宿主）时 ReadKey 不可用，直接结束。
    }
}
