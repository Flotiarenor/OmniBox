<#
    lan-test.ps1 —— Windows ↔ Linux 跨机联调（group-mesh MVP）

    在 Windows 侧一条命令跑完整链路：
      1. Linux 建身份/团体/共享项，导出邀请串
      2. Windows 建身份并 join
      3. Linux 群主把 Windows 设备加进名单（签发名单 v2）
      4. Windows 用名单 v2 连接 Linux 共享节点
      5. 下载 700 KB 文件并**比对 sha256**，再验证越权写入被拒

    依赖：~/.ssh/config 里已有 omnibox-linux 别名（见 OmniBox-OneClick.ps1）。
    用法：pwsh -File shell/groupmesh/tools/lan-test.ps1
#>
[CmdletBinding()]
param(
    [string]$SshAlias = 'omnibox-linux',
    [string]$LinuxPython = '/root/OmniBox/venv/bin/python',
    [string]$LinuxRepo = '/root/OmniBox',
    [string]$WinDir = '',
    [string]$Target = '192.168.31.16:19443'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) -Parent
if (-not $WinDir) { $WinDir = Join-Path ([System.IO.Path]::GetTempPath()) 'gmtest-win' }
$Py = Join-Path $RepoRoot 'venv\Scripts\python.exe'

function Write-Step2 { param([string]$Text) Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok2 { param([string]$Text) Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Err3 { param([string]$Text) Write-Host "  [X]  $Text" -ForegroundColor Red }

# 经 stdin 喂 bash 脚本。必须先归一化行尾：CRLF 会让 bash 报 $'\r': command not found。
function Invoke-RemoteBash {
    param([Parameter(Mandatory)][string]$Script, [string]$Prefix = '')
    $lf = ($Script -replace "`r`n", "`n") -replace "`r", "`n"
    $out = $lf | ssh $SshAlias "$Prefix bash -s" 2>&1
    return @($out)
}

function Get-Handoff {
    param([string[]]$Lines)
    $map = @{}
    foreach ($line in $Lines) {
        # 键名可能含数字（SHARED_SHA256），因此字符类要带 0-9
    if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { $map[$Matches[1]] = $Matches[2] }
    }
    return $map
}

# 控制台代码页会让中文输出变成乱码（而且看不出断言是否真的命中）。
# 因此断言一律用**退出码**：cli 失败时 die() 会以非零码退出。
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Invoke-Cli {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    # 必须在 MVP 根目录下跑：groupmesh 是那里的包，靠当前目录进 sys.path
    $all = @('-m', 'shell.groupmesh.cli') + $Args
    Push-Location $RepoRoot
    try {
        $out = & $Py @all 2>&1
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    return [pscustomobject]@{ Output = @($out); Code = $code }
}

# ── 1) Linux 侧准备 ────────────────────────────────────────────────────────
Write-Step2 "1/5 Linux 侧建立团体与共享项"
$prep = Invoke-RemoteBash -Script (Get-Content (Join-Path $PSScriptRoot 'lan-test-linux-prep.sh') -Raw)
$handoff = Get-Handoff -Lines $prep
if (-not $handoff['INVITE']) { $prep | ForEach-Object { Write-Host "  $_" }; throw 'Linux 侧准备失败（没有拿到邀请串）' }
$expectSha = $handoff['SHARED_SHA256']
Write-Ok2 "团体 $($handoff['GROUP'])，名单 v$($handoff['ROSTER_VERSION'])，共享 700000 字节"

# ── 2) Windows 侧建身份并加入 ──────────────────────────────────────────────
Write-Step2 "2/5 Windows 侧建立身份并加入团体"
if (Test-Path $WinDir) { Remove-Item -Recurse -Force $WinDir }
$initRes = Invoke-Cli init --dir $WinDir --name windows-pc --device-name win-node
if ($initRes.Code -ne 0) { $initRes.Output | ForEach-Object { Write-Host "  $_" }; throw 'Windows 侧 init 失败' }
$joinRes = Invoke-Cli join --dir $WinDir --invite $handoff['INVITE']
if ($joinRes.Code -ne 0) { $joinRes.Output | ForEach-Object { Write-Host "  $_" }; throw 'Windows 侧 join 失败' }
Write-Ok2 "已加入团体（名单 v1）"

# ── 3) 群主把 Windows 设备写进名单 ─────────────────────────────────────────
Write-Step2 "3/5 群主把 Windows 设备加入名单"
$winInfo = (& $Py -c @"
import base64, json, sys
sys.path.insert(0, r'$RepoRoot')
from pathlib import Path
from shell.groupmesh.identity import Identity
i = Identity.load(Path(r'$WinDir'))
print(json.dumps({'principal': base64.b64encode(i.principal.public_key).decode(),
                  'device': base64.b64encode(i.device.public_key).decode(),
                  'name': i.principal.name}))
"@) | ConvertFrom-Json

$prefix = "MEMBER_PRINCIPAL='$($winInfo.principal)' MEMBER_DEVICE='$($winInfo.device)' MEMBER_NAME='$($winInfo.name)'"
$addOut = Invoke-RemoteBash -Script (Get-Content (Join-Path $PSScriptRoot 'lan-test-linux-add-member.sh') -Raw) -Prefix $prefix
$newHandoff = Get-Handoff -Lines $addOut
if (-not $newHandoff['INVITE']) { $addOut | ForEach-Object { Write-Host "  $_" }; throw '加入成员失败' }
Write-Ok2 "名单已更新到 v$($newHandoff['ROSTER_VERSION'])"
$addOut | Where-Object { $_ -like 'MEMBER=*' } | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
$rejoin = Invoke-Cli join --dir $WinDir --invite $newHandoff['INVITE']
if ($rejoin.Code -ne 0) { $rejoin.Output | ForEach-Object { Write-Host "  $_" }; throw 'Windows 侧更新名单失败' }
Write-Ok2 "Windows 侧名单已更新到 v$($newHandoff['ROSTER_VERSION'])"

# ── 4) 启动 Linux 共享节点 ────────────────────────────────────────────────
Write-Step2 "4/5 启动 Linux 共享节点（后台）"
# 用 systemd-run 起一个临时 unit，而不是 `nohup ... &`：
# 后台进程即使 nohup/setsid 也会**继承 ssh 的 stdout 管道**，于是 ssh 会一直等到
# 它退出（实测卡满 120 秒超时，而服务其实早已在监听）。systemd-run 由 pid 1 接管，
# 与 ssh 会话彻底解耦，停止也用 systemctl 明确收尾。
$unit = 'gm-mvp-lan-test'
# 注意：不能用 `ssh $SshAlias "a" + "b"` —— ssh 是原生命令，参数会被拆开，
# 结果远端收到的是一个孤立的 "+"。先把整条命令拼成一个变量再传。
$startRemote = "systemctl stop $unit 2>/dev/null; sleep 1; " +
    "systemd-run --unit=$unit --collect --working-directory=$LinuxRepo " +
    "--setenv=LC_ALL=C.UTF-8 --setenv=PYTHONIOENCODING=utf-8 " +
    "$LinuxPython -m shell.groupmesh.cli serve --dir /root/gmtest/node --bind 0.0.0.0 --port 19443"
ssh $SshAlias $startRemote | Out-Null

$listening = 0
foreach ($attempt in 1..15) {
    Start-Sleep -Milliseconds 700
    $countText = (ssh $SshAlias "ss -lnt 2>/dev/null | grep -c ':19443'") -join ''
    [void][int]::TryParse(($countText -replace '[^0-9]', ''), [ref]$listening)
    if ($listening -ge 1) { break }
}
if ($listening -lt 1) {
    ssh $SshAlias "journalctl -u $unit --no-pager -n 30" 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    throw 'Linux 共享节点未能在 19443 上监听'
}
Write-Ok2 "已在 192.168.31.16:19443 监听（临时 unit: $unit）"
ssh $SshAlias "journalctl -u $unit --no-pager -n 6" 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }

# ── 5) 从 Windows 取文件并校验 ───────────────────────────────────────────
Write-Step2 "5/5 从 Windows 连接并取回文件"
$download = Join-Path $WinDir 'downloaded-big.bin'
$peers = Invoke-Cli peers --dir $WinDir --target $Target
if ($peers.Code -ne 0) { $peers.Output | ForEach-Object { Write-Host "  $_" }; throw 'peers 失败' }
Write-Ok2 '已连接 Linux 共享节点，且能看到共享项'
$peers.Output | Where-Object { $_ -like '{*' } | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }

$getRes = Invoke-Cli get --dir $WinDir --target $Target --share-id pub --path big.bin --output $download
if ($getRes.Code -ne 0) { $getRes.Output | ForEach-Object { Write-Host "  $_" }; throw 'get 失败' }

$actualSha = (Get-FileHash -Algorithm SHA256 -Path $download).Hash.ToLower()
Write-Host "  期望 sha256: $expectSha"
Write-Host "  实际 sha256: $actualSha"
if ($actualSha -ne $expectSha) { Write-Err3 '跨机传输内容不一致'; exit 1 }
Write-Ok2 '700 KB 文件跨机（Noise 加密通道）传输校验通过'

# 越权与越界必须被拒：断言用**退出码**，不用输出文本（控制台编码会让中文变乱码）
Write-Step2 "附加验证：越权与越界必须被拒"
$negative = @(
    @{ Name = '无写权限上传'; Result = (Invoke-Cli put --dir $WinDir --target $Target --share-id pub --file $download --remote-path evil.bin) },
    @{ Name = '无删除权限删除'; Result = (Invoke-Cli rm --dir $WinDir --target $Target --share-id pub --path note.txt) },
    @{ Name = '路径越界读取'; Result = (Invoke-Cli get --dir $WinDir --target $Target --share-id pub --path '../../etc/passwd' --output (Join-Path $WinDir 'evil.txt')) }
)
foreach ($check in $negative) {
    if ($check.Result.Code -ne 0) {
        Write-Ok2 "$($check.Name) 被拒绝（退出码 $($check.Result.Code)）"
    } else {
        Write-Err3 "$($check.Name) 竟然成功了 —— 权限或路径校验失效"
        exit 1
    }
}
if (Test-Path (Join-Path $WinDir 'evil.txt')) { Write-Err3 '越界内容竟然落盘了'; exit 1 }

Write-Host "`n跨机联调全部通过。" -ForegroundColor Green
ssh $SshAlias "systemctl stop gm-mvp-lan-test 2>/dev/null; true" | Out-Null
