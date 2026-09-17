<#
    ipv6-test.ps1 —— Windows ↔ Linux 的**纯 IPv6** 跨机联调（group-mesh MVP）

    设计文档 §1.1 的目标就是公网 IPv6，因此这条链路才是主路径；IPv4 只是
    同一网段内的兜底。本脚本验证：

      1. Linux 绑到本机全局 IPv6 并在注册记录里写入 IPv6 端点
      2. Windows 用 [2409:...]:port 形式连接（IPv6 地址含冒号，必须带方括号）
      3. 700 KB 文件经 IPv6 传输，sha256 两侧一致
      4. 越权与越界在 IPv6 上同样被拒
      5. **运行中的节点能看到名单更新**（先起节点、后加成员，不应要求重启）

    依赖：~/.ssh/config 里已有 omnibox-linux 别名。
    用法：pwsh -File shell/groupmesh/tools/ipv6-test.ps1
#>
[CmdletBinding()]
param(
    [string]$SshAlias = 'omnibox-linux',
    [string]$LinuxPython = '/root/OmniBox/venv/bin/python',
    [string]$LinuxRepo = '/root/OmniBox',
    [int]   $Port = 19444
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) -Parent
$WinDir = Join-Path ([System.IO.Path]::GetTempPath()) 'gmtest-v6'
$Py = Join-Path $RepoRoot 'venv\Scripts\python.exe'

$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Write-Step2 { param([string]$Text) Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok2 { param([string]$Text) Write-Host "  [OK] $Text" -ForegroundColor Green }
function Write-Err3 { param([string]$Text) Write-Host "  [X]  $Text" -ForegroundColor Red }

function Invoke-RemoteBash {
    param([Parameter(Mandatory)][string]$Script)
    $lf = ($Script -replace "`r`n", "`n") -replace "`r", "`n"
    return @($lf | ssh $SshAlias 'bash -s' 2>&1)
}

function Get-Handoff {
    param([string[]]$Lines)
    $map = @{}
    foreach ($line in $Lines) {
        if ($line -match '^([A-Z][A-Z0-9_]*)=(.*)$') { $map[$Matches[1]] = $Matches[2] }
    }
    return $map
}

function Invoke-Cli {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
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

# ── 1) Linux 侧准备 ───────────────────────────────────────────────────────
Write-Step2 "1/5 Linux 侧建立团体，并确认它有全局 IPv6"
$handoff = Get-Handoff -Lines (Invoke-RemoteBash -Script (Get-Content (Join-Path $PSScriptRoot 'lan-test-linux-prep.sh') -Raw))
$v6 = $handoff['GLOBAL_V6']
if (-not $v6) { throw 'Linux 侧没有全局 IPv6 地址，无法做 IPv6 联调' }
Write-Ok2 "Linux 全局 IPv6: $v6"
Write-Host "  Linux 检测到的地址: $($handoff['LOCAL_ADDRESSES'])" -ForegroundColor DarkGray

# ── 2) 启动绑在全局 IPv6 上的共享节点（此时名单里还没有 Windows）───────
Write-Step2 "2/5 启动共享节点（绑定 [$v6]:$Port），此时成员尚未加入"
$unit = 'gm-mvp-ipv6-test'
$startRemote = "systemctl stop $unit 2>/dev/null; sleep 1; " +
    "systemd-run --unit=$unit --collect --working-directory=$LinuxRepo " +
    "--setenv=LC_ALL=C.UTF-8 --setenv=PYTHONIOENCODING=utf-8 " +
    "$LinuxPython -m shell.groupmesh.cli serve --dir /root/gmtest/node --bind $v6 --port $Port"
ssh $SshAlias $startRemote | Out-Null

$listening = 0
foreach ($attempt in 1..15) {
    Start-Sleep -Milliseconds 700
    $countText = (ssh $SshAlias "ss -lnt 2>/dev/null | grep -c ':$Port'") -join ''
    [void][int]::TryParse(($countText -replace '[^0-9]', ''), [ref]$listening)
    if ($listening -ge 1) { break }
}
if ($listening -lt 1) {
    ssh $SshAlias "journalctl -u $unit --no-pager -n 30" 2>&1 | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    throw "共享节点未能在 [$v6]:$Port 上监听"
}
Write-Ok2 "已在 [$v6]:$Port 监听（纯 IPv6）"

# ── 3) Windows 侧加入，并在节点运行期间把成员写进名单 ───────────────────
Write-Step2 "3/5 Windows 侧加入；群主在节点运行期间签发名单 v2"
if (Test-Path $WinDir) { Remove-Item -Recurse -Force $WinDir }
$init = Invoke-Cli init --dir $WinDir --name windows-pc --device-name win-node
if ($init.Code -ne 0) { $init.Output | ForEach-Object { Write-Host "  $_" }; throw 'init 失败' }
$join = Invoke-Cli join --dir $WinDir --invite $handoff['INVITE']
if ($join.Code -ne 0) { $join.Output | ForEach-Object { Write-Host "  $_" }; throw 'join 失败' }

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
$addOut = @((Get-Content (Join-Path $PSScriptRoot 'lan-test-linux-add-member.sh') -Raw) -replace "`r`n", "`n" |
    ssh $SshAlias "$prefix bash -s" 2>&1)
$newInvite = (Get-Handoff -Lines $addOut)['INVITE']
if (-not $newInvite) { $addOut | ForEach-Object { Write-Host "  $_" }; throw '加入成员失败' }
$rejoin = Invoke-Cli join --dir $WinDir --invite $newInvite
if ($rejoin.Code -ne 0) { $rejoin.Output | ForEach-Object { Write-Host "  $_" }; throw '更新名单失败' }
Write-Ok2 '名单已签发到 v2（节点仍在运行，未重启）'

# ── 4) Windows 侧经 IPv6 连接并取文件 ────────────────────────────────────
Write-Step2 "4/5 Windows 侧经 IPv6 连接并取回文件"
# IPv6 地址含冒号，--target 必须写成 [addr]:port
$target = "[$v6]:$Port"
Write-Host "  target = $target" -ForegroundColor DarkGray
$peers = Invoke-Cli peers --dir $WinDir --target $target
if ($peers.Code -ne 0) { $peers.Output | ForEach-Object { Write-Host "  $_" }; throw 'IPv6 peers 失败' }
Write-Ok2 '已通过 IPv6 连接（协商 + Noise 握手 + 名单授权；节点未重启即认新成员）'

$download = Join-Path $WinDir 'big-v6.bin'
$get = Invoke-Cli get --dir $WinDir --target $target --share-id pub --path big.bin --output $download
if ($get.Code -ne 0) { $get.Output | ForEach-Object { Write-Host "  $_" }; throw 'IPv6 get 失败' }

$actualSha = (Get-FileHash -Algorithm SHA256 -Path $download).Hash.ToLower()
Write-Host "  期望 sha256: $($handoff['SHARED_SHA256'])"
Write-Host "  实际 sha256: $actualSha"
if ($actualSha -ne $handoff['SHARED_SHA256']) { Write-Err3 'IPv6 传输内容不一致'; exit 1 }
Write-Ok2 '700 KB 文件经纯 IPv6 传输校验通过'

# ── 5) 负向验证与注册记录核对 ────────────────────────────────────────────
Write-Step2 "5/5 越权/越界被拒，且注册记录写入 IPv6 端点"
$negative = @(
    @{ Name = '无写权限上传'; Result = (Invoke-Cli put --dir $WinDir --target $target --share-id pub --file $download --remote-path evil.bin) },
    @{ Name = '路径越界读取'; Result = (Invoke-Cli get --dir $WinDir --target $target --share-id pub --path '../../etc/passwd' --output (Join-Path $WinDir 'evil.txt')) }
)
foreach ($check in $negative) {
    if ($check.Result.Code -ne 0) { Write-Ok2 "$($check.Name) 被拒绝（退出码 $($check.Result.Code)）" }
    else { Write-Err3 "$($check.Name) 竟然成功了"; exit 1 }
}

# 注册记录里应出现刚才绑定的那个全局 IPv6 地址。
# 注意不能拿 "[" 当判据 —— JSON 数组本身就有方括号，那样断言永远为真。
$state = (ssh $SshAlias 'cat /root/gmtest/node/state.json') -join ''
Write-Host "  $state" -ForegroundColor DarkGray
if ($state -notmatch [regex]::Escape($v6)) {
    Write-Err3 "注册记录里没有绑定地址 $v6"
    exit 1
}
Write-Ok2 '注册记录的端点包含绑定用的全局 IPv6'

ssh $SshAlias "systemctl stop $unit 2>/dev/null; true" | Out-Null
Write-Host "`n纯 IPv6 跨机联调全部通过。" -ForegroundColor Green
