"""Noise_XX 官方测试向量的字节级互通。

夹具 `tests/fixtures/noise_XX_25519_ChaChaPoly_BLAKE2s.json` 逐字段取自
cacophony 官方向量（`noiseprotocol` 仓库 `tests/vectors/cacophony.txt`，
Noise 官方测试向量规范），不是什么"本实现输出的快照"。期望值来自夹具，
不来自被测代码 —— 这一点是 §5.14"测试复制被测常量"教训的直接应用。

向量驱动的是我们用 `noiseprotocol` 包装出来的 `HandshakeState`：
如果包装层的角色、prologue、静态/临时密钥注入、nonce 延续任何一处接错，
第 1 条消息的字节就对不上，而不是等到 AEAD 才炸。
"""

from __future__ import annotations

import json
import unittest
import warnings
from pathlib import Path

from shell.groupmesh import crypto_prims as cp
from shell.groupmesh import noise as nm

FIXTURE = (Path(__file__).resolve().parent / 'fixtures'
           / 'noise_XX_25519_ChaChaPoly_BLAKE2s.json')


def _hex(value: str) -> bytes:
    return bytes.fromhex(value)


class NoiseOfficialVectorTest(unittest.TestCase):
    """用官方 cacophony 向量锁住握手 + 传输的每一个字节。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.vector = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def _pair(self):
        v = self.vector
        init_static, init_eph = _hex(v['init_static']), _hex(v['init_ephemeral'])
        resp_static, resp_eph = _hex(v['resp_static']), _hex(v['resp_ephemeral'])
        init_pub = cp.dh_public_from_private(init_static)
        resp_pub = cp.dh_public_from_private(resp_static)
        with warnings.catch_warnings():
            # 向量要求固定临时密钥；库会对"预置 e"发一条测试用途警告
            warnings.simplefilter('ignore')
            initiator = nm.HandshakeState(nm.ROLE_INITIATOR, init_static, init_pub,
                                          _hex(v['init_prologue']),
                                          ephemeral_private=init_eph)
            responder = nm.HandshakeState(nm.ROLE_RESPONDER, resp_static, resp_pub,
                                          _hex(v['resp_prologue']),
                                          ephemeral_private=resp_eph)
        return initiator, responder

    def test_handshake_and_transport_match_official_vector(self):
        v = self.vector
        initiator, responder = self._pair()
        messages = v['messages']
        self.assertEqual(len(messages), 6, 'XX 向量应为 3 条握手 + 3 条传输')

        init_to_resp = True
        split_pair = None
        handshake_done = False
        for index, item in enumerate(messages):
            payload = _hex(item['payload'])
            expected = _hex(item['ciphertext'])
            if not handshake_done:
                sender, receiver = ((initiator, responder) if init_to_resp
                                    else (responder, initiator))
                message = sender.write_message(payload)
                self.assertEqual(message, expected,
                                 f'第 {index} 条握手消息与官方向量不一致')
                self.assertEqual(receiver.read_message(message), payload)
                if sender.complete and receiver.complete:
                    handshake_done = True
                    self.assertEqual(initiator.handshake_hash, _hex(v['handshake_hash']))
                    self.assertEqual(responder.handshake_hash, initiator.handshake_hash)
                    init_send, init_recv = initiator.split()
                    resp_send, resp_recv = responder.split()
                    self.assertEqual(init_send.key, resp_recv.key)
                    self.assertEqual(init_recv.key, resp_send.key)
                    self.assertNotEqual(init_send.key, init_recv.key)
                    split_pair = (init_send, init_recv, resp_send, resp_recv)
                else:
                    self.assertEqual(sender.complete, receiver.complete,
                                     '两端完成状态必须同步推进')
            else:
                assert split_pair is not None
                init_send, init_recv, resp_send, resp_recv = split_pair
                sender, receiver = ((init_send, resp_recv) if init_to_resp
                                    else (resp_send, init_recv))
                ciphertext = sender.encrypt(payload)
                self.assertEqual(ciphertext, expected,
                                 f'第 {index} 条传输消息与官方向量不一致')
                self.assertEqual(receiver.decrypt(expected), payload)
            init_to_resp = not init_to_resp

        self.assertTrue(handshake_done)

    def test_tampered_handshake_message_is_rejected(self):
        v = self.vector
        initiator, responder = self._pair()
        first = initiator.write_message(_hex(v['messages'][0]['payload']))
        responder.read_message(first)
        # 第一条消息（-> e）的负载是明文，改它不会触发 AEAD；必须改**加密**消息。
        second = bytearray(responder.write_message(_hex(v['messages'][1]['payload'])))
        second[-1] ^= 0x01
        with self.assertRaises(nm.NoiseError):
            initiator.read_message(bytes(second))


class NoiseRawInteropTest(unittest.TestCase):
    """我们的包装层必须能与**原生 noiseprotocol** 对端互通（双向传输）。"""

    def test_wrapper_handshakes_with_raw_noiseprotocol(self):
        try:
            from noise.connection import Keypair, NoiseConnection
        except ImportError:  # pragma: no cover
            self.skipTest('未安装 noiseprotocol')

        prologue = b'group-mesh-raw-interop'
        init_priv, init_pub = cp.generate_dh_keypair()
        resp_priv, resp_pub = cp.generate_dh_keypair()

        initiator = nm.HandshakeState(nm.ROLE_INITIATOR, init_priv, init_pub, prologue)
        responder = NoiseConnection.from_name(nm.NOISE_LIB_NAME)
        responder.set_prologue(prologue)
        responder.set_as_responder()
        responder.set_keypair_from_private_bytes(Keypair.STATIC, resp_priv)
        responder.start_handshake()

        msg1 = initiator.write_message(b'one')
        self.assertEqual(responder.read_message(msg1), b'one')
        msg2 = responder.write_message(b'two')
        self.assertEqual(initiator.read_message(msg2), b'two')
        msg3 = initiator.write_message(b'three')
        self.assertEqual(responder.read_message(msg3), b'three')

        self.assertEqual(initiator.remote_static_public, resp_pub)
        # 原生对端内部不会把 rs 回填到 NoiseProtocol.keypairs（库的行为），
        # 因此这里不读它的内部字段；双向传输互通已经证明它认出了发起方。

        init_send, init_recv = initiator.split()
        resp_send = responder.noise_protocol.cipher_state_encrypt
        resp_recv = responder.noise_protocol.cipher_state_decrypt
        self.assertEqual(init_send.key, bytes(resp_recv.k))
        self.assertEqual(init_recv.key, bytes(resp_send.k))

        # 双向传输：包装层 -> 原生、原生 -> 包装层
        for text in (b'alpha', b'beta'):
            self.assertEqual(bytes(resp_recv.decrypt_with_ad(None, init_send.encrypt(text))), text)
            self.assertEqual(init_recv.decrypt(bytes(resp_send.encrypt_with_ad(None, text))), text)

    def test_prologue_mismatch_fails(self):
        a_priv, a_pub = cp.generate_dh_keypair()
        b_priv, b_pub = cp.generate_dh_keypair()
        a = nm.HandshakeState(nm.ROLE_INITIATOR, a_priv, a_pub, b'prologue-a')
        b = nm.HandshakeState(nm.ROLE_RESPONDER, b_priv, b_pub, b'prologue-b')
        # 第一条消息（-> e）还没有 AEAD 密钥，prologue 不一致要到第二条
        # （<- e, ee, ...）解密时才会暴露。
        b.read_message(a.write_message())
        with self.assertRaises(nm.NoiseError):
            a.read_message(b.write_message())


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
