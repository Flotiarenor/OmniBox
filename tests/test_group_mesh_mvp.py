"""group-mesh 协议内核（tools/group-mesh-mvp）的单元测试。

被测对象是实现 `docs/group-mesh-design.md` 的**验证内核**，不是 Shell 插件：
本文件只依赖标准库与 pycryptodome，不加载 Shell、不需要 GUI。

覆盖重点（都是"错了会静默出事"的地方）：

  * 密码学原语的边界：低阶点、AD/nonce 不匹配、验签失败都要被拒绝；
  * Ed25519 签名与外部标准实现互通（RFC 8032），而不是只有自己认；
  * X25519 密钥必须由身份种子**派生**，Ed25519 私钥不能直接当 DH 标量；
  * Noise_XX 的 token 顺序与 nonce 延续规则（本实现曾在此处取错密钥与 nonce）；
  * 名单验证规则 1–6 与并发收敛（§5.2/§5.3）；
  * 共享项 ACL（§6.2：读 / 写按主体判定，写入可加）；
  * 注册表 seq 单调性（§7）；
  * 真实 TCP 的端到端链路，含越界路径与无权限写入必须被拒。

运行：
    python -m unittest tests.test_group_mesh_mvp -v
"""

import base64
import gc
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import ClassVar, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.groupmesh import PROTO_VERSION, client
from shell.groupmesh import crypto_prims as cp
from shell.groupmesh import noise as nm
from shell.groupmesh.identity import (
    Device,
    Identity,
    Principal,
    encode_binding_payload,
    parse_binding,
    remote_dh_public,
)
from shell.groupmesh.node import Node, PathRejected, _serve_one, resolve_in_share, serve
from shell.groupmesh.records import RecordError
from shell.groupmesh.registry import Registry, new_registration
from shell.groupmesh.roster import Roster, RosterEntry, founding_roster, next_roster, staleness_report
from shell.groupmesh.shares import Acl, Authorizer, Permission, new_share
from shell.groupmesh.transport import (
    Negotiation,
    PeerIdentity,
    RemoteError,
    TransportError,
    authorize_peer,
    connect,
)


class CryptoPrimsTest(unittest.TestCase):
    """原语层：正确路径能用，异常输入必须被拒绝。"""

    def test_dh_agrees_both_directions(self):
        sk1, pk1 = cp.generate_dh_keypair()
        sk2, pk2 = cp.generate_dh_keypair()
        self.assertEqual(cp.dh(sk1, pk2), cp.dh(sk2, pk1))

    def test_dh_rejects_low_order_point(self):
        sk, _ = cp.generate_dh_keypair()
        with self.assertRaises(cp.CryptoError):
            cp.dh(sk, b'\x00' * 32)

    def test_dh_rejects_wrong_length(self):
        sk, _ = cp.generate_dh_keypair()
        with self.assertRaises(cp.CryptoError):
            cp.dh(sk, b'\x01' * 8)

    def test_sign_verify_and_reject_tampering(self):
        sk, pk = cp.generate_sign_keypair()
        signature = cp.sign(sk, b'payload')
        self.assertTrue(cp.verify(pk, b'payload', signature))
        self.assertFalse(cp.verify(pk, b'payloaD', signature))
        self.assertFalse(cp.verify(pk, b'payload', bytes([signature[0] ^ 1]) + signature[1:]))
        other_sk, other_pk = cp.generate_sign_keypair()
        self.assertFalse(cp.verify(other_pk, b'payload', signature))

    def test_verify_never_raises_on_bad_input(self):
        sk, pk = cp.generate_sign_keypair()
        signature = cp.sign(sk, b'x')
        self.assertFalse(cp.verify(b'short', b'x', signature))
        self.assertFalse(cp.verify(pk, b'x', b'short'))

    def test_aead_rejects_wrong_ad_and_nonce(self):
        key = cp.random_bytes(32)
        ciphertext = cp.aead_encrypt(key, 0, b'secret', b'ad')
        self.assertEqual(cp.aead_decrypt(key, 0, ciphertext, b'ad'), b'secret')
        with self.assertRaises(cp.CryptoError):
            cp.aead_decrypt(key, 0, ciphertext, b'other-ad')
        with self.assertRaises(cp.CryptoError):
            cp.aead_decrypt(key, 1, ciphertext, b'ad')

    def test_hkdf_is_deterministic_and_separated(self):
        outputs = cp.hkdf(b'c' * 32, b'i' * 32, 3)
        self.assertEqual([len(o) for o in outputs], [32, 32, 32])
        self.assertEqual(len(set(outputs)), 3)
        self.assertEqual(cp.hkdf(b'c' * 32, b'i' * 32, 3), outputs)
        self.assertNotEqual(cp.hkdf(b'd' * 32, b'i' * 32, 2), cp.hkdf(b'c' * 32, b'i' * 32, 2))

    def test_derived_dh_keypair_is_deterministic_and_commutative(self):
        """设备 DH 密钥由身份种子派生，且两个不同身份的 DH 必须双向一致。

        这条覆盖的是一个真实踩过的坑：把 Ed25519 私钥直接当 X25519 标量用，
        双方算出的共享密钥**不一致但都不报错**，直到 AEAD 才以 MAC 失败暴露。
        """
        seed_a, _ = cp.generate_sign_keypair()
        seed_b, _ = cp.generate_sign_keypair()
        a_priv, a_pub = cp.dh_keypair_from_sign_seed(seed_a)
        b_priv, b_pub = cp.dh_keypair_from_sign_seed(seed_b)

        self.assertEqual((a_priv, a_pub), cp.dh_keypair_from_sign_seed(seed_a))
        self.assertEqual(cp.dh(a_priv, b_pub), cp.dh(b_priv, a_pub))
        self.assertEqual(cp.dh_public_from_private(a_priv), a_pub)
        # 派生结果必须与身份密钥本身不同，否则等于没做用途分离
        self.assertNotEqual(a_priv, seed_a)

    def test_derived_dh_scalar_is_clamped(self):
        seed, _ = cp.generate_sign_keypair()
        private, _ = cp.dh_keypair_from_sign_seed(seed)
        self.assertEqual(private[0] & 0b111, 0, '低 3 位必须清零')
        self.assertEqual(private[31] & 0b10000000, 0, '最高位必须清零')
        self.assertEqual(private[31] & 0b01000000, 0b01000000, '第 254 位必须置一')


class X25519Rfc7748VectorTest(unittest.TestCase):
    """X25519 必须符合 RFC 7748，而不是"只有本项目两端互通"。

    为什么单独一组用例：v0.1 的公钥编码与 DH 解码**两处都错**（一个把
    `pointQ.x` 大端当线格式，另一个把字节按大端解读回来），两者互相抵消，
    于是 `test_dh_agrees_both_directions` 这类"两端一致"的用例全是绿的，
    但与任何外部实现都不互通。只有官方向量能发现这类缺陷，
    因此这里的期望值逐字节抄自 RFC 7748 §6.1 / §5.2，不来自本实现的输出。
    """

    # RFC 7748 §6.1 的两组 X25519(scalar, u) 向量（含结果的全零/高位置位边界）
    VECTOR_1 = (
        'a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4',
        'e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c',
        'c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552',
    )
    VECTOR_2 = (
        '4b66e9d4d1b4673c5ad22691957d6af5c11b6421e0ea01d42ca4169e7918ba0d',
        'e5210f12786811d3f4b7959d0538ae2c31dbe7106fc03c3efc4cd549c715a493',
        '95cbde9476e8907d7aade45cb4b873f88b595a68799fa152e6f8f7647aac7957',
    )

    def test_dh_matches_rfc7748_section_6_1(self):
        for scalar_hex, u_hex, expected in (self.VECTOR_1, self.VECTOR_2):
            with self.subTest(scalar=scalar_hex[:8]):
                self.assertEqual(cp.dh(bytes.fromhex(scalar_hex), bytes.fromhex(u_hex)).hex(),
                                 expected)

    def test_dh_masks_the_high_bit_of_the_u_coordinate(self):
        """RFC 7748 §5：解码公钥时必须屏蔽 `u[31]` 的最高位。

        不屏蔽的实现会与按规范解码的对端算出不同的共享秘密，而两边都不报错
        —— 只会在后面的 AEAD 上以 MAC 失败暴露。这条用例把"我按规范做的"
        与"两端恰好一致"区分开。
        """
        scalar_hex, u_hex, expected = self.VECTOR_1
        raised = bytearray(bytes.fromhex(u_hex))
        raised[31] |= 0x80
        self.assertEqual(cp.dh(bytes.fromhex(scalar_hex), bytes(raised)).hex(), expected)

    def test_public_key_encoding_is_rfc7748(self):
        """公钥字节必须等于 RFC 7748 §5 的编码。

        §5.2 的基点迭代向量用的是**未 clamp** 的标量，而本项目与所有标准
        X25519 实现一样先 clamp（§5 第 5 步），因此这里改用一个等价的、可复核
        的判据：本实现的公钥/私钥字节必须与 pycryptodome 自己的 RFC 7748
        导入导出路径逐字节相同（`import_x25519_public_key` 内部就是
        `from_bytes(..., 'little')`）。
        """
        from Crypto.Protocol.DH import import_x25519_private_key, import_x25519_public_key

        scalar_hex, u_hex, _ = self.VECTOR_1
        scalar = bytes.fromhex(scalar_hex)
        self.assertEqual(cp.dh_public_from_private(scalar),
                         import_x25519_private_key(scalar).public_key().export_key(format='raw'))
        self.assertEqual(cp.dh_public_bytes(import_x25519_public_key(bytes.fromhex(u_hex))),
                         bytes.fromhex(u_hex))

    def test_generated_keypair_round_trips_through_seed(self):
        """生成的私钥字节重新装入必须得到同一把密钥（大端 ↔ 小端的往返）。"""
        private, public = cp.generate_dh_keypair()
        self.assertEqual(cp.dh_public_from_private(private), public)
        self.assertEqual(cp.dh(private, public), cp.dh(private, public))
        # 私钥字节必须与库自己认定的标量小端表示一致
        from Crypto.Protocol.DH import import_x25519_private_key
        self.assertEqual(int(import_x25519_private_key(private).d).to_bytes(32, 'little'),
                         private)

    def test_stale_device_dh_public_does_not_block_loading(self):
        """落盘的 `dh_public` 与派生值不符时，读盘必须照常成功（派生值才是权威）。

        为什么要专门锁这条：这个字段不参与任何密码学计算（握手用派生值），
        但 v0.1 把它写成了"不符即判文件被改动"的硬校验。RFC 7748 修正之后，
        磁盘上所有旧身份文件都不符 —— 于是插件加载正常、节点永远起不来、
        界面停在"正在读取设备"。实测本仓库 `data/group-mesh` 里那份既不等于
        现行编码、也不等于大端遗留，说明拿"大端兼容"打补丁不够，
        正确语义是"警告并采用派生值"。
        """
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = Identity.init(root, 'alice', 'laptop')
            device_path = next((root / 'devices').glob('*.json'))
            payload = json.loads(device_path.read_text(encoding='utf-8'))
            derived = cp.b64d(payload['dh_public'])

            for label, stale in (
                ('大端遗留', derived[::-1]),
                ('来历不明的陈旧值', b'\x5a' * 32),
            ):
                with self.subTest(kind=label):
                    payload['dh_public'] = cp.b64(stale)
                    device_path.write_text(json.dumps(payload), encoding='utf-8')
                    loaded = Identity.load(root)
                    self.assertEqual(loaded.device.id, identity.device.id)
                    self.assertEqual(loaded.device.dh_public, derived,
                                     '必须采用由私钥派生的 DH 公钥')

            # 长度不对仍然是结构性错误：它说明文件被截断或写坏，不能静默放过
            payload['dh_public'] = cp.b64(b'\x11' * 8)
            device_path.write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaises(RecordError):
                Identity.load(root)


class Ed25519InteropTest(unittest.TestCase):
    """签名必须是标准 RFC 8032，外部实现能独立复核。"""

    def test_signatures_verify_with_cryptography_library(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError:  # pragma: no cover - 开发期可选依赖
            self.skipTest('未安装 cryptography，跳过外部互通验证')

        sk, pk = cp.generate_sign_keypair()
        message = b'group-mesh roster v1 canonical bytes'
        signature = cp.sign(sk, message)
        Ed25519PublicKey.from_public_bytes(pk).verify(signature, message)

    def test_seed_is_standard_private_key_representation(self):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        except ImportError:  # pragma: no cover
            self.skipTest('未安装 cryptography，跳过外部互通验证')

        sk, pk = cp.generate_sign_keypair()
        rebuilt = Ed25519PrivateKey.from_private_bytes(sk)
        self.assertEqual(rebuilt.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw), pk)


class NoiseXXTest(unittest.TestCase):
    """Noise_XX 握手：token 顺序、nonce 延续、防篡改、防降级。"""

    @staticmethod
    def _pair(prologue_a=b'p', prologue_b=None):
        ipriv, ipub = cp.generate_dh_keypair()
        rpriv, rpub = cp.generate_dh_keypair()
        initiator = nm.HandshakeState(nm.ROLE_INITIATOR, ipriv, ipub, prologue_a)
        responder = nm.HandshakeState(nm.ROLE_RESPONDER, rpriv, rpub,
                                      prologue_a if prologue_b is None else prologue_b)
        return initiator, responder, ipub, rpub

    def _run(self, prologue_a=b'p', prologue_b=None):
        initiator, responder, ipub, rpub = self._pair(prologue_a, prologue_b)
        responder.read_message(initiator.write_message())
        payload_for_initiator = initiator.read_message(responder.write_message(b'from-responder'))
        payload_for_responder = responder.read_message(initiator.write_message(b'from-initiator'))
        return initiator, responder, ipub, rpub, payload_for_initiator, payload_for_responder

    def test_full_handshake_and_transport(self):
        initiator, responder, ipub, rpub, p2, p3 = self._run()
        self.assertEqual(p2, b'from-responder')
        self.assertEqual(p3, b'from-initiator')
        self.assertEqual(initiator.remote_static_public, rpub)
        self.assertEqual(responder.remote_static_public, ipub)
        self.assertEqual(initiator.handshake_hash, responder.handshake_hash)

        init_send, init_recv = initiator.split()
        resp_send, resp_recv = responder.split()
        self.assertEqual(init_send.key, resp_recv.key)
        self.assertEqual(resp_send.key, init_recv.key)
        self.assertNotEqual(init_send.key, init_recv.key)
        for index in range(3):
            text = f'frame-{index}'.encode()
            self.assertEqual(resp_recv.decrypt(init_send.encrypt(text)), text)
            self.assertEqual(init_recv.decrypt(resp_send.encrypt(text)), text)

    def test_nonce_advances_so_ciphertexts_differ(self):
        initiator, _responder, _ipub, _rpub, _p2, _p3 = self._run()
        send, _ = initiator.split()
        self.assertNotEqual(send.encrypt(b'same'), send.encrypt(b'same'))

    def test_prologue_mismatch_fails_handshake(self):
        with self.assertRaises((nm.NoiseError, cp.CryptoError)):
            self._run(prologue_a=b'prologue-a', prologue_b=b'prologue-b')

    def test_tampered_ciphertext_is_rejected(self):
        initiator, responder, _ipub, _rpub, _p2, _p3 = self._run()
        send, _ = initiator.split()
        _, recv = responder.split()
        tampered = bytearray(send.encrypt(b'secret'))
        tampered[-1] ^= 1
        with self.assertRaises(cp.CryptoError):
            recv.decrypt(bytes(tampered))

    def test_cannot_write_after_completion(self):
        initiator, _responder, _ipub, _rpub, _p2, _p3 = self._run()
        initiator.split()
        with self.assertRaises(nm.NoiseError):
            initiator.write_message()

    def test_negotiation_prologue_is_order_independent(self):
        """两端各自算出的 prologue 必须相同（与角色无关）。"""
        local = Negotiation.local_hello_payload()
        from shell.groupmesh.transport import _json_bytes
        a_bytes, b_bytes = _json_bytes(local), _json_bytes({'proto': 1, 'suites': ['x'],
                                                            'features': []})
        one = Negotiation('s', [], a_bytes, b_bytes).prologue()
        two = Negotiation('s', [], b_bytes, a_bytes).prologue()
        self.assertEqual(one, two)


class NegotiationTest(unittest.TestCase):
    """§4.5 协商字段的边界检查。

    `LOCAL` 的 `proto` 取自 `PROTO_VERSION` 而不是写字面量：这里要验的是"匹配则
    通过、不匹配则拒绝"，把版本号写死会让每次递增版本都改一遍用例，而真正的
    回归点（本机 hello 报的是不是当前版本）由 `NoiseXXTest` 里那条用例守。
    """

    LOCAL: ClassVar[dict] = {'proto': PROTO_VERSION,
                             'suites': ['noise-XX-25519-chacha20poly1305-blake2s'],
                             'features': ['roster-v1', 'share-read']}

    def _resolve(self, remote):
        return Negotiation.resolve(self.LOCAL, remote, b'{}', b'{}')

    def test_accepts_matching_hello(self):
        result = self._resolve({'proto': PROTO_VERSION,
                                'suites': ['noise-XX-25519-chacha20poly1305-blake2s'],
                                'features': ['roster-v1', 'unknown-feature']})
        self.assertEqual(result.suite, 'noise-XX-25519-chacha20poly1305-blake2s')
        self.assertEqual(result.features, ['roster-v1'])

    def test_rejects_proto_mismatch(self):
        with self.assertRaises(TransportError) as ctx:
            self._resolve({'proto': PROTO_VERSION + 1,
                           'suites': ['noise-XX-25519-chacha20poly1305-blake2s']})
        # 报错必须点名两个版本号：v1 与 v2 的 X25519 编码互为字节序反转，
        # 若只说"握手失败"，用户看到的是 MAC 校验失败而不是"版本不同"。
        message = str(ctx.exception)
        self.assertIn(str(PROTO_VERSION), message)
        self.assertIn(str(PROTO_VERSION + 1), message)

    def test_rejects_no_common_suite(self):
        with self.assertRaises(TransportError):
            self._resolve({'proto': PROTO_VERSION, 'suites': ['something-else']})

    def test_rejects_malformed_fields(self):
        for bad in ({'proto': '1', 'suites': ['x']},
                    {'proto': PROTO_VERSION, 'suites': []},
                    {'proto': PROTO_VERSION, 'suites': [1, 2]},
                    {'proto': PROTO_VERSION, 'suites': ['x'], 'features': 'not-a-list'},
                    {'proto': PROTO_VERSION, 'suites': ['x'], 'features': [1]}):
            with self.subTest(bad=bad), self.assertRaises(TransportError):
                self._resolve(bad)

    def test_local_hello_reports_the_current_protocol_version(self):
        """本机 hello 必须报出 `PROTO_VERSION` 本身。

        存在理由：本轮把 X25519 编码改成 RFC 7748 时**忘了**递增版本号，于是新旧
        节点都自报"版本 1"，而真正的失败发生在握手末尾 —— 对端只看到一句
        MAC 校验失败，看不出是版本不同。这条用例让"改了不兼容的线格式却没动版本号"
        这件事至少能被测试提醒一次。
        """
        payload = Negotiation.local_hello_payload()
        self.assertEqual(payload['proto'], PROTO_VERSION)
        self.assertIsInstance(PROTO_VERSION, int)
        # 版本 2 = RFC 7748 编码；回到 1 意味着线格式又变回大端
        self.assertGreaterEqual(PROTO_VERSION, 2)


class RosterRulesTest(unittest.TestCase):
    """§5.2 验证规则 1–6 与 §5.3 并发收敛。"""

    def setUp(self):
        self.owner = Principal.create('owner')
        self.alice = Principal.create('alice')
        self.admin = Principal.create('admin')
        self.roster = founding_roster('g', self.owner, [cp.generate_sign_keypair()[1]],
                                      ttl_seconds=3600)

    def _v2_with_alice(self):
        members = [*list(self.roster.members), RosterEntry('alice', self.alice.public_key, [cp.generate_sign_keypair()[1]])]
        roster = next_roster(self.roster, 'g', self.roster.owner_key, members, ttl_seconds=3600)
        roster.sign(self.owner.private_key)
        return roster, members

    def test_rule6_founding_roster_is_trusted_when_no_local_copy(self):
        self.roster.accepts(None)
        ok, signer = self.roster.verify_signature()
        self.assertTrue(ok)
        self.assertEqual(signer, self.owner.public_key)

    def test_owner_can_add_member(self):
        roster, _members = self._v2_with_alice()
        roster.accepts(self.roster)
        self.assertTrue(roster.contains_principal(self.alice.public_key))

    def test_rule1_rejects_same_version(self):
        roster, _members = self._v2_with_alice()
        stale = next_roster(roster, 'g', roster.owner_key, list(roster.members), ttl_seconds=3600)
        stale.version = roster.version
        stale.sign(self.owner.private_key)
        with self.assertRaises(RecordError):
            stale.accepts(roster)

    def test_rule2_rejects_wrong_prev(self):
        roster, _members = self._v2_with_alice()
        wrong = next_roster(roster, 'g', roster.owner_key, list(roster.members), ttl_seconds=3600)
        wrong.prev = b'\x11' * 32
        wrong.sign(self.owner.private_key)
        with self.assertRaises(RecordError):
            wrong.accepts(roster)

    def test_rule3_rejects_outsider_signature(self):
        roster, _members = self._v2_with_alice()
        outsider = Principal.create('outsider')
        forged = next_roster(roster, 'g', roster.owner_key, list(roster.members), ttl_seconds=3600)
        forged.sign(outsider.private_key)
        with self.assertRaises(RecordError):
            forged.accepts(roster)

    def test_rule5_admin_cannot_change_admin_set(self):
        roster, members = self._v2_with_alice()
        members = [*list(members), RosterEntry('admin', self.admin.public_key, [cp.generate_sign_keypair()[1]])]
        v3 = next_roster(roster, 'g', roster.owner_key, members,
                         admin_keys=[self.admin.public_key], ttl_seconds=3600)
        v3.sign(self.owner.private_key)
        v3.accepts(roster)

        escalate = next_roster(v3, 'g', v3.owner_key,
                               [*list(v3.members), RosterEntry('x', self.alice.public_key, [cp.generate_sign_keypair()[1]])],
                               admin_keys=[self.admin.public_key, self.alice.public_key],
                               ttl_seconds=3600)
        escalate.sign(self.admin.private_key)
        with self.assertRaises(RecordError):
            escalate.accepts(v3)

    def test_rule5_admin_can_add_plain_member(self):
        roster, members = self._v2_with_alice()
        members = [*list(members), RosterEntry('admin', self.admin.public_key, [cp.generate_sign_keypair()[1]])]
        v3 = next_roster(roster, 'g', roster.owner_key, members,
                         admin_keys=[self.admin.public_key], ttl_seconds=3600)
        v3.sign(self.owner.private_key)
        v3.accepts(roster)

        extra = Principal.create('extra')
        legal = next_roster(v3, 'g', v3.owner_key,
                            [*list(v3.members), RosterEntry('extra', extra.public_key, [cp.generate_sign_keypair()[1]])],
                            admin_keys=list(v3.admin_keys), ttl_seconds=3600)
        legal.sign(self.admin.private_key)
        legal.accepts(v3)
        self.assertTrue(legal.contains_principal(extra.public_key))

    def test_concurrent_successors_converge_deterministically(self):
        roster, members = self._v2_with_alice()
        first = next_roster(roster, 'g', roster.owner_key, list(members), ttl_seconds=3600)
        first.sign(self.owner.private_key)
        second = next_roster(roster, 'g', roster.owner_key, list(members), ttl_seconds=7200)
        second.sign(self.owner.private_key)
        self.assertIs(Roster.pick_concurrent(first, second),
                      Roster.pick_concurrent(second, first))

    def test_structural_validation(self):
        list(self.roster.members)
        # 群主必须留在成员列表里
        with self.assertRaises(RecordError):
            next_roster(None, 'g', self.owner.public_key, [], ttl_seconds=60).validate_structure()
        # 同一设备不得出现在两名成员名下
        device = cp.generate_sign_keypair()[1]
        duplicate = next_roster(None, 'g', self.owner.public_key,
                                [RosterEntry('a', self.alice.public_key, [device]),
                                 RosterEntry('b', self.admin.public_key, [device])],
                                ttl_seconds=60)
        with self.assertRaises(RecordError):
            duplicate.validate_structure()

    def test_expiry_is_advisory_only(self):
        """§5.4：过期只提示，不阻断 —— 过期名单照样能被接受。"""
        expired = founding_roster('g', self.owner, [cp.generate_sign_keypair()[1]],
                                  ttl_seconds=-10)
        report = staleness_report(expired, None)
        self.assertTrue(report['expired'])
        expired.accepts(None)


class ShareAclTest(unittest.TestCase):
    """§6.2 / §6.3：授权按主体判定，写入是**可加**的（只能新增）。"""

    def setUp(self):
        self.owner = Principal.create('owner')
        self.alice = Principal.create('alice')
        self.stranger = Principal.create('stranger')
        self.node_priv, self.node_pub = cp.generate_sign_keypair()

    def _share(self, acl):
        return new_share('docs', '/tmp/docs', owner_key=self.owner.public_key,
                         node_key=self.node_pub, node_private_key=self.node_priv, acl=acl)

    def test_write_tier_is_granted_per_principal(self):
        share = self._share(Acl(read='group', write=[self.alice.public_key]))
        alice = Authorizer(requester_key=self.alice.public_key, group_member=True)
        self.assertTrue(alice.allows(share.declaration, Permission.READ))
        self.assertTrue(alice.allows(share.declaration, Permission.WRITE))

    def test_write_owner_only_denies_members(self):
        """write=owner 时成员只能读 —— 可上传档位必须显式授予。"""
        share = self._share(Acl(read='group', write='owner'))
        alice = Authorizer(requester_key=self.alice.public_key, group_member=True)
        self.assertTrue(alice.allows(share.declaration, Permission.READ))
        self.assertFalse(alice.allows(share.declaration, Permission.WRITE))

    def test_owner_has_all_permissions(self):
        share = self._share(Acl(read='group', write=[self.alice.public_key]))
        owner = Authorizer(requester_key=self.owner.public_key, group_member=False)
        for permission in Permission:
            self.assertTrue(owner.allows(share.declaration, permission))

    def test_non_member_cannot_read_group_share(self):
        share = self._share(Acl())
        stranger = Authorizer(requester_key=self.stranger.public_key, group_member=False)
        self.assertFalse(stranger.allows(share.declaration, Permission.READ))
        self.assertFalse(stranger.allows(share.declaration, Permission.WRITE))

    def test_tampered_acl_fails_signature(self):
        """改 ACL 会让签名失效 —— 这是「客户端不能自称权限」的根据。"""
        import copy
        share = self._share(Acl(read='owner', write='owner'))
        tampered = copy.deepcopy(share.declaration)
        tampered.acl.read = 'group'
        self.assertFalse(tampered.verify_signature())

    def test_acl_field_validation(self):
        with self.assertRaises(RecordError):
            Acl.from_dict({'read': 'everyone', 'write': 'owner'})
        with self.assertRaises(RecordError):
            Acl.from_dict({'read': [], 'write': 'owner'})
        with self.assertRaises(RecordError):
            Acl.from_dict({'read': 'group'})

    def test_legacy_delete_field_is_ignored(self):
        """旧 `shares.json` 里的 delete 字段不再参与解析，也不会被写回。"""
        acl = Acl.from_dict({'read': 'group', 'write': 'owner', 'delete': 'group'})
        self.assertEqual(acl.to_dict(), {'read': 'group', 'write': 'owner'})

    def test_share_id_charset_is_enforced(self):
        for bad in ('../evil', 'a/b', '', 'x' * 80):
            with self.subTest(bad=bad), self.assertRaises(RecordError):
                new_share(bad, '/tmp/x', owner_key=self.owner.public_key,
                          node_key=self.node_pub, node_private_key=self.node_priv)


class PathSafetyTest(unittest.TestCase):
    """§6.5：路径校验必须挡住 `..`、绝对路径与符号链接逃逸。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'share'
        self.root.mkdir()
        (self.root / 'ok.txt').write_text('fine', encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def test_normal_relative_path_resolves(self):
        resolved = resolve_in_share(str(self.root), 'ok.txt')
        self.assertEqual(Path(resolved).name, 'ok.txt')

    def test_dot_and_empty_mean_root(self):
        self.assertEqual(resolve_in_share(str(self.root), '.'),
                         str(self.root.resolve()))
        self.assertEqual(resolve_in_share(str(self.root), ''),
                         str(self.root.resolve()))

    def test_parent_traversal_is_rejected(self):
        for bad in ('../secret', 'a/../../secret', '..', 'a/..'):
            with self.subTest(bad=bad), self.assertRaises(PathRejected):
                resolve_in_share(str(self.root), bad)

    def test_absolute_paths_are_rejected(self):
        with self.assertRaises(PathRejected):
            resolve_in_share(str(self.root), '/etc/passwd')
        with self.assertRaises(PathRejected):
            resolve_in_share(str(self.root), 'C:/Windows/win.ini')

    def test_symlink_escape_is_rejected(self):
        outside = Path(self._tmp.name) / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('secret', encoding='utf-8')
        link = self.root / 'escape'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - 需要权限
            self.skipTest('当前环境不允许创建符号链接')
        with self.assertRaises(PathRejected):
            resolve_in_share(str(self.root), 'escape/secret.txt')


class RegistryTest(unittest.TestCase):
    """§7：注册记录由设备自签，seq 必须严格递增。"""

    def setUp(self):
        self.priv, self.pub = cp.generate_sign_keypair()
        self.registry = Registry()

    def test_first_record_accepted_and_seq_must_advance(self):
        self.assertTrue(self.registry.add(
            new_registration(self.pub, self.priv, 1, [('::1', 19443)], ['docs'])))
        self.assertFalse(self.registry.add(
            new_registration(self.pub, self.priv, 1, [('::1', 1)], ['docs'])),
            '同 seq 记录必须被拒绝（防重放）')
        self.assertFalse(self.registry.add(
            new_registration(self.pub, self.priv, 0, [('::1', 1)], ['docs'])),
            'seq 回退必须被拒绝')
        self.assertTrue(self.registry.add(
            new_registration(self.pub, self.priv, 2, [('::1', 19444)], ['docs'])))
        self.assertEqual(self.registry.get(self.pub).endpoints, [('::1', 19444)])

    def test_forged_signature_rejected(self):
        other_priv, _ = cp.generate_sign_keypair()
        forged = new_registration(self.pub, self.priv, 1, [('::1', 1)], [])
        forged.signature = cp.sign(other_priv, b'nothing')
        self.assertFalse(self.registry.add(forged))

    def test_snapshot_round_trip_skips_corrupt_entries(self):
        self.registry.add(new_registration(self.pub, self.priv, 3, [('::1', 9)], ['a']))
        snapshot = self.registry.snapshot_dicts()
        snapshot.append({'device': 'not-base64', 'seq': 1})
        restored = Registry.from_snapshot_dicts(snapshot)
        self.assertEqual(len(restored), 1)

    def test_endpoint_validation(self):
        with self.assertRaises(RecordError):
            new_registration(self.pub, self.priv, 1, [('not-an-ip', 1)], [])
        with self.assertRaises(RecordError):
            new_registration(self.pub, self.priv, 1, [('::1', 70000)], [])
        with self.assertRaises(RecordError):
            new_registration(self.pub, self.priv, 1, [('::1', 19443)] * 20, [])

    def test_mismatched_signing_key_rejected(self):
        _other_priv, other_pub = cp.generate_sign_keypair()
        with self.assertRaises(RecordError):
            new_registration(self.pub, cp.generate_sign_keypair()[0], 1, [('::1', 1)], [])


class DeviceBindingTest(unittest.TestCase):
    """Ed25519 身份公钥 ↔ X25519 握手公钥的绑定证明。"""

    def test_binding_round_trip(self):
        priv, pub = cp.generate_sign_keypair()
        _dh_priv, dh_pub = cp.dh_keypair_from_sign_seed(priv)
        payload = encode_binding_payload(priv, pub, dh_pub)
        self.assertEqual(parse_binding(payload), pub)
        self.assertEqual(remote_dh_public(payload), dh_pub)

    def test_binding_rejects_forged_device_key(self):
        """把别人的设备公钥放进负载、但用自己的 DH 私钥握手 —— 必须被拒。"""
        victim_priv, victim_pub = cp.generate_sign_keypair()
        attacker_priv, _attacker_pub = cp.generate_sign_keypair()
        _victim_dh_priv, victim_dh_pub = cp.dh_keypair_from_sign_seed(victim_priv)
        _atk_dh_priv, atk_dh_pub = cp.dh_keypair_from_sign_seed(attacker_priv)
        # 攻击者用 victim 的设备公钥 + 自己的 dh 公钥 + 自己的签名
        payload = encode_binding_payload(attacker_priv, victim_pub, atk_dh_pub)
        with self.assertRaises(RecordError):
            parse_binding(payload)

    def test_binding_rejects_empty_and_malformed(self):
        for payload in (b'', b'{}', b'not json', b'{"kind":"other"}'):
            with self.subTest(payload=payload), self.assertRaises(RecordError):
                parse_binding(payload)


class AuthorizePeerTest(unittest.TestCase):
    """授权只看名单，不看请求参数。"""

    def setUp(self):
        self.owner = Principal.create('owner')
        self.alice = Principal.create('alice')
        self.device_priv, self.device_pub = cp.generate_sign_keypair()
        self.roster = founding_roster('g', self.owner, [cp.generate_sign_keypair()[1]],
                                      ttl_seconds=3600)
        members = [*list(self.roster.members), RosterEntry('alice', self.alice.public_key, [self.device_pub])]
        self.v2 = next_roster(self.roster, 'g', self.roster.owner_key, members, ttl_seconds=3600)
        self.v2.sign(self.owner.private_key)

    def test_known_device_resolves_to_principal(self):
        peer = authorize_peer(self.v2, self.device_pub)
        self.assertEqual(peer.principal_key, self.alice.public_key)
        self.assertEqual(peer.role, 'member')
        self.assertEqual(peer.name, 'alice')
        self.assertTrue(peer.member)

    def test_unknown_device_is_accepted_but_marked_not_a_member(self):
        """不在名单里的设备**可以完成握手**，但被明确标为"不是成员"。

        为什么不是直接拒绝（v0.2 改）：名单是带外建立信任锚的，群主加人后签发
        新名单，而新成员手里只有旧名单。若握手阶段就把不在名单的设备拒掉，它永远
        收不到那份新名单 —— 这正是实现路径文档 §5.10 记录的"我已被加进名单却
        连不上任何人"。准入改为"设备绑定证明有效"（密码学自足），能做什么由
        `Node.handle()` 按 op 收敛。
        """
        _priv, unknown = cp.generate_sign_keypair()
        peer = authorize_peer(self.v2, unknown)
        self.assertFalse(peer.member)
        self.assertIsNone(peer.role)
        self.assertEqual(peer.principal_key, b'', '未入名单的设备没有主体')

    def test_missing_roster_is_rejected(self):
        with self.assertRaises(TransportError):
            authorize_peer(None, self.device_pub)


class RosterDistributionTest(unittest.TestCase):
    """名单分发（§5.7）：通道不可信、签名是唯一判据、未入名单者只能拉名单。

    这一组用例守的是"自动分发"能成立**而且不把名单公开给陌生人**：
    准入判据是"对端拿得出本团体的历史名单"，采纳判据是 `Roster.accepts()` 的
    规则 1–6。两者都不能省 —— 只验血缘会让伪造名单混进来，只验签名会让任何拿到
    端点的人取走完整成员名单。
    """

    def setUp(self):
        self.owner = Principal.create('owner')
        self.alice = Principal.create('alice')
        self.bob = Principal.create('bob')
        self.alice_device = Device.create('laptop', self.alice).public_key

        self.v1 = founding_roster('g', self.owner, [cp.generate_sign_keypair()[1]],
                                  ttl_seconds=3600)
        members = [*list(self.v1.members),
                   RosterEntry('alice', self.alice.public_key, [self.alice_device])]
        self.v2 = next_roster(self.v1, 'g', self.v1.owner_key, members, ttl_seconds=3600)
        self.v2.sign(self.owner.private_key)
        self.bob_device = cp.generate_sign_keypair()[1]
        members_v3 = [*list(self.v2.members),
                      RosterEntry('bob', self.bob.public_key, [self.bob_device])]
        self.v3 = next_roster(self.v2, 'g', self.v2.owner_key, members_v3, ttl_seconds=3600)
        self.v3.sign(self.owner.private_key)
        # 三份名单到这一步都已签名完毕。**必须如此**：`content_hash` 是现算的，
        # 而 `history_chain` 用它做去重键 —— 先入链、后签名会让同一个对象在链里
        # 以两个不同的哈希各出现一次，血缘判定随之颠倒（实测踩到，表现为
        # "更新的名单被判成更旧那份的祖先"）。

        share_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(share_root, ignore_errors=True))
        self.node = Node(identity=self._identity(), roster=self.v2,
                         shares={'docs': new_share('docs', str(share_root),
                                                   owner_key=self.owner.public_key,
                                                   node_key=self.owner.public_key,
                                                   node_private_key=self.owner.private_key,
                                                   acl=Acl(read='group', write='owner'))})
        self.saved: list = []
        self.node.roster_saver = self.saved.append

    def _identity(self) -> Identity:
        """一个真实身份（Node 需要 identity.principal / device 的形状）。"""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(root, ignore_errors=True))
        return Identity.init(root, self.owner.name, 'owner-pc')

    def _peer(self, *, member: bool, device_key: Optional[bytes] = None,
              principal_key: bytes = b'') -> PeerIdentity:
        return PeerIdentity(device_key=device_key or cp.generate_sign_keypair()[1],
                            principal_key=principal_key, name='alice' if member else '',
                            role='member' if member else None, group='g', member=member)

    # ── 血缘判定 ──────────────────────────────────────────────────────────

    def test_appears_in_chain_follows_the_hash_chain(self):
        """`A.appears_in_chain(B, history=...)` = "A 出现在 B 一侧的链里"。"""
        chain = Roster.history_chain(self.v3, self.v2, self.v1)
        self.assertTrue(self.v1.appears_in_chain(self.v3, history=chain))
        self.assertTrue(self.v2.appears_in_chain(self.v3, history=chain))
        self.assertTrue(self.v3.appears_in_chain(self.v3, history=chain))

    def test_newer_roster_does_not_appear_in_the_older_chain(self):
        """反向不成立：更新的名单不会出现在更旧那份的链里。

        构造要点：查 `v3` 是否出现在 `v2` 一侧的链里时，history 必须**只含
        v2 一侧**（[v2, v1]）。若把 v3 自己放进 history，判定会平凡成立 ——
        这正是本方法存在的理由（见 `appears_in_chain` 的说明）。
        """
        chain_for_v2 = Roster.history_chain(self.v2, self.v1)
        self.assertFalse(self.v3.appears_in_chain(self.v2, history=chain_for_v2))
        # 不带 history 时只看 `other` 本身
        self.assertFalse(self.v3.appears_in_chain(self.v2))
        self.assertTrue(self.v3.appears_in_chain(self.v3))

    def test_chain_search_does_not_depend_on_history_order(self):
        """history 乱序也必须判对。

        为什么要专门锁：调用方（尤其插件侧的历史拼接）不保证严格按版本降序，
        而"遇到更旧的项就 break"这种单遍扫描一旦乱序就会漏判 ——
        漏判的表现是"该发的名单没发"，在现场很难定位。
        """
        forward = Roster.history_chain(self.v3, self.v2, self.v1)
        self.assertTrue(self.v2.appears_in_chain(self.v3, history=forward))
        # 故意把最旧的一份排在最前面
        self.assertTrue(self.v1.appears_in_chain(self.v3, history=[self.v1, self.v3, self.v2]))
        self.assertTrue(self.v2.appears_in_chain(self.v3, history=[self.v1, self.v2, self.v3]))
        # 不带 history 时只看 `other` 本身：更新的那份不会出现在旧者眼里
        self.assertFalse(self.v3.appears_in_chain(self.v2))
        self.assertTrue(self.v3.appears_in_chain(self.v3))

    def test_unrelated_roster_does_not_contain_ours(self):
        """血缘判定的前提是"同一条链"：别的团体里没有我们的名单。

        这条用例的构造很关键：`other` 与 `v1` **只有团体名不同**（成员、设备、
        版本号、有效期全部相同），因此 `content_hash` 的差别**只来自 group 字段**。
        为什么必须这样构造：`content_hash` 只对参与签名的字段求值，它保证的是
        "同一份名单"，不是"同一个团体"。少了 group 这一关，准入判定就会把无关
        团体的名单也算成"见过本团体"。
        （我第一版用例给两份名单用了不同的设备公钥，哈希天然不同，断言虽然过了
        却什么都没验到 —— 判别力必须靠"只差一个字段"来保证。）
        """
        other = founding_roster('other', self.owner, [self.v1.members[0].device_keys[0]],
                                ttl_seconds=self.v1.expires - int(time.time()))
        # 差异只来自 group：成员与设备公钥完全一致
        self.assertEqual(other.members, self.v1.members)
        self.assertEqual(other.version, self.v1.version)
        self.assertNotEqual(other.content_hash, self.v1.content_hash)
        chain = Roster.history_chain(other)
        # 本机（v3）不出现在 `other` 一侧的链里
        self.assertFalse(self.v3.appears_in_chain(other, history=chain))
        # `other` 也不出现在本机（v3）一侧的链里（v3 的链只含 'g' 的三份）
        our_chain = Roster.history_chain(self.v3, self.v2, self.v1)
        self.assertFalse(other.appears_in_chain(self.v3, history=our_chain))

    def test_history_chain_deduplicates_and_sorts(self):
        chain = Roster.history_chain(self.v2, self.v3, self.v2)
        self.assertEqual([r.version for r in chain], [3, 2])

    # ── op=roster 的准入 ──────────────────────────────────────────────────

    def test_member_can_pull_without_proving_history(self):
        response = self.node.handle({'op': 'roster', 'action': 'list'},
                                    self._peer(member=True, principal_key=self.alice.public_key))
        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['version'], self.v2.version)

    def test_pending_device_with_history_can_pull(self):
        """新成员手里只有旧名单：这正是它必须能连进来的原因（§5.7）。"""
        response = self.node.handle(
            {'op': 'roster', 'action': 'list', 'history': [self.v1.to_dict()]},
            self._peer(member=False))
        self.assertEqual(response['status'], 'ok')
        self.assertEqual(response['version'], self.v2.version)

    def test_stranger_without_history_cannot_pull(self):
        """没有本团体任何历史名单的设备拿不到名单（否则成员名单对任何 IP 公开）。"""
        response = self.node.handle({'op': 'roster', 'action': 'list'},
                                    self._peer(member=False))
        self.assertEqual(response['status'], 'error')
        self.assertEqual(response['code'], 'forbidden')

    def test_unrelated_roster_does_not_grant_access(self):
        """拿**别的团体**的名单来换本机名单必须被拒（准入条件是"同一条链"）。

        注意："拿一份本机名单的副本"是**允许**的（它证明对端确实见过这个团体，
        这正是分发的目的），因此这里用的是一个无关团体的名单 —— 而且它只有团体名
        不同，是这份构造里最有判别力的形态。
        """
        other = founding_roster('other', self.owner, [self.v1.members[0].device_keys[0]],
                                ttl_seconds=self.v1.expires - int(time.time()))
        self.assertNotEqual(other.content_hash, self.v1.content_hash)
        response = self.node.handle(
            {'op': 'roster', 'action': 'list', 'history': [other.to_dict()]},
            self._peer(member=False))
        self.assertEqual(response['status'], 'error')
        self.assertEqual(response['code'], 'forbidden')

    # ── op=roster 的采纳 ──────────────────────────────────────────────────

    def test_valid_successor_is_adopted_and_saved(self):
        response = self.node.handle({'op': 'roster', 'action': 'push',
                                     'roster': self.v3.to_dict()},
                                    self._peer(member=False))
        self.assertEqual(response['status'], 'ok', response)
        self.assertEqual(response['previous_version'], 2)
        self.assertEqual(len(self.saved), 1, '采纳后必须落盘，否则重启退回旧名单')
        self.assertEqual(self.saved[0].content_hash, self.v3.content_hash)
        self.assertEqual(self.node.current_roster().version, 3)

    def test_forged_roster_is_rejected(self):
        """没有群主/管理员私钥就签不出被接受的名单（规则 3）。"""
        forged = next_roster(self.v2, 'g', self.v2.owner_key,
                             [*list(self.v2.members),
                              RosterEntry('mallory', self.bob.public_key,
                                          [cp.generate_sign_keypair()[1]])],
                             ttl_seconds=3600)
        forged.sign(self.alice.private_key)      # 拿成员的私钥签
        response = self.node.handle({'op': 'roster', 'action': 'push',
                                     'roster': forged.to_dict()},
                                    self._peer(member=False))
        self.assertEqual(response['status'], 'error')
        self.assertEqual(response['code'], 'rejected')
        self.assertEqual(self.saved, [], '被拒绝的名单绝不能落盘')

    def test_stale_roster_is_rejected(self):
        """重放一份更旧的签名名单不能把本机回滚（规则 1）。"""
        response = self.node.handle({'op': 'roster', 'action': 'push',
                                     'roster': self.v1.to_dict()},
                                    self._peer(member=True, principal_key=self.alice.public_key))
        self.assertEqual(response['status'], 'error')
        self.assertEqual(response['code'], 'rejected')
        self.assertEqual(self.saved, [])

    def test_skipping_a_version_is_rejected(self):
        """跳版（v1 → v3）被规则 2 拒绝：名单链必须逐级前进。"""
        node = Node(identity=self._identity(), roster=self.v1)
        node.roster_saver = self.saved.append
        response = node.handle({'op': 'roster', 'action': 'push',
                                'roster': self.v3.to_dict()},
                               self._peer(member=True, principal_key=self.owner.public_key))
        self.assertEqual(response['status'], 'error')
        self.assertEqual(response['code'], 'rejected')

    # ── 未入名单者的权限收敛 ──────────────────────────────────────────────

    def test_pending_device_can_only_use_the_roster_operation(self):
        """未入名单的设备：只能拉名单，其余 op 一律 not-in-roster。

        这条是"握手不再按名单拒绝"的**配套防线** —— 少了它，任何能完成握手的设备
        都能直接读共享项，等于把团体墙拆掉。
        """
        pending = self._peer(member=False)
        allowed = self.node.handle({'op': 'roster', 'action': 'list',
                                    'roster': self.v2.to_dict()}, pending)
        self.assertEqual(allowed['status'], 'ok')
        for op in ('list', 'hello', 'registry', 'stat'):
            with self.subTest(op=op):
                response = self.node.handle({'op': op, 'share': 'docs'}, pending)
                self.assertEqual(response['status'], 'error')
                self.assertEqual(response['code'], 'not-in-roster')
        # write 走的是另一条分派路径（带 write_state），单独确认同样被拦
        response = self.node.handle({'op': 'write', 'share': 'docs', 'path': 'x',
                                     'data': '', 'part': True, 'offset': 0}, pending)
        self.assertEqual(response['code'], 'not-in-roster')

    def test_pending_device_cannot_read_shares_even_when_acl_is_group(self):
        """未入名单的设备走 ACL 也拿不到东西：`_authorizer` 里 group_member 恒为假。"""
        node = Node(identity=self._identity(), roster=self.v2)
        pending = self._peer(member=False)
        response = node.handle({'op': 'list'}, pending)
        self.assertEqual(response['status'], 'error')
        self.assertEqual(response['code'], 'not-in-roster')


class EndToEndTest(unittest.TestCase):
    """真实 socket：协商 → 握手 → 授权 → 分块取文件 → 越界与越权被拒。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.owner = Identity.init(root / 'owner', 'owner', 'owner-pc')
        cls.member = Identity.init(root / 'member', 'member', 'member-pc')

        roster = founding_roster('e2e', cls.owner.principal, [cls.owner.device.public_key],
                                 ttl_seconds=3600)
        members = [*list(roster.members), RosterEntry('member', cls.member.principal.public_key, [cls.member.device.public_key])]
        cls.roster = next_roster(roster, 'e2e', roster.owner_key, members, ttl_seconds=3600)
        cls.roster.sign(cls.owner.principal.private_key)
        cls.roster.accepts(roster)

        cls.shared = root / 'shared'
        cls.shared.mkdir()
        cls.payload = cp.random_bytes(300 * 1024)  # 跨多个分块
        (cls.shared / 'big.bin').write_bytes(cls.payload)
        (cls.shared / 'note.txt').write_text('hello from owner', encoding='utf-8')
        (cls.shared / 'sub').mkdir()
        (cls.shared / 'sub' / 'nested.txt').write_text('nested', encoding='utf-8')

        share = new_share('pub', str(cls.shared), owner_key=cls.owner.principal.public_key,
                          node_key=cls.owner.device.public_key,
                          node_private_key=cls.owner.device.private_key,
                          acl=Acl(read='group', write='owner'))
        cls.node = Node(identity=cls.owner, roster=cls.roster, shares={'pub': share},
                        registry=Registry())

        cls.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.listener.bind(('127.0.0.1', 0))
        cls.listener.listen(4)
        cls.port = cls.listener.getsockname()[1]
        cls.stop = threading.Event()

        def accept_loop():
            cls.listener.settimeout(0.5)
            while not cls.stop.is_set():
                try:
                    sock, address = cls.listener.accept()
                except (socket.timeout, OSError):
                    continue
                _serve_one(sock, address, cls.node)

        cls.thread = threading.Thread(target=accept_loop, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.listener.close()
        cls.thread.join(timeout=3)
        cls._tmp.cleanup()

    def _connect(self):
        return connect('127.0.0.1', self.port, self.member, self.roster, timeout=10.0)

    def test_peer_identity_is_resolved_from_roster(self):
        with self._connect() as connection:
            self.assertEqual(connection.peer.device_key, self.owner.device.public_key)
            self.assertEqual(connection.peer.principal_key, self.owner.principal.public_key)
            self.assertEqual(connection.peer.role, 'owner')
            self.assertEqual(connection.negotiation.suite,
                             'noise-XX-25519-chacha20poly1305-blake2s')

    def test_list_and_download_chunked_file(self):
        with self._connect() as connection:
            shares = client.list_shares(connection)
            self.assertEqual([s['share_id'] for s in shares], ['pub'])
            fetched = client.fetch_bytes(connection, 'pub', 'big.bin', chunk_bytes=64 * 1024)
            self.assertEqual(fetched, self.payload)
            self.assertEqual(client.fetch_bytes(connection, 'pub', 'note.txt'),
                             b'hello from owner')

    def test_directory_listing(self):
        with self._connect() as connection:
            listing = client.list_directory(connection, 'pub', '.')
            names = sorted(e['name'] for e in listing['entries'])
            self.assertEqual(names, ['big.bin', 'note.txt', 'sub'])
            nested = client.list_directory(connection, 'pub', 'sub')
            self.assertEqual([e['name'] for e in nested['entries']], ['nested.txt'])

    def test_listing_and_stat_carry_mtime_ns(self):
        """目录项与 stat 都要带对端真实的 mtime，且必须是纳秒整数。

        物化目录的变更判定（"对端换了没有"）与消费方插件的缓存失效都吃这个字段；
        缺了它只能比大小，同大小的替换永远发现不了。用 int 而不是 float 秒：
        float 在 2025 年的分辨率只有约 0.24 µs，且是 `st_mtime_ns / 1e9` 的有损视图。
        """
        expected = (self.shared / 'note.txt').stat().st_mtime_ns
        with self._connect() as connection:
            listing = client.list_directory(connection, 'pub', '.')
            entry = next(e for e in listing['entries'] if e['name'] == 'note.txt')
            stat_result = client.stat_share(connection, 'pub', 'note.txt')
        self.assertIsInstance(entry['mtime_ns'], int)
        self.assertEqual(entry['mtime_ns'], expected)
        self.assertEqual(stat_result['mtime_ns'], expected)

    def test_path_traversal_is_forbidden(self):
        with self._connect() as connection:
            with self.assertRaises(RemoteError) as ctx:
                client.fetch_bytes(connection, 'pub', '../../etc/passwd')
            self.assertEqual(ctx.exception.code, 'forbidden')

    def test_write_without_permission_is_forbidden(self):
        with self._connect() as connection:
            with self.assertRaises(RemoteError) as ctx:
                client.push_bytes(connection, 'pub', 'hack.txt', b'nope')
            self.assertEqual(ctx.exception.code, 'forbidden')

    def test_unknown_share_is_not_found(self):
        with self._connect() as connection:
            with self.assertRaises(RemoteError) as ctx:
                client.list_directory(connection, 'nosuch', '.')
            self.assertEqual(ctx.exception.code, 'not_found')

    def test_registry_push_is_accepted(self):
        with self._connect() as connection:
            registry = Registry()
            registry.add(self.node.make_registration([('127.0.0.1', self.port)], 1))
            result = client.push_registry(connection, registry)
            self.assertEqual(result.get('accepted'), 1)

    def test_unknown_device_connects_but_can_only_pull_the_roster(self):
        """不在名单里的设备**能完成握手**，但只能拉名单，别的 op 一律被拒。

        这条用例在 v0.2 改过语义（原先是"握手即被拒"）。为什么要改：名单是带外
        建立信任锚的，群主加人后签发新名单，而新成员手里只有旧名单 —— 握手阶段就
        拒绝它，它永远收不到那份新名单（实现路径文档 §5.10 的真实故障就是
        "我已被加进名单却连不上任何人"）。准入改为在 op 层收敛，因此这里断言的是
        "连得上、但除 roster 之外都被拒"。
        """
        outsider_root = Path(self._tmp.name) / 'outsider'
        outsider = Identity.init(outsider_root, 'outsider', 'outsider-pc')
        with connect('127.0.0.1', self.port, outsider, self.roster, timeout=10.0) as connection:
            # 陌生设备手里没有本团体的任何名单：连名单都不给（否则成员名单
            # 对所有能连上的人公开）。注意 `local=None` —— 一旦递上名单，
            # 那恰恰是在证明"我见过这个团体"，就不该再期待被拒。
            with self.assertRaises(RemoteError) as ctx:
                client.fetch_roster(connection, None)
            self.assertEqual(ctx.exception.code, 'forbidden')
            for op in ('list', 'hello'):
                with self.subTest(op=op):
                    with self.assertRaises(RemoteError) as sub:
                        connection.request({'op': op})
                    self.assertEqual(sub.exception.code, 'not-in-roster')

    def test_pending_member_with_old_roster_can_pull_the_new_one(self):
        """群主加了人、成员手里还是旧名单时，它连得上并能把新名单拉走。

        这是自动分发要解决的**那个**场景，因此必须有真实 TCP 的用例锁住：
        先起节点（v1），成员拿着 v1 来拉，拿到 v2。名单在握手之后才重读，
        因此这里也能顺带验证"运行中的节点认新名单"。
        """
        outsider_root = Path(self._tmp.name) / 'pending'
        pending = Identity.init(outsider_root, 'pending', 'pending-pc')
        # 把 pending 加进 v2（本机节点的名单），对端只持有 v1
        members = [*list(self.roster.members),
                   RosterEntry('pending', pending.principal.public_key,
                               [pending.device.public_key])]
        v2 = next_roster(self.roster, 'e2e', self.roster.owner_key, members, ttl_seconds=3600)
        v2.sign(self.owner.principal.private_key)
        self.node.roster = v2
        try:
            # 对端只有 v1（就是 self.roster），不带 roster 字段时它得把 v1 当历史递上
            with connect('127.0.0.1', self.port, pending, self.roster, timeout=10.0) as connection:
                fetched = client.fetch_roster(connection, self.roster, history=[self.roster])
                self.assertIsNotNone(fetched)
                assert fetched is not None
                self.assertEqual(fetched.version, v2.version)
                # 客户端侧按规则 1–6 验证并采纳（传输层不替调用方验签）
                fetched.accepts(self.roster)
        finally:
            self.node.roster = self.roster


class UploadTest(unittest.TestCase):
    """§6.4 写入语义：暂存 + 提交、覆盖策略、配额、断线只留 `.part`。

    这一组是"共享面能不能投放"的核心：上传走 `<目标>.part` 分块写、最后一块 `eof`
    时原子改名。直接用 `push_bytes(part=False)` 覆写目标文件的话，一次断线就等于
    毁掉对端已有的那份内容。
    """

    CAP = 4 * 1024 * 1024
    TINY_CAP = 16 * 1024

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.owner = Identity.init(root / 'owner', 'owner', 'owner-pc')
        cls.member = Identity.init(root / 'member', 'member', 'member-pc')

        roster = founding_roster('upload', cls.owner.principal,
                                 [cls.owner.device.public_key], ttl_seconds=3600)
        members = [*list(roster.members),
                   RosterEntry('member', cls.member.principal.public_key,
                               [cls.member.device.public_key])]
        cls.roster = next_roster(roster, 'upload', roster.owner_key, members,
                                 ttl_seconds=3600)
        cls.roster.sign(cls.owner.principal.private_key)
        cls.roster.accepts(roster)

        cls.shared = root / 'dropbox'
        cls.shared.mkdir()
        (cls.shared / 'keep.txt').write_text('keep me', encoding='utf-8')
        # 配额用例单独一个共享项与目录：共用目录会让"前面用例上传的文件"改变基线，
        # 配额判定随之飘（实测就是这么红的）。
        cls.tiny = root / 'tiny'
        cls.tiny.mkdir()

        def _share(share_id: str, path: Path, acl: Acl, cap: 'int | None') -> tuple:
            return (share_id, new_share(share_id, str(path),
                                        owner_key=cls.owner.principal.public_key,
                                        node_key=cls.owner.device.public_key,
                                        node_private_key=cls.owner.device.private_key,
                                        acl=acl, max_bytes=cap))

        cls.node = Node(
            identity=cls.owner, roster=cls.roster,
            shares=dict([
                _share('drop', cls.shared, Acl(read='group', write='group'), cls.CAP),
                _share('tiny', cls.tiny, Acl(read='group', write='group'), cls.TINY_CAP),
                _share('ro', cls.shared, Acl(read='group', write='owner'), None),
            ]),
            registry=Registry())

        cls.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.listener.bind(('127.0.0.1', 0))
        cls.listener.listen(4)
        cls.port = cls.listener.getsockname()[1]
        cls.stop = threading.Event()

        def accept_loop():
            cls.listener.settimeout(0.5)
            while not cls.stop.is_set():
                try:
                    sock, address = cls.listener.accept()
                except (socket.timeout, OSError):
                    continue
                _serve_one(sock, address, cls.node)

        cls.thread = threading.Thread(target=accept_loop, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.listener.close()
        cls.thread.join(timeout=3)
        cls._tmp.cleanup()

    def _connect(self):
        return connect('127.0.0.1', self.port, self.member, self.roster, timeout=10.0)

    def _local(self, name: str, payload: bytes) -> Path:
        path = Path(self._tmp.name) / name
        path.write_bytes(payload)
        return path

    def test_upload_commits_and_leaves_no_part_file(self):
        payload = cp.random_bytes(40 * 1024)
        source = self._local('upload-ok.bin', payload)
        with self._connect() as connection:
            written = client.push_file(connection, 'drop', source, 'new.bin',
                                       chunk_bytes=8 * 1024)
        self.assertEqual(written, len(payload))
        self.assertEqual((self.shared / 'new.bin').read_bytes(), payload)
        self.assertFalse((self.shared / 'new.bin.part').exists(),
                         '提交之后不该留下暂存文件')

    def test_upload_creates_subdirectories(self):
        source = self._local('deep.bin', b'x' * 10)
        with self._connect() as connection:
            client.push_file(connection, 'drop', source, 'sub/dir/deep.bin')
        self.assertEqual((self.shared / 'sub' / 'dir' / 'deep.bin').read_bytes(), b'x' * 10)

    def test_empty_file_is_committed(self):
        source = self._local('empty.bin', b'')
        with self._connect() as connection:
            written = client.push_file(connection, 'drop', source, 'empty.bin')
        self.assertEqual(written, 0)
        self.assertEqual((self.shared / 'empty.bin').stat().st_size, 0)

    def test_upload_refuses_to_clobber_without_overwrite(self):
        source = self._local('clobber.bin', b'other content')
        before = (self.shared / 'keep.txt').read_bytes()
        with self._connect() as connection:
            with self.assertRaises(RemoteError) as ctx:
                client.push_file(connection, 'drop', source, 'keep.txt')
            self.assertEqual(ctx.exception.code, 'exists')
        self.assertEqual((self.shared / 'keep.txt').read_bytes(), before,
                         '被拒绝的上传不得改动已有文件')
        self.assertFalse((self.shared / 'keep.txt.part').exists())

    def test_upload_with_overwrite_replaces_content(self):
        source = self._local('replace.bin', b'replaced')
        with self._connect() as connection:
            client.push_file(connection, 'drop', source, 'keep.txt', overwrite=True)
        self.assertEqual((self.shared / 'keep.txt').read_bytes(), b'replaced')

    def test_interrupted_upload_leaves_target_intact(self):
        """模拟"传到一半断线"：目标文件必须原封不动，只多一个 `.part`。

        这是 `part` 暂存的全部意义 —— 直接覆写目标文件的话，这条用例会看到
        keep.txt 已经被截断成半份。
        """
        self.addCleanup(lambda: (self.shared / 'keep.txt.part').unlink(missing_ok=True))
        before = (self.shared / 'keep.txt').read_bytes()
        with self._connect() as connection:
            # 显式走暂存路径，但**不**发 eof：等价于客户端中途掉线
            connection.request({'op': 'write', 'share': 'drop', 'path': 'keep.txt',
                                'data': base64.b64encode(b'a' * 4096).decode('ascii'),
                                'part': True, 'offset': 0, 'overwrite': True, 'eof': False})
        self.assertEqual((self.shared / 'keep.txt').read_bytes(), before)
        self.assertTrue((self.shared / 'keep.txt.part').exists(),
                        '未提交的上传应当只留下 .part')

    def test_stale_part_does_not_block_a_retry(self):
        """断线留下的 `.part` 不阻塞重传：重传从第一个分块开始覆盖它。"""
        payload = b'retry payload'
        source = self._local('retry.bin', payload)
        (self.shared / 'retry.bin.part').write_bytes(b'stale partial data')
        with self._connect() as connection:
            client.push_file(connection, 'drop', source, 'retry.bin')
        self.assertEqual((self.shared / 'retry.bin').read_bytes(), payload)
        self.assertFalse((self.shared / 'retry.bin.part').exists())

    def test_quota_rejects_before_writing_anything(self):
        """第一个分块就超上限：必须在写盘**之前**拒绝，不留 `.part`。"""
        payload = cp.random_bytes(20 * 1024)
        source = self._local('tiny-over.bin', payload)
        with self._connect() as connection:
            with self.assertRaises(RemoteError) as ctx:
                client.push_file(connection, 'tiny', source, 'over.bin',
                                 chunk_bytes=len(payload))
            self.assertEqual(ctx.exception.code, 'quota_exceeded')
        self.assertFalse((self.tiny / 'over.bin').exists())
        self.assertFalse((self.tiny / 'over.bin.part').exists(),
                         '被配额拒绝的上传不该留下暂存文件')

    def test_quota_accounting_frees_the_stale_part(self):
        """重传时基线要扣掉被覆盖的 `.part`，否则"上次传了一半"会把这次挡在配额之外。

        12 KiB 陈旧 `.part` + 8 KiB 新文件，上限 16 KiB：不扣的话 12 + 8 = 20 > 16，
        就会被误拒 —— 而那份陈旧数据正是这次上传要丢掉的。
        """
        (self.tiny / 'again.bin.part').write_bytes(b'p' * (12 * 1024))
        payload = b'q' * (8 * 1024)
        source = self._local('again.bin', payload)
        with self._connect() as connection:
            client.push_file(connection, 'tiny', source, 'again.bin',
                             chunk_bytes=len(payload))
        self.assertEqual((self.tiny / 'again.bin').read_bytes(), payload)
        self.assertFalse((self.tiny / 'again.bin.part').exists())

    def test_write_without_permission_is_forbidden(self):
        source = self._local('nope.bin', b'nope')
        with self._connect() as connection:
            with self.assertRaises(RemoteError) as ctx:
                client.push_file(connection, 'ro', source, 'nope.bin')
            self.assertEqual(ctx.exception.code, 'forbidden')

    def test_direct_write_still_works(self):
        """`part=False` 的直写路径保留：小文件与旧客户端走它。"""
        with self._connect() as connection:
            client.push_bytes(connection, 'drop', 'direct.bin', b'direct')
        self.assertEqual((self.shared / 'direct.bin').read_bytes(), b'direct')

    # ── 续传与取消 ─────────────────────────────────────────────────────────

    def test_probe_reports_staged_bytes(self):
        """续传前先问对端收了多少：没有暂存文件时是 0，有则报真实大小。"""
        with self._connect() as connection:
            probe = client.probe_upload(connection, 'drop', 'resume.bin')
            self.assertEqual(probe['staged'], 0)
            self.assertFalse(probe['exists'])

            (self.shared / 'resume.bin.part').write_bytes(b'x' * 1234)
            probe = client.probe_upload(connection, 'drop', 'resume.bin')
            self.assertEqual(probe['staged'], 1234)
        (self.shared / 'resume.bin.part').unlink()

    def test_probe_needs_write_not_read(self):
        """只有写权限的共享项也要能续传：probe 走 write 权限。"""
        with self._connect() as connection:
            with self.assertRaises(RemoteError) as ctx:
                client.probe_upload(connection, 'ro', 'x.bin')
            self.assertEqual(ctx.exception.code, 'forbidden')

    def test_resume_continues_from_staged_offset(self):
        """断点续传：对端已有前 4096 字节，客户端只补后半段，最终内容完整。"""
        payload = cp.random_bytes(10 * 1024)
        source = self._local('resume-full.bin', payload)
        (self.shared / 'resume-full.bin.part').write_bytes(payload[:4096])
        with self._connect() as connection:
            total = client.push_file(connection, 'drop', source, 'resume-full.bin',
                                     offset=4096, chunk_bytes=2048)
        self.assertEqual(total, len(payload))
        self.assertEqual((self.shared / 'resume-full.bin').read_bytes(), payload)
        self.assertFalse((self.shared / 'resume-full.bin.part').exists())

    def test_offset_mismatch_is_rejected(self):
        """游标与对端暂存大小不符时拒绝 —— 两个上传方抢同一个目标不会拼出垃圾。"""
        source = self._local('mismatch.bin', b'm' * 100)
        (self.shared / 'mismatch.bin.part').write_bytes(b'x' * 50)
        with self._connect() as connection:
            with self.assertRaises(RemoteError) as ctx:
                client.push_file(connection, 'drop', source, 'mismatch.bin', offset=10)
            self.assertEqual(ctx.exception.code, 'offset_mismatch')
        self.assertEqual((self.shared / 'mismatch.bin.part').read_bytes(), b'x' * 50,
                         '被拒绝的续传不得改动暂存文件')
        (self.shared / 'mismatch.bin.part').unlink()

    def test_cancel_stops_at_a_chunk_boundary_and_keeps_staged_bytes(self):
        """取消：抛 UploadCancelled、保留已传的 `.part`，之后能接着传完。"""
        payload = cp.random_bytes(64 * 1024)
        source = self._local('cancelled.bin', payload)
        chunk = 8 * 1024
        seen = {'sent': 0}

        def progress(sent: int, _total: int) -> None:
            seen['sent'] = sent

        def cancelled() -> bool:
            # 传满 3 个分块就取消（检查发生在分块边界上）
            return seen['sent'] >= 3 * chunk

        with self._connect() as connection:
            with self.assertRaises(client.UploadCancelled):
                client.push_file(connection, 'drop', source, 'cancelled.bin',
                                 chunk_bytes=chunk, progress=progress,
                                 cancelled=cancelled)
            staged = client.probe_upload(connection, 'drop', 'cancelled.bin')['staged']
            self.assertEqual(staged, 3 * chunk, '取消后应当保留已传的分块')
            # 接着传：offset 取对端报的值，最终内容必须完整
            total = client.push_file(connection, 'drop', source, 'cancelled.bin',
                                     offset=staged, chunk_bytes=chunk)
        self.assertEqual(total, len(payload))
        self.assertEqual((self.shared / 'cancelled.bin').read_bytes(), payload)


class RosterRefreshTest(unittest.TestCase):
    """长驻节点必须在每次建连时重读名单。

    覆盖一个真实缺陷：`Node` 若只在构造时持有一份名单快照，那么"先起服务、
    再由群主 `roster add`"这种顺序下，新成员会被判为不在名单里而拒绝，
    必须重启节点才能生效。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.owner = Identity.init(root / 'owner', 'owner', 'owner-pc')
        self.member = Identity.init(root / 'member', 'member', 'member-pc')

        roster = founding_roster('refresh', self.owner.principal,
                                 [self.owner.device.public_key], ttl_seconds=3600)
        self.v1 = roster
        members = [*roster.members,
                   RosterEntry('member', self.member.principal.public_key,
                               [self.member.device.public_key])]
        self.v2 = next_roster(roster, 'refresh', roster.owner_key, members, ttl_seconds=3600)
        self.v2.sign(self.owner.principal.private_key)
        self.v2.accepts(roster)

    def tearDown(self):
        self._tmp.cleanup()

    def test_loader_replaces_snapshot(self):
        node = Node(identity=self.owner, roster=self.v1)
        # 构造时用的是 v1：成员还不在名单里
        self.assertFalse(node.current_roster().contains_device(self.member.device.public_key))

    def test_loader_supplies_updated_roster(self):
        node = Node(identity=self.owner, roster=self.v1, roster_loader=lambda: self.v2)
        self.assertTrue(node.current_roster().contains_device(self.member.device.public_key))

    def test_loader_failure_falls_back_to_snapshot(self):
        def boom():
            raise OSError('模拟读盘失败')

        node = Node(identity=self.owner, roster=self.v1, roster_loader=boom)
        # 不能因为一次 IO 错误就让全部连接被拒
        self.assertIs(node.current_roster(), self.v1)

    def test_loader_returning_none_keeps_snapshot(self):
        node = Node(identity=self.owner, roster=self.v1, roster_loader=lambda: None)
        self.assertIs(node.current_roster(), self.v1)

    def test_running_node_accepts_newly_added_member_without_restart(self):
        """端到端：服务端先启动，之后名单更新，成员连接必须成功。"""
        shared = Path(self._tmp.name) / 'shared'
        shared.mkdir()
        (shared / 'note.txt').write_text('hi', encoding='utf-8')
        share = new_share('pub', str(shared), owner_key=self.owner.principal.public_key,
                          node_key=self.owner.device.public_key,
                          node_private_key=self.owner.device.private_key)
        node = Node(identity=self.owner, roster=self.v1, shares={'pub': share},
                    roster_loader=lambda: self.v2)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        stop = threading.Event()

        def accept_loop():
            listener.settimeout(0.5)
            while not stop.is_set():
                try:
                    sock, address = listener.accept()
                except (socket.timeout, OSError):
                    continue
                _serve_one(sock, address, node)

        thread = threading.Thread(target=accept_loop, daemon=True)
        thread.start()
        try:
            # 客户端也要用 v2，否则它自己就会先拒绝服务端（v1 里没有自己）
            with connect('127.0.0.1', port, self.member, self.v2, timeout=10.0) as connection:
                self.assertEqual(connection.peer.principal_key, self.owner.principal.public_key)
                self.assertEqual(client.fetch_bytes(connection, 'pub', 'note.txt'), b'hi')
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=3)


class TargetParsingTest(unittest.TestCase):
    """`--target` 必须同时接受 host:port 与 [IPv6]:port（测的是 CLI 里的真函数）。"""

    def _parse(self, target):
        from shell.groupmesh.cli import parse_target
        return parse_target(target)

    def test_ipv4_form(self):
        self.assertEqual(self._parse('192.168.31.16:19443'), ('192.168.31.16', 19443))

    def test_hostname_form(self):
        self.assertEqual(self._parse('omnibox.local:19443'), ('omnibox.local', 19443))

    def test_ipv6_bracketed_form(self):
        host, port = self._parse('[2409:8a60:2abf:f5c0:94b8:7fff:fe0a:e9f0]:19444')
        self.assertEqual(host, '2409:8a60:2abf:f5c0:94b8:7fff:fe0a:e9f0')
        self.assertEqual(port, 19444)

    def test_ipv6_loopback(self):
        self.assertEqual(self._parse('[::1]:19443'), ('::1', 19443))

    def test_ipv6_without_brackets_is_rejected(self):
        """不带方括号的 IPv6 必须报错，而不是把地址尾段当端口。"""
        with self.assertRaises(ValueError):
            self._parse('2409:8a60:2abf::1:19443')

    def test_bad_forms_rejected(self):
        for bad in ('', '127.0.0.1', '127.0.0.1:notaport', ':19443',
                    '[::1]', '[::1]19443', '127.0.0.1:0', '127.0.0.1:70000'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self._parse(bad)


class KernelPlacementTest(unittest.TestCase):
    """内核的位置与依赖边界，由门禁锁住，避免以后被"顺手挪回去"。

    内核住在 `shell/groupmesh`（与 `shell/backend`、`shell/frontend` 并列），
    不是 `tools/`、也不是某个插件内部。两条硬约束：

    1. 它必须能在**只装 pycryptodome** 的情况下跑起来 —— 这是"协议错误与插件
       集成错误能分开定位、跨机联调不需要先起图形界面"的前提；
    2. 因此它不能住在 `shell/backend/` **下面**：那个包的 `__init__.py` 会
       eagerly import `file_server`，从而把整个 Flask 栈拖进任何一次导入。
    """

    def test_kernel_importable_as_shell_package(self):
        import shell.groupmesh as kernel
        self.assertTrue(kernel.__version__)
        self.assertIn('shell', Path(kernel.__file__).parts or [])
        self.assertNotIn('tools', Path(kernel.__file__).resolve().parts)

    def test_kernel_does_not_drag_in_flask_or_gui_stack(self):
        """跑一次自检，确认全过程没有加载 Flask / pywebview。

        把探针写成临时脚本再执行，不用 `python -c`：内联多行代码在
        Windows 上要经过一层引号转义，很容易变成"测的是转义"而不是"测的是依赖"。
        """
        probe_source = '\n'.join([
            'import runpy',
            'import sys',
            '',
            "sys.argv = ['probe', 'selftest']",
            'try:',
            "    runpy.run_module('shell.groupmesh.cli', run_name='__main__')",
            'except SystemExit:',
            '    pass',
            '',
            "heavy = sorted({m.split('.')[0] for m in sys.modules",
            "                if m.split('.')[0] in ('flask', 'werkzeug', 'jinja2', 'webview')})",
            "print('HEAVY=' + ','.join(heavy))",
            '',
        ])
        with tempfile.TemporaryDirectory() as tmp:
            probe_path = Path(tmp) / 'probe_kernel_isolation.py'
            probe_path.write_text(probe_source, encoding='utf-8')
            # PYTHONPATH 指到仓库根：等价于"在仓库根执行 python -m shell.groupmesh.cli"，
            # 而脚本自身所在目录会被解释器自动加进 sys.path，所以不能只靠 cwd。
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONPATH=str(PROJECT_ROOT))
            # encoding 必须显式给 utf-8：子进程按 PYTHONIOENCODING 输出 UTF-8，
            # 而 text=True 用的是**父进程的 locale 编码**（中文 Windows 上是 GBK），
            # 解码失败发生在 subprocess 的读取线程里 —— 表现为 stdout 为空字符串、
            # 却看不到任何异常，排查成本很高。
            result = subprocess.run([sys.executable, str(probe_path)], cwd=str(PROJECT_ROOT),
                                    capture_output=True, text=True, timeout=180, env=env,
                                    encoding='utf-8', errors='replace')

        stdout = result.stdout or ''
        self.assertEqual(result.returncode, 0, (result.stderr or '')[-2000:])
        self.assertIn('全部 6 项自检通过', stdout)
        heavy = next((line.split('=', 1)[1] for line in stdout.splitlines()
                      if line.startswith('HEAVY=')), 'missing')
        self.assertEqual(heavy, '', f'内核不应加载图形栈，实际加载了: {heavy}')

    def test_cli_runs_as_module_from_repo_root(self):
        """`python -m shell.groupmesh.cli --version` 必须能从仓库根跑通。

        这条覆盖一类容易漏的回归：包内残留 `from groupmesh.x import y` 这类
        绝对导入（在 `python -m` 下会因为找不到顶层 `groupmesh` 而在**运行到那一行时**
        才失败 —— 只要那条路径没被走到，单元测试就发现不了）。
        """
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONPATH=str(PROJECT_ROOT))
        result = subprocess.run([sys.executable, '-m', 'shell.groupmesh.cli', '--version'],
                                cwd=str(PROJECT_ROOT), capture_output=True, text=True,
                                timeout=60, env=env, encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, (result.stderr or '')[-2000:])
        self.assertIn('group-mesh', result.stdout or '')


class LocalAddressesTest(unittest.TestCase):
    """§7.4 的地址过滤：至少不能把回环与链路本地当成可注册端点。"""

    def test_excludes_loopback_and_linklocal(self):
        from shell.groupmesh.registry import local_addresses
        addresses = local_addresses()
        for address in addresses:
            if ':' in address:
                self.assertFalse(address.startswith('fe80'), f'不应含链路本地地址 {address}')
                self.assertNotEqual(address, '::1')
            else:
                self.assertFalse(address.startswith('127.'), f'不应含回环地址 {address}')

    def test_prefers_ipv6_ordering(self):
        from shell.groupmesh.registry import local_addresses
        addresses = local_addresses(prefer_ipv6=True)
        if any(':' in a for a in addresses) and any(':' not in a for a in addresses):
            first_v6 = next(i for i, a in enumerate(addresses) if ':' in a)
            first_v4 = next(i for i, a in enumerate(addresses) if ':' not in a)
            self.assertLess(first_v6, first_v4, 'IPv6 应排在前面（设计文档以 IPv6 为目标）')


class ServeBindFailureTest(unittest.TestCase):
    """`serve()` 在 `bind` 失败时**不得泄漏监听套接字**。

    为什么这条必须有：实测踩到过一个"端口永远被占"的故障 —— 服务重启窗口期旧进程
    还占着端口，`bind` 抛错，而 socket 建在 `try` 之外，于是它既没被 close 也没人
    再引用，却仍以 LISTEN 状态占着端口（`ss` 显示该 socket 归 omnibox-web.service
    的 cgroup，当前进程里已经找不到它）。之后每次重试都失败，节点再也起不来，
    只能靠重启整个服务释放 —— 而"重试"正是自动启动在做的动作。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.identity = Identity.init(Path(self._tmp.name) / 'node', 'bind-probe', 'host')

    def test_bind_failure_raises_from_a_thread_and_the_port_becomes_followup_testable(self):
        """绑定失败必须抛出可捕获的 OSError（线程里也一样），且不得把进程搞崩。

        这条只保证"错误形态正确"。真正区分"显式 close"与"靠 GC 回收"的那条断言
        依赖 `/proc/self/fd`，只在 Linux 上跑得起来（见下一个用例）；在 Windows 上
        "端口能否立刻重绑"受系统保留段与 TIME_WAIT 影响，**不能**拿来做判定 ——
        我第一版就是这么写的，它在删掉修复后依然通过（假绿）。
        """
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(('127.0.0.1', 0))
        port = blocker.getsockname()[1]
        blocker.listen(1)

        errors: list = []
        marker: list = []

        def run():
            try:
                serve('127.0.0.1', port, self.identity, None)
            except BaseException as exc:
                errors.append(exc)
            marker.append(object())                   # 复刻"线程还要继续跑一段"的形态

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=10)
        blocker.close()

        self.assertFalse(thread.is_alive(), 'serve() 应当已因绑定失败返回')
        self.assertEqual(len(errors), 1, f'应当抛出一次绑定错误，实际 {errors}')
        self.assertIsInstance(errors[0], OSError)

    def test_stop_request_ends_the_accept_loop_and_frees_the_port(self):
        """`stop_requested` 置位后 accept 循环必须退出，**且端口真的被释放**。

        这条守的是"停止节点点了没用"：原先只有"另一个线程 close 套接字"这一条退出
        路径，而 Windows 上 `closesocket()` 不保证解开阻塞中的 `accept()`，线程可能
        永远卡住、套接字也关不掉 —— 界面显示已停止，端口却仍被占，再启动就是
        EADDRINUSE。改成 0.5 秒轮询 + 检查停止标志后，退出是确定性的。
        """
        stop = threading.Event()
        started = threading.Event()
        bound: list = []
        errors: list = []

        def run():
            try:
                serve('127.0.0.1', 0, self.identity, None,
                      ready=lambda listener: (bound.append(listener.getsockname()[1]),
                                              started.set()),
                      stop_requested=stop.is_set)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=5), 'serve() 应当把监听套接字交出来')
        port = bound[0]
        self.assertTrue(thread.is_alive(), '未请求停止时应当一直在服务')

        stop.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(),
                         '停止标志置位后 accept 循环应当在轮询间隔内退出')
        self.assertEqual(errors, [], f'serve() 不应因正常停止抛异常：{errors}')

        # 端口必须真的腾出来了（这正是用户能再次点"启动节点"的前提）
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError as e:
            self.fail(f'停止后端口 {port} 仍不可用：{e}')
        finally:
            probe.close()

    def test_stalled_connection_does_not_block_stopping(self):
        """一个"连上就不说话"的对端不得阻止节点停止。

        这是实测故障的复现：处理连接原先在 **accept 循环里同步**跑，而握手会阻塞在
        对端 socket 上最多 60 秒；"从另一个线程关闭套接字"在 Windows 上不保证解开
        阻塞中的 recv，于是循环回不到检查停止标志的地方 —— 日志里连续出现
        "停止节点超时：监听线程没有在 8 秒内退出"，端口也就一直放不出来
        （再启动就是 EADDRINUSE）。修法：每条连接单独开线程处理。
        """
        stop = threading.Event()
        started = threading.Event()
        bound: list = []
        errors: list = []

        def run():
            try:
                serve('127.0.0.1', 0, self.identity, None,
                      ready=lambda listener: (bound.append(listener.getsockname()[1]),
                                              started.set()),
                      stop_requested=stop.is_set)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        port = bound[0]

        # 制造一个"只连上、不发任何数据"的对端：服务端会卡在握手的第一步
        stalled = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.addCleanup(stalled.close)
        time.sleep(0.3)                                # 让它进入握手

        stop.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(),
                         '有僵死连接时停止也必须生效（accept 循环不该被它占住）')
        self.assertEqual(errors, [], f'serve() 不应因正常停止抛异常：{errors}')

    def test_bind_failure_does_not_leave_a_listening_socket(self):
        """绑定失败后不得留下**新增的** LISTEN 套接字（fd 级判定）。

        只比"这个进程现在有几个 LISTEN"是不够的：测试进程里本来就有别的监听套接字
        （实测远端上就有，导致我第一版断言 2 != 1 假失败）。因此取前后差集，并且
        复用异常里携带的那个套接字引用来定位它 —— 这样即使引用计数把它回收了，
        也能确认它当时确实被置为关闭。
        """
        if not Path('/proc/self/fd').is_dir():
            self.skipTest('该平台没有 /proc/self/fd，无法做 fd 级判定')

        # 为什么用 on_error 回调拿到那个套接字，而不是只断言"没有新增 LISTEN fd"：
        # 失败路径抛异常时，CPython 的引用计数/循环 GC 会顺手把 socket 回收，
        # 于是"忘了 close"这条缺陷在测试里**观察不到**（实测：删掉修复用例照样通过，
        # 只在 stderr 留一条 ResourceWarning）。回调让我们持有它、并**关掉 GC**，
        # 没有显式 close 时它就会一直以 LISTEN 状态留在 fd 表里。
        captured: list = []
        gc_was_enabled = gc.isenabled()
        gc.disable()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(('127.0.0.1', 0))
        port = blocker.getsockname()[1]
        blocker.listen(1)
        try:
            with self.assertRaises(OSError):
                serve('127.0.0.1', port, self.identity, None,
                      on_error=lambda sock, exc: captured.append(sock))
            self.assertEqual(len(captured), 1, '失败时应把监听套接字交给 on_error')
            listener = captured[0]
            # 显式关闭过的 fd 在这里已经无效（fileno() 为 -1 或 dup 直接失败）；
            # 没关的话它就还在 LISTEN —— 这正是"端口从此被占死"的现场。
            self.assertEqual(listener.fileno(), -1,
                             '绑定失败后监听套接字必须已被关闭（否则端口会一直被占）')
        finally:
            blocker.close()
            if gc_was_enabled:
                gc.enable()


if __name__ == '__main__':
    unittest.main()
