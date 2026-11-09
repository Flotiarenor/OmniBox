"""读应答的帧上限：一条读请求不得让服务端按文件大小分配内存。

背景（审计项 A4）：`_op_read` 曾写成 `length = min(length or size - offset, size - offset)`，
不带 `length`（或 `length=0`）的请求会把"剩余文件大小"整段读进内存，而帧上限要到
加密阶段才检查 —— 加密失败时数据已经在进程里了，于是有读权限的对端用一条请求就能
把服务端内存放大到文件大小。

本文件锁住四件事：

1. 8 MiB 文件的 0 长度读回 `too_large`，而不是把它整段读进来再失败；
2. 显式 `length` 超过帧上限同样被拒 —— 客户端的分块循环把"收到的字节少于请求的
   字节"当成文件已读完，静默截断会让它悄悄返回半份文件；
3. 分段读（显式 offset/length）语义完全保留：拼回来与原文件逐字节一致，
   `eof` / `offset` / `size` 字段仍按原规则给出；
4. 允许的最大单次读取编码成 JSON 后确实装得进 Noise 传输态的明文帧 ——
   否则"允许"等于"必然在发送阶段断连"。

运行：
    python -m unittest tests.test_group_mesh_read_limits -v
"""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.groupmesh import crypto_prims as cp
from shell.groupmesh.identity import Identity
from shell.groupmesh.node import MAX_READ_BYTES, Node
from shell.groupmesh.noise import MAX_TRANSPORT_FRAME
from shell.groupmesh.roster import founding_roster
from shell.groupmesh.shares import Acl, new_share
from shell.groupmesh.transport import MAX_FRAME_BYTES, _json_bytes, authorize_peer

LARGE_FILE_BYTES = 8 * 1024 * 1024
SMALL_TEXT = '小文件照常整段读取'


class ReadFrameLimitTest(unittest.TestCase):
    """`op=read` 的单条应答上限与分段读取语义。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.owner = Identity.init(root / 'owner', 'owner', 'owner-pc')
        cls.roster = founding_roster('frames', cls.owner.principal,
                                     [cls.owner.device.public_key], ttl_seconds=3600)

        cls.shared = root / 'shared'
        cls.shared.mkdir()
        # 逐字节可区分的内容：分段拼回时一旦错位，比较就不会通过
        cls.payload = bytes(range(256)) * (LARGE_FILE_BYTES // 256)
        (cls.shared / 'big.bin').write_bytes(cls.payload)
        (cls.shared / 'note.txt').write_bytes(SMALL_TEXT.encode('utf-8'))

        share = new_share('pub', str(cls.shared), owner_key=cls.owner.principal.public_key,
                          node_key=cls.owner.device.public_key,
                          node_private_key=cls.owner.device.private_key,
                          acl=Acl(read='group', write='owner'))
        cls.node = Node(identity=cls.owner, roster=cls.roster, shares={'pub': share})
        # 真实握手路径解析出来的对端身份：主体在名单里、只有读权限
        cls.peer = authorize_peer(cls.roster, cls.owner.device.public_key)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _read(self, path: str = 'big.bin', **fields):
        request = {'op': 'read', 'share': 'pub', 'path': path}
        request.update(fields)
        return self.node.handle(request, self.peer)

    def test_zero_length_read_of_large_file_is_rejected(self):
        """不带 length 与 length=0 是同一语义（读到末尾），两者都必须回 too_large。"""
        for label, response in (('缺省 length', self._read()),
                                ('length=0', self._read(length=0))):
            with self.subTest(case=label):
                self.assertEqual(response.get('status'), 'error', response)
                self.assertEqual(response.get('code'), 'too_large', response)
                self.assertNotIn('data', response)

    def test_explicit_length_over_frame_limit_is_rejected(self):
        """显式要求 8 MiB 也是同一条内存放大路径，不能因为"写了 length"就放行。"""
        response = self._read(length=LARGE_FILE_BYTES)
        self.assertEqual(response.get('status'), 'error', response)
        self.assertEqual(response.get('code'), 'too_large', response)

    def test_unspecified_length_still_reads_a_small_file_whole(self):
        """一帧装得下的文件，原有"不带 length 读完整份"的行为不变。"""
        response = self._read(path='note.txt')
        self.assertEqual(response.get('status'), 'ok', response)
        self.assertEqual(base64.b64decode(response['data']), SMALL_TEXT.encode('utf-8'))
        self.assertTrue(response['eof'])
        self.assertEqual(response['offset'], 0)
        self.assertEqual(response['size'], len(SMALL_TEXT.encode('utf-8')))

    def test_chunked_read_reassembles_the_original_file(self):
        """分段读回与原文件逐字节一致，且只有最后一帧置 eof。"""
        chunks = []
        offset = 0
        while True:
            response = self._read(offset=offset, length=MAX_READ_BYTES)
            self.assertEqual(response.get('status'), 'ok', response)
            self.assertEqual(response['offset'], offset)
            self.assertEqual(response['size'], LARGE_FILE_BYTES)
            data = base64.b64decode(response['data'])
            self.assertLessEqual(len(data), MAX_READ_BYTES)
            chunks.append(data)
            offset += len(data)
            if response['eof']:
                break
            self.assertGreater(len(data), 0, '未到 eof 却读不到数据，客户端循环会卡住')
        self.assertGreater(len(chunks), 1, '8 MiB 文件必须分多段才取得回来')
        self.assertEqual(offset, LARGE_FILE_BYTES)
        self.assertEqual(b''.join(chunks), self.payload)

    def test_largest_allowed_read_fits_the_transport_frame(self):
        """允许的最大读取必须真的发得出去，否则这条上限只是把错误推迟到发送阶段。

        读应答走的是 JSON + base64，密文 = 明文 + AEAD 标签：若把 `MAX_READ_BYTES`
        按传输层的 4 MiB 密文帧去算，base64 之后的 JSON 会超过 Noise 传输态 1 MiB
        的明文帧，于是"允许的读"每次都在加密时抛错断连。
        """
        response = self._read(offset=0, length=MAX_READ_BYTES)
        self.assertEqual(response.get('status'), 'ok', response)
        wire = _json_bytes(response)
        self.assertLessEqual(len(wire), MAX_TRANSPORT_FRAME)
        self.assertLessEqual(len(wire) + cp.TAGLEN, MAX_FRAME_BYTES)

    def test_read_budget_uses_the_frame_capacity(self):
        """`data` 的预算要用满帧，而不是变成一个与帧常量无关的常数。

        判据：base64 膨胀后的长度距离明文帧上限的差额，不得超过 JSON 外壳余量。
        若有人把 `MAX_READ_BYTES` 写成一个保守且与帧常量无关的数（例如两倍于
        `DEFAULT_CHUNK_BYTES`），差额会明显超过外壳余量，这条随之失败。
        """
        plaintext_budget = min(MAX_TRANSPORT_FRAME, MAX_FRAME_BYTES - cp.TAGLEN)
        base64_len = 4 * ((MAX_READ_BYTES + 2) // 3)
        self.assertLessEqual(plaintext_budget - base64_len, 4096 + 3,
                             '读上限没有用满帧：像是写死了一个与帧常量无关的数')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
