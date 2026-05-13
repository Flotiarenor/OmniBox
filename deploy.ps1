#Requires -Version 5.1

param (
    [string]$ProjectPath = ".",
    [string]$RequirementsFile = "requirements.txt"
)

# 颜色定义
$ColorSuccess = "Green"
$ColorWarning = "Yellow"
$ColorError = "Red"
$ColorInfo = "Cyan"
$ColorMenu = "White"

<#
.SYNOPSIS
    发现并列出系统上可用的Python版本。
    简化版：仅通过 py.exe 查找，并让用户选择。
#>
function Select-PythonVersion {
    Write-Host "`n------ 正在搜索可用的 Python 版本 ------" -ForegroundColor $ColorInfo
    $foundPythons = @()

    # 尝试使用 py.exe 启动器，这是Windows上最标准的方式
    if (Get-Command py -ErrorAction SilentlyContinue) {
        Write-Host "使用 'py.exe' 启动器查找已安装的Python版本..." -ForegroundColor $ColorInfo
        try {
            # -0p 列出所有版本和路径
            $pyListOutput = & py -0p 2>&1
            if ($LASTEXITCODE -eq 0) {
                foreach ($line in $pyListOutput) {
                    # 解析输出行，例如: " -V:3.11 * C:\Users\...\python.exe"
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

# ======================== 主脚本开始 ========================

Write-Host "========================================" -ForegroundColor $ColorMenu
Write-Host "    Python 项目一键部署脚本      " -ForegroundColor $ColorMenu
Write-Host "========================================" -ForegroundColor $ColorMenu

# --- 1. 确定项目路径 ---
# 如果用户没有提供路径，默认为当前目录（脚本所在目录）
$resolvedProjectPath = Resolve-Path -Path $ProjectPath -ErrorAction SilentlyContinue
if (-not $resolvedProjectPath) {
    Write-Host "错误: 项目路径 '$ProjectPath' 不存在。" -ForegroundColor $ColorError
    exit 1
}
$projectDir = $resolvedProjectPath.Path
Write-Host "项目目录: $projectDir" -ForegroundColor $ColorInfo

# --- 2. 查找 requirements.txt ---
$reqFilePath = Join-Path $projectDir $RequirementsFile
if (-not (Test-Path $reqFilePath)) {
    Write-Host "错误: 在项目目录中未找到 '$RequirementsFile' 文件。" -ForegroundColor $ColorError
    Write-Host "请确保 '$RequirementsFile' 文件位于项目根目录: $projectDir" -ForegroundColor $ColorWarning
    exit 1
}
Write-Host "找到依赖文件: $reqFilePath" -ForegroundColor $ColorSuccess

# --- 3. 选择 Python 版本 ---
$pythonExecutable = Select-PythonVersion
if (-not $pythonExecutable) {
    Write-Host "由于未选择有效的Python版本，脚本将退出。" -ForegroundColor $ColorError
    exit 1
}

# --- 4. 创建虚拟环境 ---
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

# 检查虚拟环境是否创建成功
if (-not (Test-Path $activatePath)) {
    Write-Host "错误: 虚拟环境创建失败。" -ForegroundColor $ColorError
    exit 1
}
Write-Host "虚拟环境就绪: $venvDir" -ForegroundColor $ColorSuccess

# --- 5. 激活虚拟环境并安装依赖 ---
try {
    # 绕过执行策略限制，仅对当前会话生效
    $currentPolicy = Get-ExecutionPolicy
    if ($currentPolicy -eq 'Restricted') {
        Write-Host "当前执行策略为 Restricted，尝试临时绕过..." -ForegroundColor $ColorWarning
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
    }

    # 激活虚拟环境
    . $activatePath
    Write-Host "虚拟环境已激活" -ForegroundColor $ColorSuccess

    # 升级 pip 到最新版本（可选，但推荐）
    Write-Host "正在升级 pip..." -ForegroundColor $ColorInfo
    python -m pip install --upgrade pip -q

    # 安装依赖
    Write-Host "正在从 '$RequirementsFile' 安装依赖..." -ForegroundColor $ColorInfo
    pip install -r $reqFilePath

    if ($LASTEXITCODE -eq 0) {
        Write-Host "`n========================================" -ForegroundColor $ColorSuccess
        Write-Host "    部署成功完成！" -ForegroundColor $ColorSuccess
        Write-Host "========================================" -ForegroundColor $ColorSuccess
        Write-Host "虚拟环境位置: $venvDir" -ForegroundColor $ColorInfo
        Write-Host "激活命令: .\$venvDir\Scripts\Activate" -ForegroundColor $ColorInfo
    }
    else {
        Write-Host "错误: 依赖安装失败，请检查 '$RequirementsFile' 文件内容。" -ForegroundColor $ColorError
        exit 1
    }
}
catch {
    Write-Host "部署过程中发生错误: $_" -ForegroundColor $ColorError
    exit 1
}
finally {
    # 恢复执行策略（如果之前修改过）
    if ($currentPolicy -eq 'Restricted') {
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy $currentPolicy -Force
    }
}

# 脚本结束，保持窗口打开以便查看结果
Write-Host "`n按任意键退出..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")