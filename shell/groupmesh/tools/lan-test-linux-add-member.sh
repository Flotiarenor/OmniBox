#!/usr/bin/env bash
# 让 Linux 侧群主把一台成员设备加入名单，并把新名单回写供 Windows 侧 join。
# 参数经环境变量传入：MEMBER_PRINCIPAL、MEMBER_DEVICE、MEMBER_NAME
set -euo pipefail

export LC_ALL=C.UTF-8 PYTHONIOENCODING=utf-8
PY=/root/OmniBox/venv/bin/python
BASE=/root/gmtest

cd /root/OmniBox

"$PY" -m shell.groupmesh.cli roster add --dir "$BASE/node" \
      --principal "$MEMBER_PRINCIPAL" --dev "$MEMBER_DEVICE" --name "$MEMBER_NAME" >/dev/null

# 新邀请串写入 handoff-roster.txt
"$PY" - "$BASE" > "$BASE/handoff-roster.txt" <<'PYEOF'
import sys
from pathlib import Path

sys.path.insert(0, '/root/OmniBox')
from shell.groupmesh.cli import invite_string, load_roster

roster = load_roster(Path(sys.argv[1]) / 'node')
print('INVITE=' + invite_string(roster))
print('ROSTER_VERSION=' + str(roster.version))
for member in roster.members:
    devices = ','.join(d[:12].hex() for d in member.device_keys) or '(none)'
    print(f'MEMBER={member.name} devices={devices}')
PYEOF

cat "$BASE/handoff-roster.txt"
