# OmniBox Windows 一键部署脚本
# 用法：
#   .\deploy.ps1
#
# 功能：
#   1. 复用 setup-venv.ps1 -Install 完成 venv 创建、依赖安装
#   2. 构建 Vue 前端壳
#   本脚本针对本仓库环境，不再提供路径 / Python 版本交互。

$ColorSuccess = "Green"
$ColorWarning = "Yellow"
$ColorError = "Red"
$ColorInfo = "Cyan"
$ColorMenu = "White"

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

    Write-Host "Node.js 版本: $(node -v)" -ForegroundColor $ColorSuccess
    Write-Host "npm 版本: $(npm -v)" -ForegroundColor $ColorSuccess

    Push-Location $frontendDir
    try {
        Write-Host "安装前端依赖 (npm install)..." -ForegroundColor $ColorInfo
        npm install
        if ($LASTEXITCODE -ne 0) {
            throw "npm install 失败"
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
Write-Host "`n------ 准备 Python 环境与依赖 ------" -ForegroundColor $ColorInfo
& (Join-Path $ProjectRoot "setup-venv.ps1") -Install -ProjectRoot $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    Write-Host "环境准备失败，部署中止。" -ForegroundColor $ColorError
    exit 1
}

# 调用 Vue 前端构建
Build-VueFrontend

Write-Host "`n========================================" -ForegroundColor $ColorSuccess
Write-Host "    部署成功   " -ForegroundColor $ColorSuccess
Write-Host "========================================" -ForegroundColor $ColorSuccess
Write-Host "虚拟环境位置: $(Join-Path $ProjectRoot 'venv')" -ForegroundColor $ColorInfo
Write-Host "启动应用: python main.py" -ForegroundColor $ColorInfo
Write-Host "或: Web-only 模式 python main.py --web-only" -ForegroundColor $ColorInfo

# 脚本结束，保持窗口打开以便查看结果
Write-Host "`nexit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")