#!/usr/bin/env bash
# 跨机联调的 Linux 侧准备脚本（由 Windows 侧经 `ssh omnibox-linux bash -s` 喂进来执行）。
#
# 做三件事：建身份与团体、挂共享项、把邀请串与校验和写入 /root/gmtest/handoff.txt。
#
# 环境变量：
#   ADVERTISE_V6   非空时打印本机全局 IPv6 地址，供 Windows 侧做 IPv6 直连测试
set -euo pipefail

export LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8
PY=/root/OmniBox/venv/bin/python
BASE=/root/gmtest

# 内核在 shell/groupmesh，按包路径运行，因此工作目录固定为仓库根
cd /root/OmniBox

rm -rf "$BASE"
mkdir -p "$BASE/shared" "$BASE/node"

# 700 KB 跨多个分块，用于验证分块传输；另一份小文件用于验证零碎路径
head -c 700000 /dev/urandom > "$BASE/shared/big.bin"
printf 'hello from linux owner\n' > "$BASE/shared/note.txt"

"$PY" -m shell.groupmesh.cli init --dir "$BASE/node" --name flotiarenor --device-name ubuntu-server >/dev/null
"$PY" -m shell.groupmesh.cli create --dir "$BASE/node" --group home-lab >/dev/null

# read=group 让所有成员可读；write 保持仅属主（验证 ACL 的另一半：越权上传必须被拒）
"$PY" -m shell.groupmesh.cli share add --dir "$BASE/node" --share-id pub \
      --path "$BASE/shared" --read group --write owner >/dev/null

# 邀请串与设备/主体公钥由 Python 打印，避免在 bash 里拼 base64
"$PY" - "$BASE" > "$BASE/handoff.txt" <<'PYEOF'
import json
import sys
from pathlib import Path

sys.path.insert(0, '/root/OmniBox')
from shell.groupmesh.cli import invite_string, load_roster
from shell.groupmesh.registry import local_addresses

base = Path(sys.argv[1])
node = base / 'node'
roster = load_roster(node)
principal = json.loads((node / 'principal.json').read_text(encoding='utf-8'))
device_file = sorted((node / 'devices').glob('*.json'))[0]
device = json.loads(device_file.read_text(encoding='utf-8'))

print('INVITE=' + invite_string(roster))
print('GROUP=' + roster.group)
print('ROSTER_VERSION=' + str(roster.version))
print('OWNER_DEVICE=' + device['public_key'])
print('OWNER_PRINCIPAL=' + principal['public_key'])
# 本机检测到的地址（含 IPv6 优先），用于核对注册记录会写入哪些端点
print('LOCAL_ADDRESSES=' + ','.join(local_addresses()))
PYEOF

# 共享文件的校验和与大小（分开一条命令，便于 Windows 侧直接比对）
echo "SHARED_SHA256=$(sha256sum "$BASE/shared/big.bin" | awk '{print $1}')" >> "$BASE/handoff.txt"
echo "SHARED_SIZE=$(stat -c %s "$BASE/shared/big.bin")" >> "$BASE/handoff.txt"

# 全局 IPv6（设计文档的目标地址族）：供 Windows 侧做真实 IPv6 直连
echo "GLOBAL_V6=$(ip -6 addr show scope global | awk '/inet6/ {print $2}' | cut -d/ -f1 | head -1)" >> "$BASE/handoff.txt"

cat "$BASE/handoff.txt"
