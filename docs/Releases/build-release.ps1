<#
.SYNOPSIS
    OmniBox 一键发布构建脚本
.DESCRIPTION
    1. 构建 Vue 前端
    2. PyInstaller 打包（onedir + UPX）
    3. 7z 压缩为便携包
    4. 输出到 docs/Releases/
.PARAMETER UseCleanPath
    自动复制项目到纯 ASCII 临时路径（解决中文路径导致 DLL 加载失败问题）
#>

param(
    [switch]$SkipFrontend,
    [switch]$SkipPyInstaller,
    [switch]$SkipArchive,
    [switch]$UseCleanPath,
    [string]$OutputDir = "$PSScriptRoot"
)

$ColorInfo    = "Cyan"
$ColorSuccess = "Green"
$ColorWarning = "Yellow"
$ColorError   = "Red"

$ProjectRoot  = Resolve-Path "$PSScriptRoot/../.."
$SpecFile     = "$PSScriptRoot/omnibox.spec"
$BuildDir     = "$ProjectRoot/.build"
$DistDir      = "$OutputDir"

Write-Host ("=" * 46) -ForegroundColor $ColorInfo
Write-Host "  OmniBox Build Script v1.0" -ForegroundColor $ColorInfo
Write-Host ("=" * 46) -ForegroundColor $ColorInfo
Write-Host "Project : $ProjectRoot"  -ForegroundColor $ColorInfo
Write-Host "Output  : $DistDir"      -ForegroundColor $ColorInfo

# -- 检查路径中是否含非 ASCII 字符（会导致 PyInstaller 加载 python DLL 失败）--
$hasNonAscii = [regex]::Match($ProjectRoot, '[^\x00-\x7F]').Success
if ($hasNonAscii -and -not $UseCleanPath) {
    Write-Host "`n[WARN] 项目路径包含非 ASCII 字符（中文），运行 exe 时会报错:" -ForegroundColor $ColorWarning
    Write-Host "  'Failed to load Python DLL ... 内存位置访问无效'" -ForegroundColor $ColorWarning
    Write-Host "  方案 A: 解压后将 OmniBox/ 移到纯英文路径（如 D:\OmniBox）" -ForegroundColor $ColorWarning
    Write-Host "  方案 B: 用 -UseCleanPath 参数自动复制到 Temp 目录构建" -ForegroundColor $ColorWarning
    Write-Host ""
}

# -- 1. 构建 Vue 前端 --
if (-not $SkipFrontend) {
    Write-Host "[1/3] Building Vue frontend..." -ForegroundColor $ColorInfo
    $frontendDir = "$ProjectRoot/shell/frontend"

    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Host "ERROR: Node.js not found" -ForegroundColor $ColorError; exit 1
    }

    Push-Location $frontendDir
    try {
        Write-Host "  -> npm install" -ForegroundColor $ColorWarning
        npm install --silent
        if ($LASTEXITCODE -ne 0) { throw "npm install failed" }

        Write-Host "  -> npm run build" -ForegroundColor $ColorWarning
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
    } finally { Pop-Location }

    if (-not (Test-Path "$frontendDir/dist/index.html")) {
        Write-Host "ERROR: dist/index.html not found" -ForegroundColor $ColorError; exit 1
    }
    Write-Host "  [OK] Frontend built" -ForegroundColor $ColorSuccess
} else {
    Write-Host "[1/3] Skipping frontend" -ForegroundColor $ColorWarning
}

# -- 2. PyInstaller 打包 --
if (-not $SkipPyInstaller) {
    Write-Host "[2/3] PyInstaller packaging..." -ForegroundColor $ColorInfo

    # 虚拟环境与依赖统一交给 setup-venv.ps1 处理（必须在 UseCleanPath 复制之前执行，
    # 因为 venv 不随项目一起复制到临时路径）
    Write-Host "  -> 确保虚拟环境与依赖 (setup-venv.ps1 -Install)..." -ForegroundColor $ColorWarning
    & "$ProjectRoot/setup-venv.ps1" -Install -ProjectRoot $ProjectRoot
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: 环境准备失败" -ForegroundColor $ColorError; exit 1
    }
    $venvPython = Join-Path $ProjectRoot "venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Host "ERROR: 未找到虚拟环境 Python: $venvPython" -ForegroundColor $ColorError; exit 1
    }

    # Check UPX
    $upxFound = Get-Command upx -ErrorAction SilentlyContinue
    if ($upxFound) {
        Write-Host "  [OK] UPX found: $(upx --version 2>&1 | Select-Object -First 1)" -ForegroundColor $ColorSuccess
    } else {
        Write-Host "  [!] UPX not found (size will be larger)" -ForegroundColor $ColorWarning
    }

    # Clean old builds
    if (Test-Path $BuildDir)  { Remove-Item -Recurse -Force $BuildDir }

    # UseCleanPath: copy to ASCII temp dir for PyInstaller
    if ($UseCleanPath -and $hasNonAscii) {
        $cleanTempDir = Join-Path $env:TEMP "OmniBox_Build_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
        Write-Host "  -> Copying to ASCII path: $cleanTempDir" -ForegroundColor $ColorWarning
        $excludeList = @('venv', '.git', 'node_modules', '.cache', '__pycache__', '.build')
        New-Item -ItemType Directory -Path $cleanTempDir -Force | Out-Null
        Get-ChildItem -Path $ProjectRoot -Exclude $excludeList | ForEach-Object {
            Copy-Item -Recurse $_.FullName -Destination $cleanTempDir -ErrorAction SilentlyContinue
        }
        $ProjectRoot = Resolve-Path $cleanTempDir
        $BuildDir = "$cleanTempDir/.build"
        $SpecFile = "$cleanTempDir/docs/Releases/omnibox.spec"
        $DistDir = "$cleanTempDir/dist"
        Write-Host "     -> Build will run in: $cleanTempDir" -ForegroundColor $ColorInfo
    }

    $oldDistDir = "$DistDir/OmniBox"
    if (Test-Path $oldDistDir) { Remove-Item -Recurse -Force $oldDistDir }

    Write-Host "  -> Running PyInstaller..." -ForegroundColor $ColorWarning
    & $venvPython -m PyInstaller `
        --workpath "$BuildDir" `
        --distpath "$DistDir" `
        --noconfirm `
        --log-level WARN `
        "$SpecFile"

    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: PyInstaller failed" -ForegroundColor $ColorError; exit 1
    }

    $exePath = "$DistDir/OmniBox/OmniBox.exe"
    if (-not (Test-Path $exePath)) {
        Write-Host "ERROR: Output not found at $exePath" -ForegroundColor $ColorError; exit 1
    }

    # PyInstaller leaves a stray executable outside the COLLECT folder; remove it
    $strayExe = "$DistDir/OmniBox.exe"
    if (Test-Path $strayExe) {
        Remove-Item -Force $strayExe
        Write-Host "  [OK] Removed stray exe: $strayExe" -ForegroundColor $ColorSuccess
    }

    $size = (Get-ChildItem -Recurse "$DistDir/OmniBox" | Measure-Object -Property Length -Sum).Sum
    $sizeMB = [math]::Round($size / 1MB, 2)
    Write-Host "  [OK] PyInstaller done" -ForegroundColor $ColorSuccess
    Write-Host "       Output: $DistDir/OmniBox/" -ForegroundColor $ColorSuccess
    Write-Host "       Size: ${sizeMB}MB" -ForegroundColor $ColorSuccess
} else {
    Write-Host "[2/3] Skipping PyInstaller" -ForegroundColor $ColorWarning
}

# -- 3. 7z/zip 压缩 --
if (-not $SkipArchive) {
    Write-Host "[3/3] Creating archive..." -ForegroundColor $ColorInfo

    $sevenZip = Get-Command 7z -ErrorAction SilentlyContinue
    if (-not $sevenZip) {
        $paths = @(
            "$env:ProgramFiles/7-Zip/7z.exe",
            "${env:ProgramFiles(x86)}/7-Zip/7z.exe",
            "$env:LOCALAPPDATA/Programs/7-Zip/7z.exe"
        )
        foreach ($p in $paths) {
            if (Test-Path $p) { $sevenZip = $p; break }
        }
    }

    $useZip = $null -eq $sevenZip

    $sourceDir  = "$DistDir/OmniBox"
    $version    = (Get-Date -Format "yyyyMMdd")
    $archiveName = "OmniBox_${version}"

    if (-not $useZip) {
        $archiveFile = "$OutputDir/${archiveName}.7z"
        Write-Host "  -> 7z a -mx=9 -ms=on $archiveFile" -ForegroundColor $ColorWarning
        if ($sevenZip -is [System.Management.Automation.CommandInfo]) {
            & 7z a -mx=9 -ms=on "$archiveFile" "$sourceDir/*"
        } else {
            & $sevenZip a -mx=9 -ms=on "$archiveFile" "$sourceDir/*"
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: 7z failed" -ForegroundColor $ColorError; exit 1
        }
    } else {
        $archiveFile = "$OutputDir/${archiveName}.zip"
        Write-Host "  -> zip (7z not found, fallback)" -ForegroundColor $ColorWarning
        Compress-Archive -Path "$sourceDir/*" -DestinationPath $archiveFile -CompressionLevel Optimal
        if (-not (Test-Path $archiveFile)) {
            Write-Host "ERROR: zip failed" -ForegroundColor $ColorError; exit 1
        }
    }

    $archiveSize = [math]::Round((Get-Item $archiveFile).Length / 1MB, 2)
    $dirSize = [math]::Round(
        (Get-ChildItem -Recurse $sourceDir | Measure-Object -Property Length -Sum).Sum / 1MB, 2)
    Write-Host "  [OK] Archive created" -ForegroundColor $ColorSuccess
    Write-Host "       File: $archiveFile" -ForegroundColor $ColorSuccess
    Write-Host "       Size: ${dirSize}MB -> ${archiveSize}MB (${[math]::Round((1-$archiveSize/$dirSize)*100, 0)}% saved)" -ForegroundColor $ColorSuccess
} else {
    Write-Host "[3/3] Skipping archive" -ForegroundColor $ColorWarning
}

Write-Host ("=" * 46) -ForegroundColor $ColorSuccess
Write-Host "  BUILD COMPLETE" -ForegroundColor $ColorSuccess
Write-Host ("=" * 46) -ForegroundColor $ColorSuccess

if ($hasNonAscii -and -not $UseCleanPath) {
    Write-Host "`n[IMPORTANT] Path has non-ASCII chars. To run the exe:" -ForegroundColor $ColorWarning
    Write-Host "  1. Extract the 7z archive" -ForegroundColor $ColorWarning
    Write-Host "  2. Move OmniBox/ folder to an ASCII path, e.g.:" -ForegroundColor $ColorWarning
    Write-Host "     D:\OmniBox\OmniBox.exe" -ForegroundColor $ColorInfo
}
if (-not $upxFound) {
    Write-Host "`n[TIP] Install UPX for smaller binary: https://github.com/upx/upx/releases" -ForegroundColor $ColorWarning
}