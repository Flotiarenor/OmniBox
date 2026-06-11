param (
    [string]$ProjectPath = ".",
    [string]$RequirementsFile = "requirements.txt"
)

$ColorSuccess = "Green"
$ColorWarning = "Yellow"
$ColorError = "Red"
$ColorInfo = "Cyan"
$ColorMenu = "White"

function Select-PythonVersion {
    Write-Host "`n------ 正在搜索可用的 Python 版本 ------" -ForegroundColor $ColorInfo
    $foundPythons = @()

    if (Get-Command py -ErrorAction SilentlyContinue) {
        Write-Host "使用 'py.exe' 启动器查找已安装的Python版本..." -ForegroundColor $ColorInfo
        try {
            $pyListOutput = & py -0p 2>&1
            if ($LASTEXITCODE -eq 0) {
                foreach ($line in $pyListOutput) {
                    if ($line -match '^\s*-V:(?<version>\d+\.\d+)(?<default>\s*\*)?\s+(?<path>.+python\.exe)$') {
                        $version = $matches.version
                        $path = $matches.path.Trim()
                        $isDefault = -not [string]::IsNullOrEmpty($matches.default)
                        $foundPythons += [PSCustomObject]@{
                            Version   = $version
                            Path      = $path
                            IsDefault = $isDefault
                            Source    = "py.exe"
                        }
                    }
                }
            }
        }
        catch {
            Write-Warning "执行 'py -0p' 时出错: $_"
        }
    }
    else {
        Write-Host "错误: 'py.exe' 启动器未找到。请确保Python已安装并添加到PATH。" -ForegroundColor $ColorError
        return $null
    }

    if ($foundPythons.Count -eq 0) {
        Write-Host "错误: 未在系统上找到任何Python安装。" -ForegroundColor $ColorError
        return $null
    }

    Write-Host "`n发现以下Python版本:" -ForegroundColor $ColorSuccess
    for ($i = 0; $i -lt $foundPythons.Count; $i++) {
        $python = $foundPythons[$i]
        $defaultMarker = if ($python.IsDefault) { " (默认)" } else { "" }
        Write-Host ("{0}. Python {1}{2} - {3}" -f ($i + 1), $python.Version, $defaultMarker, $python.Path) -ForegroundColor $ColorMenu
    }

    do {
        $choice = Read-Host "`n请选择要用于创建虚拟环境的Python版本 (输入序号)"
        if ($choice -match '^\d+$' -and [int]$choice -ge 1 -and [int]$choice -le $foundPythons.Count) {
            $selectedIndex = [int]$choice - 1
            $selectedPython = $foundPythons[$selectedIndex]
            Write-Host "已选择: Python $($selectedPython.Version) at $($selectedPython.Path)" -ForegroundColor $ColorSuccess
            return $selectedPython.Path
        }
        else {
            Write-Host "无效的选择，请输入列表中的数字序号。" -ForegroundColor $ColorError
        }
    } while ($true)
}

function Build-VueFrontend {
    Write-Host "`n------ 构建 Vue 前端壳 ------" -ForegroundColor $ColorInfo
    $frontendDir = Join-Path $projectDir "shell/frontend"
    if (-not (Test-Path $frontendDir)) {
        Write-Host "错误: 前端目录不存在: $frontendDir" -ForegroundColor $ColorError
        exit 1
    }

    # 检查 Node.js 和 npm
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
Write-Host "    Python 项目一键部署脚本      " -ForegroundColor $ColorMenu
Write-Host "========================================" -ForegroundColor $ColorMenu

$resolvedProjectPath = Resolve-Path -Path $ProjectPath -ErrorAction SilentlyContinue
if (-not $resolvedProjectPath) {
    Write-Host "错误: 项目路径 '$ProjectPath' 不存在。" -ForegroundColor $ColorError
    exit 1
}
$projectDir = $resolvedProjectPath.Path
Write-Host "项目目录: $projectDir" -ForegroundColor $ColorInfo

$reqFilePath = Join-Path $projectDir $RequirementsFile
if (-not (Test-Path $reqFilePath)) {
    Write-Host "错误: 在项目目录中未找到 '$RequirementsFile' 文件。" -ForegroundColor $ColorError
    Write-Host "请确保 '$RequirementsFile' 文件位于项目根目录: $projectDir" -ForegroundColor $ColorWarning
    exit 1
}
Write-Host "找到依赖文件: $reqFilePath" -ForegroundColor $ColorSuccess

$pythonExecutable = Select-PythonVersion
if (-not $pythonExecutable) {
    Write-Host "由于未选择有效的Python版本，脚本将退出。" -ForegroundColor $ColorError
    exit 1
}

$venvDir = Join-Path $projectDir "venv"
$activatePath = Join-Path $venvDir "Scripts\Activate.ps1"

if (Test-Path $activatePath) {
    Write-Host "检测到已存在的虚拟环境: $venvDir" -ForegroundColor $ColorInfo
    $recreate = Read-Host "是否重新创建虚拟环境? (y/N)"
    if ($recreate -eq 'y') {
        Write-Host "正在删除旧的虚拟环境..." -ForegroundColor $ColorWarning
        Remove-Item -Path $venvDir -Recurse -Force -ErrorAction Stop
        Write-Host "正在创建新的虚拟环境..." -ForegroundColor $ColorInfo
        & $pythonExecutable -m venv $venvDir
    }
    else {
        Write-Host "将使用现有的虚拟环境。" -ForegroundColor $ColorInfo
    }
}
else {
    Write-Host "正在使用 '$pythonExecutable' 创建虚拟环境..." -ForegroundColor $ColorInfo
    & $pythonExecutable -m venv $venvDir
}

if (-not (Test-Path $activatePath)) {
    Write-Host "错误: 虚拟环境创建失败。" -ForegroundColor $ColorError
    exit 1
}
Write-Host "虚拟环境就绪: $venvDir" -ForegroundColor $ColorSuccess

try {
    $currentPolicy = Get-ExecutionPolicy
    if ($currentPolicy -eq 'Restricted') {
        Write-Host "当前执行策略为 Restricted." -ForegroundColor $ColorWarning
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
    }

    . $activatePath
    Write-Host "虚拟环境已激活" -ForegroundColor $ColorSuccess

    Write-Host "正在升级 pip..." -ForegroundColor $ColorInfo
    python -m pip install --upgrade pip -q

    Write-Host "正在从 '$RequirementsFile' 安装依赖..." -ForegroundColor $ColorInfo
    pip install -r $reqFilePath

    if ($LASTEXITCODE -ne 0) {
        Write-Host "error: 依赖安装失败，请检查 '$RequirementsFile' 文件内容。" -ForegroundColor $ColorError
        exit 1
    }

    # 调用 Vue 前端构建
    Build-VueFrontend

    Write-Host "`n========================================" -ForegroundColor $ColorSuccess
    Write-Host "    部署成功   " -ForegroundColor $ColorSuccess
    Write-Host "========================================" -ForegroundColor $ColorSuccess
    Write-Host "虚拟环境位置: $venvDir" -ForegroundColor $ColorInfo
    Write-Host "激活命令: .\$venvDir\Scripts\Activate" -ForegroundColor $ColorInfo
    Write-Host "启动应用: python main.py" -ForegroundColor $ColorInfo
}
catch {
    Write-Host "部署过程中发生错误: $_" -ForegroundColor $ColorError
    exit 1
}
finally {
    if ($currentPolicy -eq 'Restricted') {
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy $currentPolicy -Force
    }
}

# 脚本结束，保持窗口打开以便查看结果
Write-Host "`nexit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")