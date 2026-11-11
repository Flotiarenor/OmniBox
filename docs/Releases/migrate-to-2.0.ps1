<#
.SYNOPSIS
    Cubby 1.4.0 → 2.0 数据迁移脚本（默认只预演）。

.DESCRIPTION
    1.4.0 把可写数据放在可执行文件旁边（`.config/` 与 `data/`），2.0 把数据根改成
    %LOCALAPPDATA%\Cubby 或 `CUBBY_HOME` 指向的位置。换版本后旧数据不会被自动读取，
    需要搬一次。

    用法（`--` 与 `-` 前缀等价）：

        .\migrate-to-2.0.ps1 [--source <旧安装目录>] [--dest <数据根>]
                             [--apply] [--include-logs] [--include-plugins]
                             [--force] [--move]

    参数：
      --source <目录>    旧安装目录。必须是"像 1.4.0 安装目录"的那一层：脚本按四项特征
                         计分，至少命中两项才接受 —— `OmniBox.exe`、`.config/app.json`
                         或 `.config/app.yaml`、`.config/auth_token.txt`、
                         `data/group-mesh/identity/`。不给时依次试探脚本所在目录与它的
                         上一级，两处都不够分就报错。`--move` 必须显式给出本参数。
      --dest <目录>      目标数据根。不给时按 2.0 自己的判据取：先看环境变量
                         `CUBBY_HOME`，其次 `%LOCALAPPDATA%\Cubby`（缺 LOCALAPPDATA
                         时用 `%APPDATA%\Cubby`）。输出里写明这一次用的是哪个来源。
      --apply            真正写盘。不加时只预演，不改任何文件。
      --include-logs     连 `.config/logs/` 一起搬。默认不搬（诊断信息，2.0 会重写）。
      --include-plugins  连旧 `plugins/` 一起搬。默认不搬，理由见下。
      --force            目标已有同名文件时，先备份到
                         <dest>\.pre-migrate-backup-<yyyyMMdd-HHmmss>\ 再覆盖；
                         不加时同名文件跳过并计数。
      --move             迁移完成后删除源文件（隐含 --apply）。只有本次运行"零失败、
                         零跳过、身份密钥全部重封装成功"时才删；删之前把计划内的源文件
                         整份备份到 <dest>\.pre-migrate-backup-<时间戳>\source\ 并打印位置。
                         默认不搬的 webview/logs 保留。必须显式给 --source。
      --help             打印这段说明。

    搬什么：
      `.config/`（不含 webview，默认不含 logs）与 `data/` 整个目录。

    另外重封装的（不是复制）：
      `data/group-mesh/identity/principal.json` 与 `devices/*.json` 在 1.4.0 里用旧格式
      标识与旧 DPAPI 熵保护，2.0 只认新的，原样搬过去的表现是"身份读不出来"：日志报
      「字段 name 缺失或不是字符串」，界面显示未初始化，点初始化又因为目录里已有
      principal.json 被拒绝覆盖私钥。脚本在复制之后把目标数据根里的这类文件逐层剥开、
      按当前格式重新封装，并在收尾校验"身份真的能解出来"；原文件先备份到
      `<dest>\.pre-migrate-backup-<时间戳>\`。

      这一步依赖 DPAPI，而 DPAPI 的密文与 Windows 用户绑定：必须用**建立身份的那个
      用户**在本机运行。换用户或换机器都解不开（脚本会报"解保护失败"并保留原文件，
      不会静默跳过）。keyring / passphrase 保护的文件同样报出来交人工处理。

      判断"是不是明文"只认一种情形：文件能解析成 JSON 且没有 `format` / `data` 字段。
      解析不了（被截断、损坏、非 JSON）一律算失败：预演与落盘都报红、计入失败项、
      退出码为 2，不会当成"2.0 会自行升级的明文"放过。收尾校验要求 `name` 字段真能读出来。

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

    已知限制：
      路径总长超过 260 字符时，Windows PowerShell 5.1 调用的 .NET 文件 API 复制不了；
      这类文件会计入失败项（源文件保留），清单阶段会提前告警。遇到时把数据根换到较短的
      路径再迁移。
      旧配置里的 `directories.data_root` 被改成非默认值时，索引与身份可能不在 `data/` 下；
      脚本只搬默认位置的 `data/`，检测到这种配置会告警，需要手工处理。

    退出码：0 成功（含预演）；1 参数或前置检查失败；2 有失败项（复制、备份、
            身份密钥重封装、身份校验任一项失败）。

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
$script:VerifiedFiles = 0
$script:BackedUpFiles = 0
$script:BackupRoot = ''
$script:DestRoot = ''

$script:UsageLines = @(
    '用法：',
    '  powershell -NoProfile -ExecutionPolicy Bypass -File migrate-to-2.0.ps1 [参数]',
    '',
    '参数（-- 与 - 前缀等价）：',
    '  --source <目录>    旧安装目录；需命中至少两项 1.4.0 特征（OmniBox.exe、',
    '                     .config/app.json 或 app.yaml、.config/auth_token.txt、',
    '                     data/group-mesh/identity）；默认按脚本位置推断',
    '  --dest <目录>      目标数据根；默认 CUBBY_HOME，其次 %LOCALAPPDATA%\Cubby',
    '  --apply            真正写盘；不加时只预演',
    '  --include-logs     连 .config/logs 一起搬（默认不搬）',
    '  --include-plugins  连旧 plugins 一起搬（默认不搬：会盖住 2.0 的内置插件）',
    '  --force            目标同名文件先备份到 <dest>\.pre-migrate-backup-<时间戳>\ 再覆盖',
    '  --move             迁移完成后删除源文件（隐含 --apply；必须显式给 --source）',
    '                     只有本次运行零失败零跳过才删，删前把计划内的源文件备份到',
    '                     <dest>\.pre-migrate-backup-<时间戳>\source\ 并打印位置',
    '  --help             打印这段说明',
    '',
    '另外：data\group-mesh\identity 下的密钥文件会从 1.4.0 的信封重封装成 2.0 的格式',
    '      （DPAPI，需在同一台机器上用建立身份的那个 Windows 用户运行；原文件备份在',
    '      <dest>\.pre-migrate-backup-<时间戳>\；损坏或解不开的文件报红并计入失败）',
    '',
    '退出码：0 成功（含预演）；1 参数或前置检查失败；2 有失败项'
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

# ── 1.4.0 安装目录识别 ──────────────────────────────────────────────────────
# 只看"有没有 .config 或 data"会把任何带 data 目录的目录当成旧安装目录：配上 --move
# 就是把无关文件复制走再删掉。这里按 1.4.0 安装目录的四项特征计分，至少两项才算。
function Get-LegacyRootEvidence {
    param([string]$Path)

    $found = New-Object System.Collections.ArrayList
    if ([string]::IsNullOrWhiteSpace($Path)) { return @{ Found = @(); Count = 0 } }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return @{ Found = @(); Count = 0 }
    }

    # 1. 旧可执行文件（Windows 文件系统大小写不敏感，一个名字就够）
    if (Test-Path -LiteralPath (Join-Path $Path 'OmniBox.exe') -PathType Leaf) {
        [void]$found.Add('OmniBox.exe')
    }
    # 2. 主配置（json 与 yaml 是同一项的两种存量写法，只算一项）
    $configDir = Join-Path $Path '.config'
    foreach ($name in @('app.json', 'app.yaml')) {
        if (Test-Path -LiteralPath (Join-Path $configDir $name) -PathType Leaf) {
            [void]$found.Add('.config\' + $name)
            break
        }
    }
    # 3. 鉴权令牌：跑过一次就会有
    if (Test-Path -LiteralPath (Join-Path $configDir 'auth_token.txt') -PathType Leaf) {
        [void]$found.Add('.config\auth_token.txt')
    }
    # 4. 团体组网身份：2.0 需要重封装的就是这里
    $identityDir = Join-Path (Join-Path (Join-Path $Path 'data') 'group-mesh') 'identity'
    if (Test-Path -LiteralPath $identityDir -PathType Container) {
        [void]$found.Add('data\group-mesh\identity')
    }
    return @{ Found = @($found); Count = $found.Count }
}

function Format-LegacyEvidence {
    param($Evidence)
    if ($null -eq $Evidence) { return '（无 1.4.0 特征）' }
    $found = @($Evidence.Found)
    if ($found.Count -eq 0) { return '（无 1.4.0 特征）' }
    return ($found -join '、')
}

# 旧配置把 directories.data_root 改成非默认值时，索引与身份可能不在 `data/` 下：
# 脚本只搬默认位置，检测到就告警（不改变行为，避免"猜"出一套路径）。
function Get-LegacyDataRootOverride {
    param([string]$Path)

    $configDir = Join-Path $Path '.config'
    $jsonPath = Join-Path $configDir 'app.json'
    if (Test-Path -LiteralPath $jsonPath -PathType Leaf) {
        try {
            $config = ([System.IO.File]::ReadAllText($jsonPath) | ConvertFrom-Json -ErrorAction Stop)
            $directories = $config.PSObject.Properties['directories']
            if ($null -ne $directories) {
                $dataRoot = $directories.Value.PSObject.Properties['data_root']
                if ($null -ne $dataRoot) { return [string]$dataRoot.Value }
            }
        } catch {
            return ''
        }
        return ''
    }
    $yamlPath = Join-Path $configDir 'app.yaml'
    if (Test-Path -LiteralPath $yamlPath -PathType Leaf) {
        try {
            foreach ($line in [System.IO.File]::ReadAllLines($yamlPath)) {
                if ($line -match '^\s*data_root\s*:\s*(.+?)\s*$') {
                    return $Matches[1].Trim('"', "'")
                }
            }
        } catch {
            return ''
        }
    }
    return ''
}

function Test-DefaultDataRoot {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
    $normalized = $Value.Trim().Replace('/', '\')
    if ($normalized.StartsWith('.\')) { $normalized = $normalized.Substring(2) }
    return ($normalized -ieq 'data')
}

# 备份目录：一次运行只建一个；时间戳后缀冲突时递增，避免同一秒跑两次把上一份覆盖。
function Get-BackupRoot {
    if ($script:BackupRoot -ne '') { return $script:BackupRoot }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $candidate = Join-Path $script:DestRoot ('.pre-migrate-backup-' + $stamp)
    $suffix = 1
    while (Test-Path -LiteralPath $candidate) {
        $suffix++
        if ($suffix -gt 99) { Fail ('备份目录名冲突过多：' + $candidate) }
        $candidate = Join-Path $script:DestRoot ('.pre-migrate-backup-' + $stamp + '-' + $suffix)
    }
    New-Item -ItemType Directory -Path $candidate -Force -ErrorAction Stop | Out-Null
    $script:BackupRoot = $candidate
    Write-Log ('  备份目录：' + $script:BackupRoot) 'Yellow'
    return $script:BackupRoot
}

# --move 删源前的整份备份：把计划内的源文件按同样相对路径复制到 <备份目录>\source\。
# 只备份计划内的文件 —— 默认不搬的 webview/logs 不在计划里，也不会被删，无需备份。
function Save-ItemSourceBackup {
    param($Item)

    $root = Get-BackupRoot
    $base = Join-Path $root 'source'
    foreach ($file in $Item.Files) {
        $target = Join-Path $base $file.Rel
        $parent = Split-Path -Parent $target
        try {
            if (-not (Test-Path -LiteralPath $parent)) {
                New-Item -ItemType Directory -Path $parent -Force -ErrorAction Stop | Out-Null
            }
            Copy-Item -LiteralPath $file.Src -Destination $target -Force -ErrorAction Stop
            if ((Get-Item -LiteralPath $file.Src).Length -ne (Get-Item -LiteralPath $target).Length) {
                throw ('备份长度不一致：' + $file.Rel)
            }
            $script:BackedUpFiles++
        } catch {
            Write-Log ('  源备份失败：' + $file.Rel + '（' + $_.Exception.Message + '）') 'Red'
            return ''
        }
    }
    return $base
}

# ── 身份密钥信封（1.4.0 → 2.0）───────────────────────────────────────────────
# 1.4.0 的 principal.json 与 devices/*.json 是"旧格式标识 + 旧 DPAPI 熵"的信封，
# 2.0 的 secret_store 只认新标识与新熵。判据与重封装都收在下面几个函数里，预演与
# 落盘用同一份实现，避免两边给出不同结论。
$script:DpapiLoadError = ''
try {
    # Windows PowerShell 5.1 不自动加载 System.Security，ProtectedData 需要显式引入；
    # PowerShell 7 已内置该类型。先按类型名探测，避免每次都触发一次程序集加载。
    if ($null -eq ('System.Security.Cryptography.ProtectedData' -as [type])) {
        Add-Type -AssemblyName System.Security -ErrorAction Stop
    }
} catch {
    $script:DpapiLoadError = $_.Exception.Message
}

$script:LegacySecretFormat = 'omnibox-secret-v1'
$script:CurrentSecretFormat = 'cubby-secret-v1'
$script:LegacySecretEntropy = 'omnibox.group-mesh.secret.v1'
$script:CurrentSecretEntropy = 'cubby.group-mesh.secret.v1'
$script:IdentityFailures = 0
$script:RewrappedKeys = 0

# 只列身份目录里**受保护**的那两个落点：principal.json 与 devices/*.json。
# roster / shares / registry 等是明文 JSON，不是信封，不能当密文重写。
function Get-IdentityKeyFiles {
    param([string]$DataRoot)

    $files = New-Object System.Collections.ArrayList
    $identityDir = Join-Path (Join-Path (Join-Path $DataRoot 'data') 'group-mesh') 'identity'
    if (-not (Test-Path -LiteralPath $identityDir -PathType Container)) { return @() }

    $principal = Join-Path $identityDir 'principal.json'
    if (Test-Path -LiteralPath $principal -PathType Leaf) {
        [void]$files.Add(@{
            Rel = 'data\group-mesh\identity\principal.json'
            Path = $principal
        })
    }

    $devicesDir = Join-Path $identityDir 'devices'
    if (Test-Path -LiteralPath $devicesDir -PathType Container) {
        foreach ($entry in @(Get-ChildItem -LiteralPath $devicesDir -Filter '*.json' -File -Force -ErrorAction SilentlyContinue)) {
            [void]$files.Add(@{
                Rel = ('data\group-mesh\identity\devices\' + $entry.Name)
                Path = $entry.FullName
            })
        }
    }
    return @($files)
}

# 逐层剥开信封，返回真身字节、层数与是否见过旧格式。只支持 dpapi：keyring /
# passphrase 在 PowerShell 侧没有对应实现，遇到就明确报出来交人工处理。
# 判据与重封装收在同一个函数组里，预演与落盘用同一份实现。
function Get-EnvelopeInfo {
    param([string]$Path)

    $result = @{
        Error = ''
        Layers = 0
        SawLegacy = $false
        Plain = [byte[]]@()
    }

    $current = [System.IO.File]::ReadAllBytes($Path)
    $result.Plain = $current

    if ($current.Length -eq 0) {
        $result.Error = '文件是空的（0 字节）'
        return $result
    }

    if ($script:DpapiLoadError -ne '') {
        $result.Error = ('当前 PowerShell 加载不了 System.Security.Cryptography.ProtectedData：' + $script:DpapiLoadError + '（请改用 Windows PowerShell 5.1 或 PowerShell 7）')
        return $result
    }

    $parseFailed = $false
    while ($true) {
        $obj = $null
        try {
            $obj = ([System.Text.Encoding]::UTF8.GetString($current) | ConvertFrom-Json -ErrorAction Stop)
        } catch {
            $parseFailed = $true
            break
        }
        if ($null -eq $obj) { $parseFailed = $true; break }
        $formatProp = $obj.PSObject.Properties['format']
        $dataProp = $obj.PSObject.Properties['data']
        if ($null -eq $formatProp -or $null -eq $dataProp) { break }

        $format = [string]$formatProp.Value
        $protectorProp = $obj.PSObject.Properties['protector']
        $protector = ''
        if ($null -ne $protectorProp) { $protector = [string]$protectorProp.Value }
        if ($protector -ne 'dpapi') {
            $result.Error = ('信封用 ' + $protector + ' 保护，本脚本只处理 dpapi')
            return $result
        }
        if ($format -eq $script:LegacySecretFormat) {
            $entropy = $script:LegacySecretEntropy
            $result.SawLegacy = $true
        } elseif ($format -eq $script:CurrentSecretFormat) {
            $entropy = $script:CurrentSecretEntropy
        } else {
            $result.Error = ('信封格式标识不认识：' + $format)
            return $result
        }

        try {
            $blob = [Convert]::FromBase64String([string]$dataProp.Value)
            $current = [System.Security.Cryptography.ProtectedData]::Unprotect(
                $blob,
                [System.Text.Encoding]::UTF8.GetBytes($entropy),
                [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        } catch {
            $result.Error = ('解保护失败（DPAPI 密文与 Windows 用户绑定，换用户或换机器都解不开）：' + $_.Exception.Message)
            return $result
        }

        $result.Layers++
        $result.Plain = $current
        if ($result.Layers -gt 4) {
            $result.Error = '信封层数超过 4 层，拒绝继续处理'
            return $result
        }
    }

    # 解析不了 = 文件坏了或内容不是 JSON。只有"能解析成 JSON 且没有 format/data 字段"
    # 这一种情形才算 1.4.0 的明文（交给 2.0 自行升级）；其余一律算失败，不能静默放过。
    if ($parseFailed) {
        if ($result.Layers -eq 0) {
            $result.Error = '文件不是合法 JSON（可能被截断或损坏），2.0 读不了它'
        } else {
            $result.Error = '信封解出来的内容不是合法 JSON（密文里的内容已损坏）'
        }
    }
    return $result
}

# 按当前格式重新封装：写回"一层新信封 + 原本的真身字节"，因此多层嵌套（旧信封被
# 上一版脚本二次包过）也会被压平成一层。JSON 交给 ConvertTo-Json 序列化，值不变。
function Set-KeyEnvelope {
    param([string]$Path, [byte[]]$Plain)

    $blob = [System.Security.Cryptography.ProtectedData]::Protect(
        $Plain,
        [System.Text.Encoding]::UTF8.GetBytes($script:CurrentSecretEntropy),
        [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    $envelope = [ordered]@{
        format = $script:CurrentSecretFormat
        protector = 'dpapi'
        data = [Convert]::ToBase64String($blob)
    }
    [System.IO.File]::WriteAllText($Path, (($envelope | ConvertTo-Json -Compress) + "`r`n"), $script:Utf8NoBom)
}

# 回读校验：能解出来、是 JSON、带 name 字段，才算 2.0 读得懂。空字符串表示通过。
# 明文（Layers==0）同样要过这一关：2.0 只在内容真的是身份 JSON 时才认它。
function Test-KeyEnvelopeReadable {
    param([string]$Path)

    $info = Get-EnvelopeInfo -Path $Path
    if ($info.Error -ne '') { return $info.Error }
    $plain = [byte[]]$info.Plain
    if ($plain.Length -eq 0) { return '内容是空的（0 字节）' }
    $obj = $null
    try {
        $obj = ([System.Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        return '解出来的内容不是 JSON'
    }
    if ($null -eq $obj -or $null -eq $obj.PSObject.Properties['name']) { return '解出来的内容没有 name 字段' }
    return ''
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

# 预演用的身份报告：对给定数据根逐文件分类并打印。预演把"源"与"目标"两处都过一遍，
# 避免出现"预演说重封装、落盘说无需处理"的两种结论。
function Write-IdentityReport {
    param([string]$DataRoot, [string]$Label)

    $items = @(Get-IdentityKeyFiles -DataRoot $DataRoot)
    if ($items.Count -eq 0) {
        Write-Log ('      ' + $Label + '：没有 data\group-mesh\identity，没有需要重封装的身份（首次安装属正常）') 'DarkGray'
        return @{ Legacy = 0; Problems = 0 }
    }
    $legacy = 0
    $problems = 0
    foreach ($target in $items) {
        $info = Get-EnvelopeInfo -Path $target.Path
        if ($info.Error -ne '') {
            $problems++
            Write-Log ('      [问题] ' + $Label + ' ' + $target.Rel + '：' + $info.Error) 'Red'
        } elseif ($info.Layers -eq 0) {
            Write-Log ('      [明文] ' + $Label + ' ' + $target.Rel + '：2.0 加载时会自行升级为受保护格式') 'DarkYellow'
        } elseif (-not $info.SawLegacy -and $info.Layers -eq 1) {
            Write-Log ('      [无需处理] ' + $Label + ' ' + $target.Rel + '：已是当前信封') 'DarkGray'
        } else {
            $legacy++
            Write-Log ('      [重封装] ' + $Label + ' ' + $target.Rel + '：' + $info.Layers + ' 层旧信封 → ' + $script:CurrentSecretFormat) 'Yellow'
        }
    }
    return @{ Legacy = $legacy; Problems = $problems }
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
            $exists = Test-Path -LiteralPath $destPath
            # 目标已存在时记下它的大小：预演据此提前分辨"长度一致的跳过"与"上次没复制完
            # 的不一致"，让预演与落盘对同一种状态给出同一个结论。
            $destSize = [int64]-1
            if ($exists -and -not $isDir) {
                try { $destSize = (Get-Item -LiteralPath $destPath).Length } catch { $destSize = [int64]-1 }
            }
            [void]$files.Add(@{
                Rel = ($Name + '\' + $rel)
                Src = $entry.FullName
                Dest = $destPath
                Size = [int64]$entry.Length
                Exists = $exists
                IsDir = $isDir
                DestSize = $destSize
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
    # 目标已存在但长度不同：上一次没复制完留下的残件。落盘时计为失败，不是"跳过"。
    if ($File.DestSize -ge 0 -and $File.DestSize -ne $File.Size) { return '不一致' }
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
    $hasInline = $false
    $eqIndex = $token.IndexOf('=')
    if ($eqIndex -gt 0) {
        $name = $token.Substring(0, $eqIndex)
        $inline = $token.Substring($eqIndex + 1)
        $hasInline = $true
    }
    if ($name.StartsWith('--')) { $name = $name.Substring(2) } else { $name = $name.Substring(1) }
    $key = $name.ToLowerInvariant()

    switch ($key) {
        { $_ -eq 'source' } {
            if ($hasInline) {
                if ($inline -eq '') { Fail '--source= 后面要跟一个目录' }
                $options.Source = $inline
            } else {
                if ($pos + 1 -ge $rawArgs.Count) { Fail '--source 后面要跟一个目录' }
                $pos++
                $next = [string]$rawArgs[$pos]
                if ([string]::IsNullOrWhiteSpace($next) -or $next.StartsWith('-')) {
                    Fail ('--source 后面要跟一个目录，实际给的是：' + $next)
                }
                $options.Source = $next
            }
        }
        { $_ -eq 'dest' } {
            if ($hasInline) {
                if ($inline -eq '') { Fail '--dest= 后面要跟一个目录' }
                $options.Dest = $inline
            } else {
                if ($pos + 1 -ge $rawArgs.Count) { Fail '--dest 后面要跟一个目录' }
                $pos++
                $next = [string]$rawArgs[$pos]
                if ([string]::IsNullOrWhiteSpace($next) -or $next.StartsWith('-')) {
                    Fail ('--dest 后面要跟一个目录，实际给的是：' + $next)
                }
                $options.Dest = $next
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
# 必须有指向 1.4.0 安装目录的正向证据（至少两项特征）。只看"有没有 .config/data"
# 会把无关目录当成旧安装目录，配上 --move 就是把别人的文件搬走再删掉。
if ($move -and $options.Source -eq '') {
    Fail '--move 会删除源文件，必须显式给出 --source（脚本不再自动推断要删的目录）'
}

if ($options.Source -ne '') {
    $source = Resolve-AbsolutePath $options.Source
    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
        Fail ('源目录不存在：' + $source)
    }
    $evidence = Get-LegacyRootEvidence $source
    if (@($evidence.Found).Count -lt 2) {
        Fail ('源目录不像 1.4.0 安装目录：' + $source + '。检测到的特征：' + (Format-LegacyEvidence $evidence) +
              '（需要至少两项：OmniBox.exe、.config/app.json 或 app.yaml、.config/auth_token.txt、data\group-mesh\identity）')
    }
    $sourceEvidenceLabel = Format-LegacyEvidence $evidence
} else {
    $scriptDir = $PSScriptRoot
    if ([string]::IsNullOrEmpty($scriptDir)) { $scriptDir = (Get-Location).Path }
    $parentDir = Split-Path -Parent $scriptDir
    $scriptEvidence = Get-LegacyRootEvidence $scriptDir
    $parentEvidence = Get-LegacyRootEvidence $parentDir
    if (@($scriptEvidence.Found).Count -ge 2) {
        $source = Resolve-AbsolutePath $scriptDir
        $sourceEvidenceLabel = Format-LegacyEvidence $scriptEvidence
    } elseif (@($parentEvidence.Found).Count -ge 2) {
        $source = Resolve-AbsolutePath $parentDir
        $sourceEvidenceLabel = Format-LegacyEvidence $parentEvidence
    } else {
        Fail ('没有 --source，并且脚本所在目录（' + $scriptDir + '，特征：' + (Format-LegacyEvidence $scriptEvidence) +
              '）与它的上一级（特征：' + (Format-LegacyEvidence $parentEvidence) +
              '）都不像 1.4.0 安装目录。请用 --source 指到旧安装目录（含 OmniBox.exe 与 .config 的那一层）')
    }
}

# ── 2. 目标数据根 ───────────────────────────────────────────────────────────
# 判据与 2.0 的 get_user_data_dir() 一致：先 CUBBY_HOME，再 %LOCALAPPDATA%\Cubby
# （缺 LOCALAPPDATA 时退 %APPDATA%\Cubby）。来源要写进输出，免得搬错地方。
$destSourceLabel = ''
if ($options.Dest -ne '') {
    $dest = Resolve-AbsolutePath $options.Dest
    $destSourceLabel = '--dest 参数'
} elseif (-not [string]::IsNullOrWhiteSpace($env:CUBBY_HOME)) {
    $dest = Resolve-AbsolutePath $env:CUBBY_HOME
    $destSourceLabel = 'CUBBY_HOME 环境变量（与 2.0 的判据一致）'
} else {
    $base = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($base)) { $base = $env:APPDATA }
    if ([string]::IsNullOrWhiteSpace($base)) {
        Fail '读不到 CUBBY_HOME / %LOCALAPPDATA% / %APPDATA%，请用 --dest 指定数据根'
    }
    $dest = Resolve-AbsolutePath (Join-Path $base 'Cubby')
    $destSourceLabel = '默认数据根（%LOCALAPPDATA%\Cubby）'
}
$script:DestRoot = $dest

# 旧配置把 directories.data_root 改过时，索引与身份可能不在 data\ 下：只告警，不猜路径。
$legacyDataRoot = Get-LegacyDataRootOverride $source
if (-not (Test-DefaultDataRoot $legacyDataRoot)) {
    Write-Log ('  注意          : 旧配置把 directories.data_root 改成了 "' + $legacyDataRoot +
               '"，索引与身份可能不在默认的 data\ 下；本脚本只搬默认位置，这部分需要手工处理') 'Yellow'
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
Write-Log ('  源目录特征    : ' + $sourceEvidenceLabel) 'Gray'
Write-Log ('  数据根（目标）: ' + $dest) 'Cyan'
Write-Log ('  目标根来源    : ' + $destSourceLabel) 'Gray'
if ($apply) {
    Write-Log '  模式          : 落盘（--apply）' 'Yellow'
} else {
    Write-Log '  模式          : 预演（dry-run，不改任何文件；加 --apply 才落盘）' 'Yellow'
}
if ($moveImpliedApply) { Write-Log '  注意          : --move 隐含 --apply；删源前会整份备份计划内的源文件' 'Yellow' }
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

# Windows PowerShell 5.1 的 .NET 文件 API 在路径总长超过 260 字符时失败：提前告警。
# 落盘时这类文件会计入失败项（源文件保留、不参与 --move 删源），不会静默丢内容。
$longPathFiles = 0
foreach ($item in $plan) {
    foreach ($file in $item.Files) {
        if ($file.Src.Length -ge 240 -or $file.Dest.Length -ge 240) { $longPathFiles++ }
    }
}
if ($longPathFiles -gt 0) {
    Write-Log ('  长路径告警    : ' + $longPathFiles + ' 个文件路径长度 ≥ 240 字符，可能会在 Windows PowerShell 5.1 下复制失败（失败会计入结果，源文件保留）') 'DarkYellow'
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
    $willMismatch = 0
    foreach ($file in $item.Files) {
        $action = Get-FileAction $file $force
        if ($action -eq '跳过') {
            $willSkip++
            $skipBytes += $file.Size
        } elseif ($action -eq '不一致') {
            $willMismatch++
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
    if ($willMismatch -gt 0) {
        Write-Log ('      长度不一致 ' + $willMismatch + ' 个：目标已存在但大小不同（可能是上次没复制完），落盘时计为失败；加 --force 覆盖') 'Red'
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

# ── 8. 身份密钥信封：预演报告 ───────────────────────────────────────────────
# 两处都看并写明：源目录（1.4.0 写的位置，复制后会成为目标里的文件）与目标数据根
# （如果已经存在）。落盘时对目标数据根执行 —— 2.0 读的是目标数据根。
Write-Log ''
Write-Log ('[身份密钥] 源 ' + $source) 'Cyan'
$sourceIdentity = Write-IdentityReport -DataRoot $source -Label '源'
$destIdentity = $null
if (Test-Path -LiteralPath $dest -PathType Container) {
    Write-Log ('[身份密钥] 目标 ' + $dest) 'Cyan'
    $destIdentity = Write-IdentityReport -DataRoot $dest -Label '目标'
}
$legacyCount = $sourceIdentity.Legacy
if ($null -ne $destIdentity) { $legacyCount += $destIdentity.Legacy }
if ($legacyCount -gt 0) {
    Write-Log ('      落盘时处理目标数据根里的同名文件，原文件先备份到 ' + $dest + '\.pre-migrate-backup-<时间戳>\') 'DarkGray'
}

# ── 9. 执行或结束预演 ───────────────────────────────────────────────────────
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
            # 目标已存在不等于已迁移成功：上次中断的复制会留下截断的文件。长度不一致
            # 时按失败计（并阻止 --move 删源），不能当成"跳过"放过。
            $srcBytes = -1
            $dstBytes = -1
            try {
                $srcBytes = (Get-Item -LiteralPath $file.Src).Length
                $dstBytes = (Get-Item -LiteralPath $file.Dest).Length
            } catch {
                $srcBytes = -1
                $dstBytes = -1
            }
            if ($srcBytes -eq $dstBytes -and $srcBytes -ge 0) {
                $item.Skipped++
                $script:SkippedFiles++
                Write-Log ('  跳过（目标已存在，长度一致）：' + $file.Rel) 'DarkYellow'
            } else {
                $item.Failed++
                $script:Failures++
                Write-Log ('  目标已存在但长度不一致：' + $file.Rel + '（源 ' + $srcBytes + ' / 目标 ' + $dstBytes +
                           ' 字节；可能是上次没复制完，加 --force 覆盖或先删掉目标那份）') 'Red'
            }
            continue
        }

        if ($file.Exists) {
            # --force：先把目标那一份备份出来，再覆盖。
            $script:BackupRoot = Get-BackupRoot
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
            # 复制没抛异常不等于落对了：逐文件比较长度，不一致算失败并删掉坏副本，
            # 这样下一次运行才会重试，而不是把截断的文件当成"已迁移"跳过。
            $srcBytes = (Get-Item -LiteralPath $file.Src).Length
            $dstBytes = (Get-Item -LiteralPath $file.Dest).Length
            if ($srcBytes -ne $dstBytes) {
                throw ('复制后长度不一致：源 ' + $srcBytes + ' 字节，目标 ' + $dstBytes + ' 字节')
            }
            $item.Copied++
            $script:CopiedFiles++
            $script:CopiedBytes += $file.Size
            $script:VerifiedFiles++
        } catch {
            $item.Failed++
            $script:Failures++
            if (Test-Path -LiteralPath $file.Dest) {
                Remove-Item -LiteralPath $file.Dest -Force -ErrorAction SilentlyContinue
            }
            Write-Log ('  复制失败：' + $file.Rel + '（' + $_.Exception.Message + '）') 'Red'
        }
    }

    Write-Log ('  完成：复制 ' + $item.Copied + ' 个（长度已校验）、跳过 ' + $item.Skipped + ' 个、失败 ' + $item.Failed + ' 个') 'Gray'
}

# ── 10. 身份密钥信封：对目标数据根重封装 ────────────────────────────────────
# 复制只搬字节、改不了信封，而 2.0 读的正是目标数据根里的那一份，所以这一步在
# 复制之后、以目标为准。重封装失败会阻止 --move 删源：源文件是唯一的原始副本。
if ($apply) {
    Write-Log ''
    Write-Log ('身份密钥信封 [' + $dest + '] ...') 'Cyan'
    $identityTargets = @(Get-IdentityKeyFiles -DataRoot $dest)
    if ($identityTargets.Count -eq 0) {
        Write-Log '  没有 data\group-mesh\identity，跳过（首次安装属正常）' 'DarkGray'
    }
    foreach ($target in $identityTargets) {
        $info = Get-EnvelopeInfo -Path $target.Path
        if ($info.Error -ne '') {
            $script:IdentityFailures++
            Write-Log ('  ' + $target.Rel + '：' + $info.Error) 'Red'
            continue
        }
        if ($info.Layers -eq 0) {
            Write-Log ('  ' + $target.Rel + '：明文，2.0 加载时会自行升级') 'DarkYellow'
            continue
        }
        if (-not $info.SawLegacy -and $info.Layers -eq 1) {
            Write-Log ('  ' + $target.Rel + '：已是当前信封，无需处理') 'DarkGray'
            continue
        }

        if ($script:BackupRoot -eq '') {
            $script:BackupRoot = Get-BackupRoot
        }
        $backupPath = Join-Path $script:BackupRoot $target.Rel
        $backupParent = Split-Path -Parent $backupPath
        try {
            if (-not (Test-Path -LiteralPath $backupParent)) {
                New-Item -ItemType Directory -Path $backupParent -Force -ErrorAction Stop | Out-Null
            }
            Copy-Item -LiteralPath $target.Path -Destination $backupPath -Force -ErrorAction Stop
            $script:BackedUpFiles++
        } catch {
            $script:IdentityFailures++
            Write-Log ('  备份失败，未改写：' + $target.Rel + '（' + $_.Exception.Message + '）') 'Red'
            continue
        }

        try {
            Set-KeyEnvelope -Path $target.Path -Plain ([byte[]]$info.Plain)
        } catch {
            $script:IdentityFailures++
            Write-Log ('  重封装失败：' + $target.Rel + '（' + $_.Exception.Message + '）') 'Red'
            continue
        }

        $problem = Test-KeyEnvelopeReadable -Path $target.Path
        if ($problem -ne '') {
            $script:IdentityFailures++
            Write-Log ('  回读校验失败：' + $target.Rel + '（' + $problem + '）') 'Red'
            continue
        }
        $script:RewrappedKeys++
        Write-Log ('  ' + $target.Rel + '：' + $info.Layers + ' 层旧信封 → ' + $script:CurrentSecretFormat + '（原文件已备份）') 'Green'
    }
    Write-Log ('  完成：重封装 ' + $script:RewrappedKeys + ' 个、失败 ' + $script:IdentityFailures + ' 个') 'Gray'
    if ($script:IdentityFailures -gt 0) {
        $script:Failures++
        Write-Log '  身份有文件没能迁移成功：2.0 启动后仍会显示未初始化，按上面的红字处理后重跑一次' 'Red'
    }
}

# ── 11. --move：删源 ────────────────────────────────────────────────────────
if ($move) {
    Write-Log ''
    Write-Log '清理源目录（--move）' 'Yellow'

    # 全局前置：只有本次运行"零失败、零跳过、身份全部成功"时才删源。任何一个文件没搬
    # 干净（或身份重封装失败）就一个都不删 —— 源目录是唯一还能确认状态的原始副本。
    $moveFailed = 0
    $moveSkipped = 0
    foreach ($item in $plan) {
        $moveFailed += $item.Failed
        $moveSkipped += $item.Skipped
    }
    $moveBlocked = ($script:IdentityFailures -gt 0) -or ($moveFailed -gt 0) -or ($moveSkipped -gt 0)
    if ($moveBlocked) {
        Write-Log ('  本次不删除任何源文件：复制失败 ' + $moveFailed + ' 个、跳过 ' + $moveSkipped + ' 个、身份未迁移成功 ' + $script:IdentityFailures + ' 个') 'Red'
        Write-Log '  源目录完整保留；按上面的红字处理后重跑，确认要删源时再带 --move' 'Yellow'
    }

    foreach ($item in $plan) {
        if ($moveBlocked) { continue }
        if ($script:IdentityFailures -gt 0) {
            # 身份没迁移成功时源目录是唯一的原始副本，一个文件都不删。
            Write-Log ('  保留 ' + $item.Source + '：身份密钥信封有 ' + $script:IdentityFailures + ' 个未迁移成功') 'Red'
            continue
        }
        if ($item.Failed -gt 0) {
            Write-Log ('  保留 ' + $item.Source + '：有 ' + $item.Failed + ' 个文件没有成功复制') 'Yellow'
            continue
        }
        if ($item.Skipped -gt 0) {
            Write-Log ('  保留 ' + $item.Source + '：有 ' + $item.Skipped + ' 个文件因目标已存在被跳过，加 --force 覆盖后再删') 'Yellow'
            continue
        }

        # 删源前先把计划内的源文件整份备份一份；备份失败则一个都不删。
        $sourceBackup = Save-ItemSourceBackup -Item $item
        if ($sourceBackup -eq '') {
            $script:Failures++
            Write-Log ('  保留 ' + $item.Source + '：源备份失败，未删除任何文件') 'Red'
            continue
        }
        Write-Log ('  已整份备份源文件到：' + $sourceBackup) 'DarkGray'

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

# ── 12. 迁移后校验与收尾 ────────────────────────────────────────────────────
Write-Log ''
$appJson = Join-Path (Join-Path $dest '.config') 'app.json'
$appYaml = Join-Path (Join-Path $dest '.config') 'app.yaml'
if ((Test-Path -LiteralPath $appJson -PathType Leaf) -or (Test-Path -LiteralPath $appYaml -PathType Leaf)) {
    Write-Log ('校验：' + (Join-Path $dest '.config') + ' 下有配置文件，2.0 启动后会用这份设置') 'Green'
} else {
    Write-Log ('警告：' + (Join-Path $dest '.config') + ' 下没有 app.json / app.yaml。2.0 会按首次运行处理（设置需要重选）') 'Yellow'
}

# 身份能不能被解析，是"2.0 真的能用这份数据"与"文件都在、插件却报未初始化"的分界。
# 设备文件在重封装时已逐个回读校验，这里只验 principal.json。
$identityPrincipal = Join-Path $dest 'data\group-mesh\identity\principal.json'
if (Test-Path -LiteralPath $identityPrincipal -PathType Leaf) {
    $identityProblem = Test-KeyEnvelopeReadable -Path $identityPrincipal
    if ($identityProblem -ne '') {
        Write-Log ('警告：身份仍不可读（' + $identityProblem + '）：' + $identityPrincipal) 'Red'
        if ($script:IdentityFailures -eq 0) { $script:Failures++ }
    } else {
        $principalName = ''
        $principalInfo = Get-EnvelopeInfo -Path $identityPrincipal
        # 明文（Layers==0）也要能读出 name：2.0 只会把合法身份 JSON 当成可升级的明文。
        if ($principalInfo.Error -eq '' -and ([byte[]]$principalInfo.Plain).Length -gt 0) {
            try {
                $principalRecord = ([System.Text.Encoding]::UTF8.GetString([byte[]]$principalInfo.Plain) | ConvertFrom-Json -ErrorAction Stop)
                $principalName = [string]$principalRecord.name
            } catch {
                $principalName = ''
            }
        }
        Write-Log ('校验：身份可读（主体 ' + $principalName + '）：' + $identityPrincipal) 'Green'
    }
}

Write-Log ''
Write-Log ('=' * 62) 'Cyan'
Write-Log '  结果' 'Cyan'
Write-Log ('  复制      : ' + $script:CopiedFiles + ' 个文件 / ' + (Format-Size $script:CopiedBytes)) 'Cyan'
Write-Log ('  校验      : ' + $script:VerifiedFiles + ' 个文件复制后源/目标长度一致') 'Cyan'
Write-Log ('  跳过      : ' + $script:SkippedFiles + ' 个（目标已有同名文件且长度一致；--force 可覆盖，覆盖前会备份）') 'Cyan'
if ($script:BackedUpFiles -gt 0) {
    Write-Log ('  备份      : ' + $script:BackedUpFiles + ' 个 → ' + $script:BackupRoot) 'Cyan'
}
if ($move -and $script:BackupRoot -ne '') {
    Write-Log ('  源备份    : ' + (Join-Path $script:BackupRoot 'source') + '（--move 删源前的整份副本）') 'Cyan'
}
if ($script:RewrappedKeys -gt 0) {
    Write-Log ('  身份重封装: ' + $script:RewrappedKeys + ' 个文件 → ' + $script:CurrentSecretFormat + '（原信封已备份）') 'Cyan'
}
if ($script:Failures -gt 0) {
    Write-Log ('  失败      : ' + $script:Failures + ' 个（详见上面的红色行与日志）') 'Red'
}
Write-Log ''
Write-Log '  下次启动预期' 'Cyan'
Write-Log ('    数据根：' + $dest) 'Gray'
Write-Log '    1.4.0 不会读取这个位置；请安装 2.0 后从开始菜单或 Cubby.exe 启动，设置、令牌与索引都在数据根里。' 'Gray'
Write-Log '    想换到别的位置（比如便携用法）：把 CUBBY_HOME 设成那个目录，再跑一次这个脚本 —— 它会把 CUBBY_HOME 当成默认目标根。' 'Gray'
Write-Log ''
Write-Log '  卸载与数据' 'Cyan'
Write-Log '    卸载 2.0 默认保留数据根；卸载程序的询问里选“是”才会删除。' 'Gray'
Write-Log ('    媒体库原文件不在数据根里，本次迁移没有移动它们。') 'Gray'
Write-Log ('  日志：' + $script:LogPath) 'DarkGray'
Write-Log ('=' * 62) 'Cyan'

if ($script:Failures -gt 0) { exit 2 }
exit 0
