#Requires -Version 5.1

<#
.SYNOPSIS
    OmniBox 虚拟环境统一入口 / pip 管理台
.DESCRIPTION
    -Install 模式（非交互）:
        ensure venv 存在 -> 升级 pip -> 从 requirements.txt 安装依赖。
        供 deploy.ps1 / build-release.ps1 及 CI 调用。

    默认模式（交互）:
        直接以本仓库 venv 打开 pip 管理控制台（增库/删库/导出等）。
.PARAMETER Install
    非交互安装依赖。
.PARAMETER ProjectRoot
    项目根目录（固定本仓库，可由调用方显式传入）。
#>

param(
    [switch]$Install,
    [string]$ProjectRoot = $PSScriptRoot
)

$ColorSuccess = "Green"
$ColorWarning = "Yellow"
$ColorError = "Red"
$ColorInfo = "Cyan"
$ColorMenu = "White"

$venvDir = Join-Path $ProjectRoot "venv"
$activatePath = Join-Path $venvDir "Scripts\Activate.ps1"

function Get-DefaultPython {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $out = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $path = ($out | Select-Object -Last 1).Trim()
            if (Test-Path $path) { return $path }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return (Get-Command python).Source
    }
    return $null
}

function Get-VenvPython {
    $cfgPath = Join-Path $venvDir "pyvenv.cfg"
    if (Test-Path $cfgPath) {
        foreach ($line in Get-Content $cfgPath) {
            if ($line -match 'executable\s*=\s*(.+\.exe)') {
                $path = $matches[1].Trim()
                if (Test-Path $path) { return $path }
            }
        }
    }
    return Get-DefaultPython
}

function Ensure-Venv {
    if (Test-Path $activatePath) {
        Write-Host "使用现有虚拟环境: $venvDir" -ForegroundColor $ColorInfo
        return
    }

    $pythonExecutable = Get-VenvPython
    if (-not $pythonExecutable) {
        throw "未找到可用的 Python（py 启动器或 python 命令均不可用）"
    }

    Write-Host "正在使用 '$pythonExecutable' 创建虚拟环境..." -ForegroundColor $ColorInfo
    & $pythonExecutable -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $activatePath)) {
        throw "虚拟环境创建失败，请检查 Python 版本是否有效。"
    }
    Write-Host "虚拟环境创建成功: $venvDir" -ForegroundColor $ColorSuccess
}

# ======================== -Install 非交互模式 ========================

if ($Install) {
    $reqFile = Join-Path $ProjectRoot "requirements.txt"
    if (-not (Test-Path $reqFile)) {
        Write-Host "[错误] 未找到依赖文件: $reqFile" -ForegroundColor $ColorError
        exit 1
    }

    try {
        Ensure-Venv
        $venvPy = Join-Path $venvDir "Scripts\python.exe"

        Write-Host "正在升级 pip..." -ForegroundColor $ColorInfo
        & $venvPy -m pip install --upgrade pip -q
        if ($LASTEXITCODE -ne 0) { throw "pip 升级失败" }

        Write-Host "正在安装依赖: $reqFile" -ForegroundColor $ColorInfo
        & $venvPy -m pip install -r $reqFile -q
        if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，请检查 '$reqFile'" }
    }
    catch {
        Write-Host "[错误] $_" -ForegroundColor $ColorError
        exit 1
    }

    Write-Host "环境就绪: $venvDir" -ForegroundColor $ColorSuccess
    exit 0
}

# ======================== 交互式 pip 管理台 ========================

Ensure-Venv

if (-not (Test-Path $activatePath)) {
    Write-Host "[错误] 虚拟环境不存在: $venvDir" -ForegroundColor $ColorError
    exit 1
}

try {
    $restorePolicy = $null
    if ((Get-ExecutionPolicy) -eq 'Restricted') {
        Write-Host "当前执行策略为 Restricted，临时切换为 Bypass..." -ForegroundColor $ColorWarning
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
        $restorePolicy = 'Restricted'
    }
    . $activatePath
    Write-Host "虚拟环境已激活: $venvDir" -ForegroundColor $ColorSuccess
}
catch {
    Write-Host "激活虚拟环境失败: $_" -ForegroundColor $ColorError
    exit 1
}

if (Get-Command Set-PSReadLineOption -ErrorAction SilentlyContinue) {
    Set-PSReadLineOption -HistoryNoDuplicates
    Set-PSReadLineKeyHandler -Key Tab -Function Complete
}

function Invoke-PipCommand {
    param(
        [Parameter(Mandatory=$true)]
        [string[]]$PipArgs
    )
    Write-Host "执行: pip $PipArgs" -ForegroundColor $ColorInfo
    & pip @PipArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "操作失败，退出代码: $LASTEXITCODE" -ForegroundColor $ColorError
    } else {
        Write-Host "操作完成。" -ForegroundColor $ColorSuccess
    }
}

function Show-Menu {
    Clear-Host
    Write-Host "`n========================================" -ForegroundColor $ColorMenu
    Write-Host "        虚拟环境库管理控制台        " -ForegroundColor $ColorMenu
    Write-Host "  环境: $venvDir" -ForegroundColor $ColorInfo
    Write-Host "========================================" -ForegroundColor $ColorMenu
    Write-Host "1. 添加库" -ForegroundColor Green
    Write-Host "2. 替换/升级库" -ForegroundColor Blue
    Write-Host "3. 移除库" -ForegroundColor Red
    Write-Host "4. 自定义安装(完整命令)" -ForegroundColor Yellow
    Write-Host "5. 查看已安装库(list)" -ForegroundColor Cyan
    Write-Host "6. 导出依赖列表" -ForegroundColor Magenta
    Write-Host "7. 退出菜单" -ForegroundColor Gray
    Write-Host "========================================" -ForegroundColor $ColorMenu
}

function Show-InstalledPackages {
    Write-Host "`n已安装的库及其版本:" -ForegroundColor $ColorInfo
    Write-Host "----------------------------------------"
    $packages = pip list --format=freeze
    if (-not $packages) {
        Write-Host "未找到已安装的库" -ForegroundColor $ColorWarning
        return
    }
    foreach ($package in $packages) {
        if ($package -match "^\s*$") { continue }
        $parts = $package -split '==', 2
        if ($parts.Count -ge 2) {
            $name = $parts[0].PadRight(25)
            Write-Host "$name $($parts[1])" -ForegroundColor Green
        }
        else {
            Write-Host "$package" -ForegroundColor Green
        }
    }
    Write-Host "----------------------------------------"
}

$parentDir = Split-Path $venvDir -Parent

while ($true) {
    Show-Menu
    $choice = Read-Host "请选择操作(输入序号)"

    switch ($choice) {
        "1" {
            $inputLibraries = Read-Host "请输入要添加的库名称，多个库之间以空格分隔(例如: numpy scipy)"
            $libraries = $inputLibraries -split '\s+' | Where-Object { $_ -ne '' }
            if ($libraries) {
                Invoke-PipCommand -PipArgs (@("install") + $libraries)
            }
        }
        "2" {
            $inputLibraries = Read-Host "请输入要替换/升级的库名称，多个库之间以空格分隔"
            $libraries = $inputLibraries -split '\s+' | Where-Object { $_ -ne '' }
            if ($libraries) {
                Invoke-PipCommand -PipArgs (@("install", "--upgrade") + $libraries)
            }
        }
        "3" {
            $inputLibraries = Read-Host "请输入要移除的库名称，多个库之间以空格分隔"
            $libraries = $inputLibraries -split '\s+' | Where-Object { $_ -ne '' }
            if ($libraries) {
                Invoke-PipCommand -PipArgs (@("uninstall", "-y") + $libraries)
            }
        }
        "4" {
            Write-Host "`n选择自定义安装方式:" -ForegroundColor $ColorInfo
            Write-Host "a. 输入完整 pip 安装命令" -ForegroundColor Yellow
            Write-Host "b. 使用 requirements.txt 文件安装" -ForegroundColor Cyan
            $customInstallOption = Read-Host "请输入选项"

            switch ($customInstallOption) {
                "a" {
                    Write-Host "`n示例命令:" -ForegroundColor $ColorInfo
                    Write-Host "torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118"
                    Write-Host ""
                    $full_command = Read-Host "请输入完整的pip安装参数 (不需要输入 pip install)"
                    if (-not [string]::IsNullOrWhiteSpace($full_command)) {
                        $splitArgs = $full_command.Split(" ")
                        Invoke-PipCommand -PipArgs (@("install") + $splitArgs)
                    }
                }
                "b" {
                    $defaultReqPath = Join-Path $parentDir "requirements.txt"
                    $reqPath = Read-Host "请输入 requirements.txt 的路径（默认为 $defaultReqPath）"
                    if ([string]::IsNullOrWhiteSpace($reqPath)) { $reqPath = $defaultReqPath }

                    if (Test-Path $reqPath) {
                        Invoke-PipCommand -PipArgs (@("install", "-r", $reqPath))
                    }
                    else {
                        Write-Host "未找到指定的 requirements.txt 文件: $reqPath" -ForegroundColor $ColorError
                    }
                }
                default {
                    Write-Host "无效选项。" -ForegroundColor $ColorError
                }
            }
        }
        "5" {
            Show-InstalledPackages
        }
        "6" {
            $exportPath = Join-Path $parentDir "requirements.txt"
            Write-Host "正在导出依赖列表到 $exportPath ..." -ForegroundColor $ColorInfo
            pip freeze | Out-File -FilePath $exportPath -Encoding UTF8
            if ($LASTEXITCODE -eq 0) {
                Write-Host "依赖已导出至 $exportPath" -ForegroundColor $ColorSuccess
            }
            else {
                Write-Host "导出失败" -ForegroundColor $ColorError
            }
        }
        "7" {
            Write-Host "`n退出虚拟环境管理，感谢使用 >_<" -ForegroundColor $ColorSuccess
            Write-Host "========================================" -ForegroundColor $ColorMenu
            return
        }
        default {
            Write-Host "无效的选择，请重试。" -ForegroundColor $ColorError
            Start-Sleep -Seconds 1
        }
    }

    Write-Host "`n按回车键返回菜单..."
    do {
        $key = [System.Console]::ReadKey($true)
    } until ($key.Key -eq 'Enter')
}

if ($restorePolicy) {
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy $restorePolicy -Force
}