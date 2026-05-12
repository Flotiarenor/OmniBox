#Requires -Version 7.0

param (
    [string]$venv_dir = "venv"
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
#>
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
        Write-Host "错误: 'py.exe' 启动器未找到。" -ForegroundColor $ColorError
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
Write-Host "    Python 虚拟环境管理工具      " -ForegroundColor $ColorMenu
Write-Host "========================================" -ForegroundColor $ColorMenu

# --- 路径配置与智能检测 ---
Write-Host "`n------ 路径配置 ------" -ForegroundColor $ColorInfo

$user_input = Read-Host "请输入项目路径或虚拟环境路径 (留空默认为当前目录)"
if (-not [string]::IsNullOrWhiteSpace($user_input)) {
    $targetPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($user_input)
} else {
    $targetPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(".")
}

$venvNames = @("venv", "env", ".venv", ".env")
$dirName = Split-Path $targetPath -Leaf
$cfgPath = Join-Path $targetPath "pyvenv.cfg"

$isExistingVenv = Test-Path $cfgPath
$isLikelyVenv = $venvNames -contains $dirName
$pathConfirmed = $false

if (-not $isExistingVenv -and -not $isLikelyVenv) {
    Write-Host "检测到输入路径为项目根目录: $targetPath" -ForegroundColor $ColorInfo
    
    $foundEnvPath = $null
    $envStatus = "" 

    foreach ($name in $venvNames) {
        $candidatePath = Join-Path $targetPath $name
        if (Test-Path $candidatePath) {
            $hasCfg = Test-Path (Join-Path $candidatePath "pyvenv.cfg")
            $childItems = Get-ChildItem -Path $candidatePath -Force -ErrorAction SilentlyContinue
            $isEmpty = ($childItems.Count -eq 0)

            if ($hasCfg) {
                $foundEnvPath = $candidatePath
                $envStatus = "existing"
                break
            }
            elseif ($isEmpty) {
                $foundEnvPath = $candidatePath
                $envStatus = "empty"
                break
            }
        }
    }

    if ($foundEnvPath) {
        if ($envStatus -eq "existing") {
            Write-Host "在目录下发现已存在的虚拟环境: $foundEnvPath" -ForegroundColor $ColorSuccess
        } else {
            Write-Host "在目录下发现空的虚拟环境目录: $foundEnvPath" -ForegroundColor $ColorInfo
        }
        
        $useExisting = Read-Host "是否直接使用此目录? (Y/n)"
        if ($useExisting -ne 'n') {
            $targetPath = $foundEnvPath
            $isExistingVenv = ($envStatus -eq "existing") 
            $pathConfirmed = $true
            $dirName = Split-Path $targetPath -Leaf
        }
    }

    if (-not $pathConfirmed) {
        $subChoice = Read-Host "未找到现有环境，是否在目录下创建 'venv' 子文件夹? (Y/n)"
        if ($subChoice -ne 'n') {
            $targetPath = Join-Path $targetPath "venv"
            $dirName = "venv"
            Write-Host "已设定环境路径为: $targetPath" -ForegroundColor $ColorSuccess
        } else {
            Write-Host "将直接在当前目录创建虚拟环境。" -ForegroundColor $ColorWarning
        }
    }
} elseif ($isExistingVenv) {
    Write-Host "检测到输入路径为已存在的虚拟环境: $targetPath" -ForegroundColor $ColorSuccess
} else {
    Write-Host "检测到输入路径为虚拟环境目录: $targetPath" -ForegroundColor $ColorInfo
}

if (-not $isExistingVenv -and $venvNames -notcontains $dirName) {
    Write-Host "`n警告: 目标文件夹名称 '$dirName' 不是标准的虚拟环境名称。" -ForegroundColor $ColorWarning
    $confirm = Read-Host "确认继续在此路径创建环境? (Y/n)"
    if ($confirm -eq 'n') {
        Write-Host "操作已取消。" -ForegroundColor $ColorError
        break
    }
}

$venv_dir = $targetPath

if (-not (Test-Path -Path $venv_dir)) {
    try {
        New-Item -ItemType Directory -Path $venv_dir -Force | Out-Null
        Write-Host "目录已准备就绪: $venv_dir" -ForegroundColor $ColorSuccess
    }
    catch {
        Write-Host "错误: 无法创建目录 $venv_dir" -ForegroundColor $ColorError
        break
    }
}

# --- Python 版本选择 ---
$pythonExecutable = $null
$pyvenvCfgPath = Join-Path $venv_dir "pyvenv.cfg"

if (Test-Path $pyvenvCfgPath) {
    Write-Host "检测到现有虚拟环境，正在读取 Python 路径..." -ForegroundColor $ColorInfo
    $cfgContent = Get-Content $pyvenvCfgPath
    foreach ($line in $cfgContent) {
        if ($line -match 'executable\s*=\s*(.+\.exe)') {
            $pythonExecutable = $matches[1].Trim()
            break
        }
    }

    if ($pythonExecutable -and (Test-Path $pythonExecutable)) {
        Write-Host "使用已有虚拟环境的 Python: $pythonExecutable" -ForegroundColor $ColorSuccess
    } else {
        Write-Host "警告：无法从 pyvenv.cfg 获取有效的 Python 路径，将重新选择。" -ForegroundColor $ColorWarning
        $pythonExecutable = Select-PythonVersion
    }
} else {
    $pythonExecutable = Select-PythonVersion
}

if (-not $pythonExecutable) {
    Write-Host "由于未选择有效的Python版本，脚本将退出。" -ForegroundColor $ColorError
    break
}

# 虚拟环境检查与创建
$activatePath = Join-Path $venv_dir "Scripts\Activate.ps1"
if (-not (Test-Path -Path $activatePath)) {
    Write-Host "正在使用 '$pythonExecutable' 创建虚拟环境..." -ForegroundColor $ColorInfo
    & $pythonExecutable -m venv $venv_dir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "创建虚拟环境失败，请检查Python版本是否有效。" -ForegroundColor $ColorError
        break
    }
    Write-Host "虚拟环境创建成功！" -ForegroundColor $ColorSuccess
}
else {
    Write-Host "使用现有虚拟环境" -ForegroundColor $ColorInfo
}

# 激活虚拟环境
try {
    if ((Get-ExecutionPolicy) -eq 'Restricted') {
        Write-Host "当前执行策略为 Restricted，请先设置为 RemoteSigned 或更高权限。" -ForegroundColor $ColorWarning
        break
    }
    . $activatePath
    Write-Host "虚拟环境已激活" -ForegroundColor $ColorSuccess
}
catch {
    Write-Host "激活虚拟环境失败: $_" -ForegroundColor $ColorError
    break
}

if (Get-Command Set-PSReadLineOption -ErrorAction SilentlyContinue) {
    Set-PSReadLineOption -HistoryNoDuplicates
    Set-PSReadLineKeyHandler -Key Tab -Function Complete
}

<#
.SYNOPSIS
    执行 Pip 命令的通用函数
.DESCRIPTION
    接收一个完整的参数数组，直接传递给 pip。
    避免了参数绑定错误
#>
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
            $version = $parts[1]
            Write-Host "$name $version" -ForegroundColor Green
        }
        else {
            Write-Host "$package" -ForegroundColor Green
        }
    }
    Write-Host "----------------------------------------"
}

# 主循环
while ($true) {
    Show-Menu
    $choice = Read-Host "请选择操作(输入序号)"
    
    switch ($choice) {
        "1" {
            $inputLibraries = Read-Host "请输入要添加的库名称，多个库之间以空格分隔(例如: numpy scipy)"
            $libraries = $inputLibraries -split '\s+' | Where-Object { $_ -ne '' }
            if ($libraries) {
                $argsList = @("install") + $libraries
                Invoke-PipCommand -PipArgs $argsList
            }
        }
        "2" {
            $inputLibraries = Read-Host "请输入要替换/升级的库名称，多个库之间以空格分隔"
            $libraries = $inputLibraries -split '\s+' | Where-Object { $_ -ne '' }
            if ($libraries) {
                $argsList = @("install", "--upgrade") + $libraries
                Invoke-PipCommand -PipArgs $argsList
            }
        }
        "3" {
            $inputLibraries = Read-Host "请输入要移除的库名称，多个库之间以空格分隔"
            $libraries = $inputLibraries -split '\s+' | Where-Object { $_ -ne '' }
            if ($libraries){
                $argsList = @("uninstall", "-y") + $libraries
                Invoke-PipCommand -PipArgs $argsList
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
                        $argsList = @("install") + $splitArgs
                        Invoke-PipCommand -PipArgs $argsList
                    }
                }
                "b" {
                    $parentDir = Split-Path -Path $venv_dir -Parent
                    if ([string]::IsNullOrWhiteSpace($parentDir)) { $parentDir = "." }
                    $defaultReqPath = Join-Path $parentDir "requirements.txt"
                    
                    $reqPath = Read-Host "请输入 requirements.txt 的路径（默认为 $defaultReqPath）"
                    if ([string]::IsNullOrWhiteSpace($reqPath)) { $reqPath = $defaultReqPath }

                    if (Test-Path -Path $reqPath) {
                        $argsList = @("install", "-r", $reqPath)
                        Invoke-PipCommand -PipArgs $argsList
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
            $parentDir = Split-Path -Path $venv_dir -Parent
            if ([string]::IsNullOrWhiteSpace($parentDir)) { $parentDir = "." }
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
            break
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
