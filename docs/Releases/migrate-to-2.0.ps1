<#
.SYNOPSIS
    Cubby 1.4.0 → 2.0 数据迁移脚本（默认只预演）。

.DESCRIPTION
    1.4.0 把可写数据放在可执行文件旁边（`.config/` 与 `data/`），2.0 把数据根改成
    %LOCALAPPDATA%\Cubby。换版本后旧数据不会被自动读取，需要搬一次。

    用法（`--` 与 `-` 前缀等价）：

        .\migrate-to-2.0.ps1 [--source <旧安装目录>] [--dest <数据根>]
                             [--apply] [--include-logs] [--include-plugins]
                             [--force] [--move]

    参数：
      --source <目录>    旧安装目录：含 `.config` 或 `data` 的那一层。不给时依次试探
                         脚本所在目录、它的上一级；都不像旧安装目录就报错。
      --dest <目录>      目标数据根。默认 %LOCALAPPDATA%\Cubby。
      --apply            真正写盘。不加时只预演，不改任何文件。
      --include-logs     连 `.config/logs/` 一起搬。默认不搬（诊断信息，2.0 会重写）。
      --include-plugins  连旧 `plugins/` 一起搬。默认不搬，理由见下。
      --force            目标已有同名文件时，先备份到
                         <dest>\.pre-migrate-backup-<yyyyMMdd-HHmmss>\ 再覆盖；
                         不加时同名文件跳过并计数。
      --move             迁移完成后删除已迁移的源文件（隐含 --apply；未迁移的 webview/logs 保留）。
      --help             打印这段说明。

    搬什么：
      `.config/`（不含 webview，默认不含 logs）与 `data/` 整个目录。

    不搬什么：
      `.config/webview/`   WebView2 的浏览器 profile 与 HTTP 缓存，2.0 会重建。
      `.config/logs/`      默认不搬，要留历史日志时加 --include-logs。
      `plugins/`           默认不搬，理由见下。
      媒体库原文件          在你选定的目录里，不迁移、不移动；脚本只搬数据根里的状态。

    为什么不默认搬 plugins：
      1.4.0 的 `plugins/` 是"内置插件副本 + 用户自装插件"混在一起的一层；2.0 的插件
      搜索顺序是 `<数据根>/plugins` 优先、`<_MEIPASS>/plugins` 其次。整目录搬过去会
      让旧内置插件盖住 2.0 自带的那一份，表现是插件界面还在、后端接口对不上。所以
      默认只列出旧 `plugins/` 的内容供你判断，确实要带用户自装插件时才加
      --include-plugins（复制后请手工删掉与 2.0 内置插件同名的目录）。

    退出码：0 成功（含预演）；1 参数或前置检查失败；2 有复制失败项。

    日志：%TEMP%\cubby-migrate-<时间戳>.log。

    首次使用请先不加 --apply 预演一遍，核对清单与体积后再落盘。
#>

# 输出编码：先设成 UTF-8 再输出任何东西，避免中文在非 UTF-8 代码页的控制台里变乱码。
# 这一段必须在任何 Write-Host 之前执行。
[Console]::OutputEncoding = New-Object -TypeName 'System.Text.UTF8Encoding' -ArgumentList $false

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:Utf8NoBom = New-Object -TypeName 'System.Text.UTF8Encoding' -ArgumentList $false
$script:LogPath = Join-Path ([System.IO.Path]::GetTempPath().TrimEnd('\')) ('cubby-migrate-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
$script:Failures = 0
$script:CopiedFiles = 0
$script:CopiedBytes = [int64]0
$script:SkippedFiles = 0
$script:BackedUpFiles = 0
$script:BackupRoot = ''

$script:UsageLines = @(
    '用法：',
    '  powershell -NoProfile -ExecutionPolicy Bypass -File migrate-to-2.0.ps1 [参数]',
    '',
    '参数（-- 与 - 前缀等价）：',
    '  --source <目录>    旧安装目录（含 .config 或 data 的那一层）；默认按脚本位置推断',
    '  --dest <目录>      目标数据根；默认 %LOCALAPPDATA%\Cubby',
    '  --apply            真正写盘；不加时只预演',
    '  --include-logs     连 .config/logs 一起搬（默认不搬）',
    '  --include-plugins  连旧 plugins 一起搬（默认不搬：会盖住 2.0 的内置插件）',
    '  --force            目标同名文件先备份到 <dest>\.pre-migrate-backup-<时间戳>\ 再覆盖',
    '  --move             迁移完成后删除已迁移的源文件（隐含 --apply；未迁移的 webview/logs 保留）',
    '  --help             打印这段说明',
    '',
    '退出码：0 成功（含预演）；1 参数或前置检查失败；2 有复制失败项'
)

function Write-Log {
    param([string]$Text, [string]$Color = 'Gray')
    Write-Host $Text -ForegroundColor $Color
    [System.IO.File]::AppendAllText($script:LogPath, $Text + "`r`n", $script:Utf8NoBom)
}

function Fail {
    param([string]$Message)
    Write-Log ('错误：' + $Message) 'Red'
    foreach ($line in $script:UsageLines) { Write-Log $line 'DarkGray' }
    Write-Log ('日志：' + $script:LogPath) 'DarkGray'
    exit 1
}

function Format-Size {
    param([int64]$Bytes)
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N2} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N1} KB' -f ($Bytes / 1KB)) }
    return ([string]$Bytes + ' B')
}

function Resolve-AbsolutePath {
    param([string]$Path)
    $expanded = [Environment]::ExpandEnvironmentVariables($Path.Trim())
    if (-not [System.IO.Path]::IsPathRooted($expanded)) {
        $expanded = Join-Path (Get-Location).Path $expanded
    }
    $full = [System.IO.Path]::GetFullPath($expanded)
    while ($full.Length -gt 3 -and $full.EndsWith('\')) { $full = $full.Substring(0, $full.Length - 1) }
    return $full
}

function Test-LegacyDataRoot {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    if (Test-Path -LiteralPath (Join-Path $Path '.config') -PathType Container) { return $true }
    if (Test-Path -LiteralPath (Join-Path $Path 'data') -PathType Container) { return $true }
    return $false
}

# 把一个数据根下的子项（.config / data / plugins）枚举成"逐文件计划"。
# 计划在复制之前一次算完：预演只读它，落盘按它执行，两者因此不会给出不同结论。
function New-PlanItem {
    param(
        [string]$Name,
        [string]$SourceRoot,
        [string]$DestRoot,
        [bool]$IncludeLogs
    )

    $root = Join-Path $SourceRoot $Name
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { return $null }

    $files = New-Object System.Collections.ArrayList
    $dirs = New-Object System.Collections.ArrayList
    $excluded = New-Object System.Collections.ArrayList
    $entries = @()

    try {
        $entries = @(Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction Stop)
    } catch {
        Fail ('读取源目录失败：' + $root + '（' + $_.Exception.Message + '）')
    }

    $destItemRoot = Join-Path $DestRoot $Name
    foreach ($entry in $entries) {
        $rel = $entry.FullName.Substring($root.Length).TrimStart('\')
        if ($rel -eq '') { continue }
        $first = ($rel -split '[\\/]')[0]
        $skip = $false
        if ($Name -eq '.config') {
            # webview 是浏览器 profile 与 HTTP 缓存；logs 是诊断日志。两者都与"设置"无关，
            # 搬过去只会拖慢迁移，2.0 启动后自行重建。
            if ($first -ieq 'webview') { $skip = $true }
            if ($first -ieq 'logs' -and -not $IncludeLogs) { $skip = $true }
        }
        if ($skip) {
            if (-not $excluded.Contains($first)) { [void]$excluded.Add($first) }
            continue
        }

        $destPath = Join-Path $destItemRoot $rel
        if ($entry.PSIsContainer) {
            [void]$dirs.Add(@{ Rel = $rel; Dest = $destPath })
        } else {
            # 目标是"任何已存在的条目"都算冲突：若目标位置被一个同名目录占住，
            # 直接 Copy-Item 会把文件塞进那个目录里（得到 app.yaml\app.yaml），
            # 表面上复制成功、实际上落错了地方。这里按存在/是目录分开记，
            # 预演与落盘才对同一种状态给出同一个结论（冲突项预演时就报出来）。
            $isDir = Test-Path -LiteralPath $destPath -PathType Container
            [void]$files.Add(@{
                Rel = ($Name + '\' + $rel)
                Src = $entry.FullName
                Dest = $destPath
                Size = [int64]$entry.Length
                Exists = (Test-Path -LiteralPath $destPath)
                IsDir = $isDir
            })
        }
    }

    return @{
        Name = $Name
        Source = $root
        DestDir = $destItemRoot
        Files = @($files)
        Dirs = @($dirs)
        Excluded = @($excluded)
        Copied = 0
        Skipped = 0
        Failed = 0
    }
}

function Get-FileAction {
    param($File, [bool]$Force)
    if ($File.IsDir) { return '冲突' }
    if (-not $File.Exists) { return '复制' }
    if ($Force) { return '覆盖' }
    return '跳过'
}

# ── 参数解析 ────────────────────────────────────────────────────────────────
# 手写解析而不是 param() 块：参数名带连字符（--include-logs）在 param() 里要额外挂
# 别名，而"未知参数/缺值"的报错文本也不受控 —— 这里要自己给出用法与退出码。
$options = @{
    Source = ''
    Dest = ''
    Apply = $false
    IncludeLogs = $false
    IncludePlugins = $false
    Force = $false
    Move = $false
    Help = $false
}

$rawArgs = @($args)
$pos = 0
while ($pos -lt $rawArgs.Count) {
    $token = [string]$rawArgs[$pos]
    if (-not $token.StartsWith('-')) { Fail ('无法识别的参数：' + $token) }

    $name = $token
    $inline = ''
    $eqIndex = $token.IndexOf('=')
    if ($eqIndex -gt 0) {
        $name = $token.Substring(0, $eqIndex)
        $inline = $token.Substring($eqIndex + 1)
    }
    if ($name.StartsWith('--')) { $name = $name.Substring(2) } else { $name = $name.Substring(1) }
    $key = $name.ToLowerInvariant()

    switch ($key) {
        { $_ -eq 'source' } {
            if ($inline -ne '') { $options.Source = $inline }
            else {
                if ($pos + 1 -ge $rawArgs.Count) { Fail '--source 后面要跟一个目录' }
                $pos++
                $options.Source = [string]$rawArgs[$pos]
            }
        }
        { $_ -eq 'dest' } {
            if ($inline -ne '') { $options.Dest = $inline }
            else {
                if ($pos + 1 -ge $rawArgs.Count) { Fail '--dest 后面要跟一个目录' }
                $pos++
                $options.Dest = [string]$rawArgs[$pos]
            }
        }
        { $_ -eq 'apply' } { $options.Apply = $true }
        { $_ -eq 'include-logs' } { $options.IncludeLogs = $true }
        { $_ -eq 'include-plugins' } { $options.IncludePlugins = $true }
        { $_ -eq 'force' } { $options.Force = $true }
        { $_ -eq 'move' } { $options.Move = $true }
        { $_ -eq 'help' -or $_ -eq 'h' -or $_ -eq '?' } { $options.Help = $true }
        default { Fail ('未知参数：' + $token) }
    }
    $pos++
}

if ($options.Help) {
    foreach ($line in $script:UsageLines) { Write-Log $line 'Cyan' }
    exit 0
}

# --move 的目的就是"搬完删源"，不给 --apply 时按落盘处理，并在输出里明确写出来。
$moveImpliedApply = $false
if ($options.Move -and -not $options.Apply) {
    $options.Apply = $true
    $moveImpliedApply = $true
}

$apply = [bool]$options.Apply
$force = [bool]$options.Force
$move = [bool]$options.Move
$includeLogs = [bool]$options.IncludeLogs
$includePlugins = [bool]$options.IncludePlugins

# ── 1. 源目录 ────────────────────────────────────────────────────────────────
if ($options.Source -ne '') {
    $source = Resolve-AbsolutePath $options.Source
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        Fail ('源目录不存在：' + $source)
    }
    if (-not (Test-LegacyDataRoot $source)) {
        Fail ('源目录里既没有 .config 也没有 data，不像 1.4.0 的数据位置：' + $source)
    }
} else {
    $scriptDir = $PSScriptRoot
    if ([string]::IsNullOrEmpty($scriptDir)) { $scriptDir = (Get-Location).Path }
    $parentDir = Split-Path -Parent $scriptDir
    if (Test-LegacyDataRoot $scriptDir) {
        $source = Resolve-AbsolutePath $scriptDir
    } elseif (Test-LegacyDataRoot $parentDir) {
        $source = Resolve-AbsolutePath $parentDir
    } else {
        Fail ('没有 --source，并且脚本所在目录（' + $scriptDir + '）与它的上一级都不含 .config/data。请用 --source 指到旧安装目录')
    }
}

# ── 2. 目标数据根 ───────────────────────────────────────────────────────────
if ($options.Dest -ne '') {
    $dest = Resolve-AbsolutePath $options.Dest
} else {
    $base = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($base)) { $base = $env:APPDATA }
    if ([string]::IsNullOrWhiteSpace($base)) {
        Fail '读不到 %LOCALAPPDATA% 与 %APPDATA%，请用 --dest 指定数据根'
    }
    $dest = Resolve-AbsolutePath (Join-Path $base 'Cubby')
}

if ($dest -ieq $source) {
    Fail ('源目录与目标数据根是同一个目录：' + $dest + '。2.0 的数据根不能是旧安装目录')
}
if ($dest.StartsWith($source + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    Fail ('目标数据根在源目录里面：' + $dest + '。先把 --source 指到别处，或换一个 --dest')
}
if ($source.StartsWith($dest + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    Fail ('源目录在目标数据根里面：' + $source + '。换一个 --dest')
}

Write-Log ('=' * 62) 'Cyan'
Write-Log '  Cubby 1.4.0 → 2.0 数据迁移' 'Cyan'
Write-Log ('=' * 62) 'Cyan'
Write-Log ('  旧数据（源）  : ' + $source) 'Cyan'
Write-Log ('  数据根（目标）: ' + $dest) 'Cyan'
if ($apply) {
    Write-Log '  模式          : 落盘（--apply）' 'Yellow'
} else {
    Write-Log '  模式          : 预演（dry-run，不改任何文件；加 --apply 才落盘）' 'Yellow'
}
if ($moveImpliedApply) { Write-Log '  注意          : --move 隐含 --apply，迁移完成后删除源目录里的对应内容' 'Yellow' }
Write-Log ('  日志          : ' + $script:LogPath) 'DarkGray'

# ── 3. 前置检查：进程 ───────────────────────────────────────────────────────
# 1.4.0 的进程名是 OmniBox，2.0 是 Cubby。两个都查：升级窗口里两种都可能还开着，
# 而它们正在写的文件被复制走只会得到"一半的状态"。
$running = @(Get-Process -Name 'Cubby', 'OmniBox' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    $list = ($running | ForEach-Object { $_.ProcessName + '(' + $_.Id + ')' }) -join '、'
    Fail ('检测到正在运行的进程：' + $list + '。请先退出 Cubby / OmniBox（含托盘图标）再迁移：运行中的实例可能正在写这些文件')
}

# ── 4. 计划 ─────────────────────────────────────────────────────────────────
$plan = @()
foreach ($itemName in @('.config', 'data')) {
    $item = New-PlanItem -Name $itemName -SourceRoot $source -DestRoot $dest -IncludeLogs $includeLogs
    if ($null -ne $item) { $plan += $item }
}

$pluginsRoot = Join-Path $source 'plugins'
$hasPlugins = Test-Path -LiteralPath $pluginsRoot -PathType Container
if ($hasPlugins -and $includePlugins) {
    $pluginsItem = New-PlanItem -Name 'plugins' -SourceRoot $source -DestRoot $dest -IncludeLogs $includeLogs
    if ($null -ne $pluginsItem) { $plan += $pluginsItem }
}

$totalFiles = 0
foreach ($item in $plan) { $totalFiles += $item.Files.Count }
if ($totalFiles -eq 0) {
    Write-Log ''
    Write-Log '源目录里没有可迁移的文件（.config 可能只有 webview/logs 这类缓存）。' 'Yellow'
}

# 体积分两本账：待写的字节（决定磁盘空间），以及目标已有、默认要跳过的字节。
$bytesToWrite = [int64]0
$bytesSkipped = [int64]0
foreach ($item in $plan) {
    foreach ($file in $item.Files) {
        if ($file.Exists -and -not $force) { $bytesSkipped += $file.Size } else { $bytesToWrite += $file.Size }
    }
}
if ($force) {
    # 覆盖前要先复制一份备份，备份同样占目标磁盘。
    $bytesToWrite = $bytesToWrite * 2
}

# ── 5. 前置检查：磁盘空间 ───────────────────────────────────────────────────
if ($bytesToWrite -gt 0) {
    $freeBytes = $null
    $driveRoot = [System.IO.Path]::GetPathRoot($dest)
    if ($driveRoot -match '^[A-Za-z]:\\$') {
        $driveName = $driveRoot.Substring(0, 1)
        try {
            $drive = Get-PSDrive -Name $driveName -PSProvider FileSystem -ErrorAction Stop
            if ($null -ne $drive.Free) { $freeBytes = [int64]$drive.Free }
        } catch {
            $freeBytes = $null
        }
    }
    if ($null -eq $freeBytes) {
        # 网络盘、SUBST 盘符、权限受限时读不到剩余空间：不猜，如实说明后继续。
        Write-Log ('  磁盘空间      : 读不到 ' + $dest + ' 的剩余空间，跳过这项检查') 'DarkYellow'
    } else {
        Write-Log ('  磁盘空间      : 需要约 ' + (Format-Size $bytesToWrite) + '，' + $driveRoot + ' 剩余 ' + (Format-Size $freeBytes)) 'Gray'
        if ($freeBytes -lt $bytesToWrite) {
            Fail ('目标磁盘空间不足：需要约 ' + (Format-Size $bytesToWrite) + '，' + $driveRoot + ' 只剩 ' + (Format-Size $freeBytes))
        }
    }
}

# ── 6. 逐项清单 ─────────────────────────────────────────────────────────────
$listEveryFile = $totalFiles -le 80
foreach ($item in $plan) {
    $writeBytes = [int64]0
    $skipBytes = [int64]0
    $willCopy = 0
    $willSkip = 0
    $willConflict = 0
    foreach ($file in $item.Files) {
        $action = Get-FileAction $file $force
        if ($action -eq '跳过') {
            $willSkip++
            $skipBytes += $file.Size
        } elseif ($action -eq '冲突') {
            $willConflict++
        } else {
            $willCopy++
            $writeBytes += $file.Size
        }
    }

    Write-Log ''
    Write-Log ('[' + $item.Name + '] ' + $item.Source) 'Cyan'
    Write-Log ('      → ' + $item.DestDir) 'Cyan'
    $verb = if ($apply) { '将写入' } else { '将复制' }
    Write-Log ('      ' + $verb + ' ' + $willCopy + ' 个文件 / ' + (Format-Size $writeBytes) + '；跳过 ' + $willSkip + ' 个 / ' + (Format-Size $skipBytes) + '（目标已有同名文件，--force 才覆盖）') 'Gray'
    if ($willConflict -gt 0) {
        Write-Log ('      冲突 ' + $willConflict + ' 个：目标位置是目录，无法覆盖，需先删除或改名') 'Red'
    }
    if ($item.Excluded.Count -gt 0) {
        Write-Log ('      不搬：' + ($item.Excluded -join '、') + '（webview 是浏览器缓存；logs 要保留请加 --include-logs）') 'DarkYellow'
    }

    if ($listEveryFile) {
        foreach ($file in $item.Files) {
            $action = Get-FileAction $file $force
            Write-Log ('      [' + $action + '] ' + $file.Rel.Replace($item.Name + '\', '') + '  ' + (Format-Size $file.Size)) 'DarkGray'
        }
    } else {
        Write-Log ('      文件较多，逐文件清单只进日志（' + $script:LogPath + '）') 'DarkGray'
    }

    # 完整清单始终进日志：屏幕只给一眼能看完的部分，审计要能回溯每一处。
    [System.IO.File]::AppendAllText($script:LogPath, '[清单] ' + $item.Name + ' -> ' + $item.DestDir + "`r`n", $script:Utf8NoBom)
    foreach ($file in $item.Files) {
        $action = Get-FileAction $file $force
        [System.IO.File]::AppendAllText($script:LogPath, ('  [' + $action + '] ' + $file.Rel + '  ' + (Format-Size $file.Size) + "`r`n"), $script:Utf8NoBom)
    }
}

# ── 7. plugins 的单独说明 ───────────────────────────────────────────────────
if ($hasPlugins -and -not $includePlugins) {
    $pluginNames = @(Get-ChildItem -LiteralPath $pluginsRoot -Directory -Force -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
    Write-Log ''
    Write-Log ('[plugins] 默认不迁移：' + $pluginsRoot) 'Yellow'
    if ($pluginNames.Count -gt 0) {
        Write-Log ('      目录里有 ' + $pluginNames.Count + ' 项：' + ($pluginNames -join '、')) 'Gray'
    }
    Write-Log '      旧 plugins 是内置插件副本与用户自装插件混在一起的一层；2.0 的插件搜索顺序让' 'Yellow'
    Write-Log '      <数据根>/plugins 优先于内置插件，整目录搬过去会盖住 2.0 自带的那一份。' 'Yellow'
    Write-Log '      确认要带用户自装插件时加 --include-plugins，并在复制后删掉与 2.0 内置插件同名的目录。' 'Yellow'
} elseif ($hasPlugins -and $includePlugins) {
    Write-Log ''
    Write-Log '注意：--include-plugins 会把旧 plugins 整目录复制到 <数据根>\plugins。' 'Yellow'
    Write-Log '      该目录优先级高于 2.0 内置插件，旧的内置插件副本会覆盖 2.0 的同名插件。' 'Yellow'
    Write-Log '      只想要用户自装插件时，复制后手工删掉与 2.0 内置插件同名的目录。' 'Yellow'
}

# ── 8. 执行或结束预演 ───────────────────────────────────────────────────────
if (-not $apply) {
    Write-Log ''
    Write-Log ('预演结束：没有改动任何文件。确认无误后加 --apply 再跑一次：') 'Green'
    Write-Log ('  .\migrate-to-2.0.ps1 --source "' + $source + '" --dest "' + $dest + '" --apply') 'Green'
    Write-Log ('日志：' + $script:LogPath) 'DarkGray'
    exit 0
}

foreach ($item in $plan) {
    Write-Log ''
    Write-Log ('迁移 [' + $item.Name + '] ...') 'Cyan'

    # 先建目录骨架：源里的空目录也要在目标里存在，否则 2.0 首次写入时可能落到别的层级。
    foreach ($dir in $item.Dirs) {
        if (-not (Test-Path -LiteralPath $dir.Dest)) {
            try {
                New-Item -ItemType Directory -Path $dir.Dest -Force -ErrorAction Stop | Out-Null
            } catch {
                $item.Failed++
                $script:Failures++
                Write-Log ('  建目录失败：' + $dir.Dest + '（' + $_.Exception.Message + '）') 'Red'
            }
        }
    }

    foreach ($file in $item.Files) {
        if ($file.IsDir) {
            # 目标位置被目录占住：Copy-Item 会把文件塞进那个目录（得到 app.yaml\app.yaml），
            # 不报错但落错地方。--force 也不处理这种形态，交给人工决定。
            $item.Failed++
            $script:Failures++
            Write-Log ('  目标位置是目录，无法覆盖：' + $file.Rel + '（先删除或改名它再跑一次）') 'Red'
            continue
        }

        if ($file.Exists -and -not $force) {
            $item.Skipped++
            $script:SkippedFiles++
            Write-Log ('  跳过（目标已存在）：' + $file.Rel) 'DarkYellow'
            continue
        }

        if ($file.Exists) {
            # --force：先把目标那一份备份出来，再覆盖。备份目录用时间戳，多次运行不会互相覆盖。
            if ($script:BackupRoot -eq '') {
                $script:BackupRoot = Join-Path $dest ('.pre-migrate-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
                Write-Log ('  备份目录：' + $script:BackupRoot) 'Yellow'
            }
            $backupFile = Join-Path $script:BackupRoot $file.Rel
            $backupParent = Split-Path -Parent $backupFile
            try {
                if (-not (Test-Path -LiteralPath $backupParent)) {
                    New-Item -ItemType Directory -Path $backupParent -Force -ErrorAction Stop | Out-Null
                }
                Copy-Item -LiteralPath $file.Dest -Destination $backupFile -Force -ErrorAction Stop
                $script:BackedUpFiles++
            } catch {
                $item.Failed++
                $script:Failures++
                Write-Log ('  备份失败，未覆盖：' + $file.Rel + '（' + $_.Exception.Message + '）') 'Red'
                continue
            }
        }

        $parent = Split-Path -Parent $file.Dest
        try {
            if (-not (Test-Path -LiteralPath $parent)) {
                New-Item -ItemType Directory -Path $parent -Force -ErrorAction Stop | Out-Null
            }
            Copy-Item -LiteralPath $file.Src -Destination $file.Dest -Force -ErrorAction Stop
            $item.Copied++
            $script:CopiedFiles++
            $script:CopiedBytes += $file.Size
        } catch {
            $item.Failed++
            $script:Failures++
            Write-Log ('  复制失败：' + $file.Rel + '（' + $_.Exception.Message + '）') 'Red'
        }
    }

    Write-Log ('  完成：复制 ' + $item.Copied + ' 个、跳过 ' + $item.Skipped + ' 个、失败 ' + $item.Failed + ' 个') 'Gray'
}

# ── 9. --move：删源 ─────────────────────────────────────────────────────────
if ($move) {
    Write-Log ''
    Write-Log '清理源目录（--move）' 'Yellow'
    foreach ($item in $plan) {
        if ($item.Failed -gt 0) {
            Write-Log ('  保留 ' + $item.Source + '：有 ' + $item.Failed + ' 个文件没有成功复制') 'Yellow'
            continue
        }
        if ($item.Skipped -gt 0) {
            Write-Log ('  保留 ' + $item.Source + '：有 ' + $item.Skipped + ' 个文件因目标已存在被跳过，加 --force 覆盖后再删') 'Yellow'
            continue
        }

        # 只删"确实已经迁移过去"的源文件，再自底向上清空目录。
        # 不整目录删除：默认不搬的 webview / logs 就在 .config 里，整目录删会把
        # 清单上写着"不搬"的缓存与日志一起删掉，用户看到的行为与清单不一致。
        $deleted = 0
        foreach ($file in $item.Files) {
            try {
                if (Test-Path -LiteralPath $file.Dest) {
                    Remove-Item -LiteralPath $file.Src -Force -ErrorAction Stop
                    $deleted++
                }
            } catch {
                $script:Failures++
                Write-Log ('  删除源文件失败：' + $file.Rel + '（' + $_.Exception.Message + '）') 'Red'
            }
        }

        # 由深到浅删空目录；目录里还留着未迁移的内容（webview / logs / plugins）就保留。
        $dirsByDepth = @($item.Dirs | Sort-Object { $_.Rel.Length } -Descending)
        foreach ($dir in $dirsByDepth) {
            $srcDir = Join-Path $item.Source $dir.Rel
            if (-not (Test-Path -LiteralPath $srcDir -PathType Container)) { continue }
            $left = @(Get-ChildItem -LiteralPath $srcDir -Force -ErrorAction SilentlyContinue)
            if ($left.Count -eq 0) {
                Remove-Item -LiteralPath $srcDir -Force -ErrorAction SilentlyContinue
            }
        }

        if ($item.Excluded.Count -gt 0) {
            Write-Log ('  已保留未迁移的内容：' + ($item.Excluded -join '、') + '（webview 为浏览器缓存；logs 想一起删请加 --include-logs）') 'DarkYellow'
        }
        Write-Log ('  已删除已迁移的源文件：' + $deleted + ' 个（' + $item.Source + '）') 'DarkGray'

        # 整项都搬走且没有剩余的项（例如只有设置文件的 data）：连空目录一起收掉。
        $rootLeft = @(Get-ChildItem -LiteralPath $item.Source -Force -ErrorAction SilentlyContinue)
        if ($rootLeft.Count -eq 0) {
            Remove-Item -LiteralPath $item.Source -Force -ErrorAction SilentlyContinue
            Write-Log ('  已删除空的源目录：' + $item.Source) 'DarkGray'
        }
    }
    if ($hasPlugins -and -not $includePlugins) {
        Write-Log ('  保留 ' + $pluginsRoot + '：默认不迁移 plugins（见上）') 'Yellow'
    }
    $leftover = @(Get-ChildItem -LiteralPath $source -Force -ErrorAction SilentlyContinue)
    if ($leftover.Count -eq 0) {
        Write-Log ('  源目录已空，可整个删除：' + $source) 'DarkGray'
    } else {
        Write-Log ('  源目录还留着未迁移的内容：' + (($leftover | ForEach-Object { $_.Name }) -join '、')) 'DarkYellow'
    }
}

# ── 10. 迁移后校验与收尾 ────────────────────────────────────────────────────
Write-Log ''
$appJson = Join-Path (Join-Path $dest '.config') 'app.json'
$appYaml = Join-Path (Join-Path $dest '.config') 'app.yaml'
if ((Test-Path -LiteralPath $appJson -PathType Leaf) -or (Test-Path -LiteralPath $appYaml -PathType Leaf)) {
    Write-Log ('校验：' + (Join-Path $dest '.config') + ' 下有配置文件，2.0 启动后会用这份设置') 'Green'
} else {
    Write-Log ('警告：' + (Join-Path $dest '.config') + ' 下没有 app.json / app.yaml。2.0 会按首次运行处理（设置需要重选）') 'Yellow'
}

Write-Log ''
Write-Log ('=' * 62) 'Cyan'
Write-Log '  结果' 'Cyan'
Write-Log ('  复制      : ' + $script:CopiedFiles + ' 个文件 / ' + (Format-Size $script:CopiedBytes)) 'Cyan'
Write-Log ('  跳过      : ' + $script:SkippedFiles + ' 个（目标已有同名文件；--force 可覆盖，覆盖前会备份）') 'Cyan'
if ($script:BackedUpFiles -gt 0) {
    Write-Log ('  备份      : ' + $script:BackedUpFiles + ' 个 → ' + $script:BackupRoot) 'Cyan'
}
if ($script:Failures -gt 0) {
    Write-Log ('  失败      : ' + $script:Failures + ' 个（详见上面的红色行与日志）') 'Red'
}
Write-Log ''
Write-Log '  下次启动预期' 'Cyan'
Write-Log ('    数据根：' + $dest) 'Gray'
Write-Log '    1.4.0 不会读取这个位置；请安装 2.0 后从开始菜单或 Cubby.exe 启动，设置、令牌与索引都在数据根里。' 'Gray'
Write-Log '    想换到别的位置（比如便携用法）：设 CUBBY_HOME 或加 --dest 指向同一目录，再迁移一次。' 'Gray'
Write-Log ''
Write-Log '  卸载与数据' 'Cyan'
Write-Log '    卸载 2.0 默认保留数据根；卸载程序的询问里选“是”才会删除。' 'Gray'
Write-Log ('    媒体库原文件不在数据根里，本次迁移没有移动它们。') 'Gray'
Write-Log ('  日志：' + $script:LogPath) 'DarkGray'
Write-Log ('=' * 62) 'Cyan'

if ($script:Failures -gt 0) { exit 2 }
exit 0
