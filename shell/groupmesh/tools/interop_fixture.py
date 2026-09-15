"""跨平台确定性检查：给定同一种子，Windows 与 Linux 必须算出完全相同的密钥与签名。

用途：验证 pycryptodome 在两个平台/Python 构建上对 Ed25519 与 X25519 的编码一致。
本项目的 X25519 依赖 `pointQ.x` 这个库内部表示，因此这一检查不能省。

从**仓库根**运行：

    python shell/groupmesh/tools/interop_fixture.py --out fixture.json
    python shell/groupmesh/tools/interop_fixture.py --check fixture.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 本脚本在 shell/groupmesh/tools/ 下，要 import 的是上三层的 shell.groupmesh
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shell.groupmesh import crypto_prims as cp  # noqa: E402 - 必须先注入仓库根

# 固定的种子与消息：两端必须完全一致地复现
SEED_HEX = '00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff'
OTHER_SEED_HEX = 'ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100'
MESSAGE = b'group-mesh cross-platform interop fixture v1'


def build_fixture() -> dict:
    seed = bytes.fromhex(SEED_HEX)
    other = bytes.fromhex(OTHER_SEED_HEX)

    sign_pub = cp.sign_public_from_private(seed)
    signature = cp.sign(seed, MESSAGE)

    dh_priv, dh_pub = cp.dh_keypair_from_sign_seed(seed)
    other_priv, other_pub = cp.dh_keypair_from_sign_seed(other)
    shared_ab = cp.dh(dh_priv, other_pub)
    shared_ba = cp.dh(other_priv, dh_pub)

    native_priv, native_pub = cp.generate_dh_keypair()
    del native_priv, native_pub  # 仅确认生成路径可用，不参与比对

    return {
        'message': MESSAGE.hex(),
        'sign_public': sign_pub.hex(),
        'signature': signature.hex(),
        'dh_private': dh_priv.hex(),
        'dh_public': dh_pub.hex(),
        'other_dh_public': other_pub.hex(),
        'shared_ab': shared_ab.hex(),
        'shared_ba': shared_ba.hex(),
        'blake2s': cp.blake2s(MESSAGE).hex(),
        'hkdf': [x.hex() for x in cp.hkdf(b'c' * 32, b'i' * 32, 2)],
        'aead': cp.aead_encrypt(b'k' * 32, 7, b'plaintext', b'ad').hex(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='跨平台确定性 fixture')
    parser.add_argument('--check', metavar='FILE', help='与给定 fixture 比对')
    parser.add_argument('--out', metavar='FILE', help='写入 fixture 文件（默认打印到 stdout）')
    args = parser.parse_args()

    fixture = build_fixture()

    if args.check:
        expected = json.loads(Path(args.check).read_text(encoding='utf-8'))
        diffs = [k for k in expected if expected[k] != fixture.get(k)]
        if diffs:
            print(f'不一致的字段（{len(diffs)} 个）: {", ".join(sorted(diffs))}')
            for key in sorted(diffs):
                print(f'  {key}:\n    expected {expected[key]}\n    actual   {fixture.get(key)}')
            return 1
        print(f'跨平台一致：{len(expected)} 个字段全部相同')
        return 0

    text = json.dumps(fixture, indent=2, sort_keys=True) + '\n'
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
        print(f'已写入 {args.out}', file=sys.stderr)
    else:
        print(text, end='')
    return 0


if __name__ == '__main__':
    sys.exit(main())
