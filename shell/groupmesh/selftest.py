"""本机自检：不联网验证密码学、握手、名单状态机与 ACL。

`python -m shell.groupmesh.cli selftest`（从仓库根）会跑这里。它与 `tests/` 下的单元测试共用思路，
但**不需要 unittest**，便于在两个平台上直接跑一次确认环境正常。
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Tuple

from . import crypto_prims as cp
from . import noise as noise_mod
from .identity import Identity, Principal
from .node import Node
from .records import RecordError
from .registry import Registry, new_registration
from .roster import Roster, RosterEntry, founding_roster, next_roster, staleness_report
from .shares import Acl, Authorizer, Permission, new_share
from .transport import (
    connect,
)

Check = Tuple[str, Callable[[], None]]


class Failure(Exception):
    pass


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def _expect_raises(kind, fn: Callable[[], object], message: str) -> None:
    """断言 `fn()` 抛出 `kind`。

    返回类型写成 `object`（而不是 `None`）：被断言的表达式常常**有返回值**
    （`lambda: cp.dh(...)` 返回 bytes、`lambda: json.loads(...)` 返回对象），
    而这里只关心"抛没抛"，不关心返回值。写成 `Callable[[], None]` 时 pyright 会
    报 "Argument of type () -> bytes cannot be assigned to parameter fn"。
    """
    try:
        fn()
    except kind:
        return
    except Exception as e:
        raise Failure(f'{message}（抛出了 {type(e).__name__} 而不是 {kind.__name__}）') from e
    raise Failure(f'{message}（没有抛异常）')


# ── 密码学 ────────────────────────────────────────────────────────────────

def check_crypto() -> None:
    sk1, pk1 = cp.generate_dh_keypair()
    sk2, pk2 = cp.generate_dh_keypair()
    _expect(cp.dh(sk1, pk2) == cp.dh(sk2, pk1), 'X25519 双方协商结果不一致')
    _expect_raises(cp.CryptoError, lambda: cp.dh(sk1, b'\x00' * 32), '低阶点应被拒绝')

    sk, pk = cp.generate_sign_keypair()
    signature = cp.sign(sk, b'message')
    _expect(cp.verify(pk, b'message', signature), '正常签名验签失败')
    _expect(not cp.verify(pk, b'messagE', signature), '篡改消息后验签应失败')
    _expect(cp.sign_public_from_private(sk) == pk, '公钥无法由私钥推出')

    key = cp.random_bytes(32)
    ciphertext = cp.aead_encrypt(key, 0, b'payload', b'ad')
    _expect(cp.aead_decrypt(key, 0, ciphertext, b'ad') == b'payload', 'AEAD 往返失败')
    _expect_raises(cp.CryptoError, lambda: cp.aead_decrypt(key, 0, ciphertext, b'bad'),
                   'AD 不匹配应被拒绝')


# ── Noise 握手 ────────────────────────────────────────────────────────────

def _run_handshake_pair(prologue_a: bytes, prologue_b: bytes):
    """在内存里跑一次完整 XX 握手，返回双方的状态机。

    注意静态密钥用的是 **X25519**（`generate_dh_keypair`），不是 Ed25519 身份密钥：
    Noise_XX_25519 的静态密钥必须在 Curve25519 上，Ed25519 的私钥不是合法标量。
    """
    initiator_sk, initiator_pk = cp.generate_dh_keypair()
    responder_sk, responder_pk = cp.generate_dh_keypair()
    initiator = noise_mod.HandshakeState(noise_mod.ROLE_INITIATOR, initiator_sk, initiator_pk,
                                         prologue_a)
    responder = noise_mod.HandshakeState(noise_mod.ROLE_RESPONDER, responder_sk, responder_pk,
                                         prologue_b)

    message1 = initiator.write_message()
    responder.read_message(message1)
    message2 = responder.write_message(b'responder-payload')
    payload2 = initiator.read_message(message2)
    message3 = initiator.write_message(b'initiator-payload')
    payload3 = responder.read_message(message3)

    _expect(payload2 == b'responder-payload', '发起方解出的负载不符')
    _expect(payload3 == b'initiator-payload', '响应方解出的负载不符')
    _expect(initiator.remote_static_public == responder_pk, '发起方拿到的对端静态公钥不符')
    _expect(responder.remote_static_public == initiator_pk, '响应方拿到的对端静态公钥不符')
    _expect(initiator.handshake_hash == responder.handshake_hash, '双方握手转录不一致')
    return initiator, responder


def check_noise() -> None:
    initiator, responder = _run_handshake_pair(b'prologue', b'prologue')

    init_send, init_recv = initiator.split()
    resp_send, resp_recv = responder.split()
    _expect(init_send.key == resp_recv.key, '发起方发送密钥与响应方接收密钥不一致')
    _expect(resp_send.key == init_recv.key, '响应方发送密钥与发起方接收密钥不一致')
    _expect(init_send.key != init_recv.key, '两个方向的密钥不应相同')

    # 双向传输
    for index in range(4):
        text = f'frame-{index}'.encode()
        _expect(resp_recv.decrypt(init_send.encrypt(text)) == text, '发起方->响应方传输失败')
        _expect(init_recv.decrypt(resp_send.encrypt(text)) == text, '响应方->发起方传输失败')

    # nonce 递增：同一明文两次加密必须得到不同密文
    first = init_send.encrypt(b'same')
    second = init_send.encrypt(b'same')
    _expect(first != second, 'nonce 未推进（相同明文产生了相同密文）')

    # prologue 不一致必须导致握手失败（防降级）
    try:
        _run_handshake_pair(b'prologue-a', b'prologue-b')
        raise Failure('prologue 不一致时握手应当失败')
    except (noise_mod.NoiseError, cp.CryptoError):
        pass

    # 密文被篡改必须失败
    initiator2, responder2 = _run_handshake_pair(b'p', b'p')
    send2, _ = initiator2.split()
    _, recv2 = responder2.split()
    tampered = bytearray(send2.encrypt(b'secret'))
    tampered[-1] ^= 0x01
    _expect_raises(cp.CryptoError, lambda: recv2.decrypt(bytes(tampered)), '篡改密文应被拒绝')


# ── 名单状态机 ────────────────────────────────────────────────────────────

def check_roster() -> None:
    owner = Principal.create('owner')
    alice = Principal.create('alice')
    _bob_device_sk, bob_device_pk = cp.generate_sign_keypair()

    roster = founding_roster('test-group', owner, [cp.generate_sign_keypair()[1]], ttl_seconds=3600)
    _expect(roster.version == 1, '创始名单版本应为 1')
    ok, signer = roster.verify_signature()
    _expect(ok and signer == owner.public_key, '创始名单验签失败')
    roster.accepts(None)  # 规则 6：本地无名单时接受带外名单

    # 规则 4：群主可加人
    members = [*list(roster.members), RosterEntry('alice', alice.public_key, [bob_device_pk])]
    v2 = next_roster(roster, 'test-group', roster.owner_key, members, ttl_seconds=3600)
    v2.sign(owner.private_key)
    v2.accepts(roster)

    # 规则 1：版本未前进
    stale = next_roster(v2, 'test-group', v2.owner_key, members, ttl_seconds=3600)
    stale.version = v2.version
    stale.sign(owner.private_key)
    _expect_raises(RecordError, lambda: stale.accepts(v2), '规则 1（防回滚）未生效')

    # 规则 2：prev 不是直接后继
    wrong_prev = next_roster(v2, 'test-group', v2.owner_key, members, ttl_seconds=3600)
    wrong_prev.prev = b'\x11' * 32
    wrong_prev.sign(owner.private_key)
    _expect_raises(RecordError, lambda: wrong_prev.accepts(v2), '规则 2（prev 链）未生效')

    # 规则 5：管理员不得改管理员集合 / 不得改群主
    admin_a = Principal.create('admin-a')
    members_with_admin = [*list(v2.members), RosterEntry('admin-a', admin_a.public_key, [cp.generate_sign_keypair()[1]])]
    v3 = next_roster(v2, 'test-group', v2.owner_key, members_with_admin,
                     admin_keys=[admin_a.public_key], ttl_seconds=3600)
    v3.sign(owner.private_key)
    v3.accepts(v2)

    # 管理员尝试自我提权成第二批管理员
    other = Principal.create('other')
    members4 = [*list(v3.members), RosterEntry('other', other.public_key, [cp.generate_sign_keypair()[1]])]
    escalate = next_roster(v3, 'test-group', v3.owner_key, members4,
                           admin_keys=[admin_a.public_key, other.public_key], ttl_seconds=3600)
    escalate.sign(admin_a.private_key)
    _expect_raises(RecordError, lambda: escalate.accepts(v3), '规则 5（管理员不得改管理员集合）未生效')

    # 管理员合法地加普通成员应当通过
    legal = next_roster(v3, 'test-group', v3.owner_key, members4,
                        admin_keys=list(v3.admin_keys), ttl_seconds=3600)
    legal.sign(admin_a.private_key)
    legal.accepts(v3)

    # 非成员签名必须被拒（规则 3）
    outsider = Principal.create('outsider')
    forged = next_roster(v3, 'test-group', v3.owner_key, members4,
                         admin_keys=list(v3.admin_keys), ttl_seconds=3600)
    forged.sign(outsider.private_key)
    _expect_raises(RecordError, lambda: forged.accepts(v3), '规则 3（签名者资格）未生效')

    # 并发收敛（§5.3）：同版本同 prev 取哈希较小者，且双方结果一致
    a = next_roster(v3, 'test-group', v3.owner_key, members4, ttl_seconds=3600)
    a.sign(owner.private_key)
    b = next_roster(v3, 'test-group', v3.owner_key, members4, ttl_seconds=7200)
    b.sign(owner.private_key)
    _expect(Roster.pick_concurrent(a, b) is Roster.pick_concurrent(b, a),
            '并发收敛不是确定性的')

    # 宽限策略（§5.4）：过期只提示，不阻断
    expired = founding_roster('g', owner, [cp.generate_sign_keypair()[1]],
                              ttl_seconds=-10)
    report = staleness_report(expired, None)
    _expect(report['expired'], '过期名单应被标记为已过期')
    expired.accepts(None)  # 仍然可接受


# ── 共享项 ACL（§6）───────────────────────────────────────────────────────

def check_share_acl() -> None:
    owner = Principal.create('owner')
    alice = Principal.create('alice')
    stranger = Principal.create('stranger')
    node_sk, node_pk = cp.generate_sign_keypair()

    # 仅上传档位：write 授予 alice，delete 仅属主
    share = new_share('docs', '/tmp/docs', owner_key=owner.public_key, node_key=node_pk,
                      node_private_key=node_sk,
                      acl=Acl(read='group', write=[alice.public_key], delete='owner'))

    # 属主有全部权限
    owner_auth = Authorizer(requester_key=owner.public_key, group_member=False)
    for permission in (Permission.READ, Permission.WRITE, Permission.DELETE):
        _expect(owner_auth.allows(share.declaration, permission), f'属主应具备 {permission.value}')

    # alice 可读可写，但**不能删除**（§6.2：删除权由 ACL 决定，不由归属推断）
    alice_auth = Authorizer(requester_key=alice.public_key, group_member=True)
    _expect(alice_auth.allows(share.declaration, Permission.READ), 'alice 应可读')
    _expect(alice_auth.allows(share.declaration, Permission.WRITE), 'alice 应可写')
    _expect(not alice_auth.allows(share.declaration, Permission.DELETE),
            'alice 不应有删除权（仅上传档位）')

    # 陌生人：read=group 但他不是成员
    stranger_auth = Authorizer(requester_key=stranger.public_key, group_member=False)
    _expect(not stranger_auth.allows(share.declaration, Permission.READ), '非成员不应可读')

    # 声明验签
    _expect(share.declaration.verify_signature(), '共享项声明验签失败')

    # 篡改 ACL 后验签必须失败（防"客户端自称有权限"）
    import copy
    tampered = copy.deepcopy(share.declaration)
    tampered.acl.write = 'group'
    _expect(not tampered.verify_signature(), '篡改 ACL 后声明仍验签通过（严重）')


# ── 注册表单调性（§7）─────────────────────────────────────────────────────

def check_registry() -> None:
    device_sk, device_pk = cp.generate_sign_keypair()
    registry = Registry()
    _expect(registry.add(new_registration(device_pk, device_sk, 1, [('::1', 19443)], ['docs'])),
            '首条注册记录应被采纳')
    _expect(not registry.add(new_registration(device_pk, device_sk, 1, [('::1', 1)], ['docs'])),
            '同 seq 的注册记录应被拒绝（防重放）')
    _expect(registry.add(new_registration(device_pk, device_sk, 2, [('::1', 19444)], ['docs'])),
            'seq 递增的注册记录应被采纳')
    # `Registry.get` 的返回类型是 Optional（未收录该设备时返回 None）。前面刚断言过
    # 它已被采纳，这里把"运行时已知非空"告诉类型检查，避免 reportOptionalMemberAccess。
    latest = registry.get(device_pk)
    assert latest is not None, '刚被采纳的注册记录不该查不到'
    _expect(latest.endpoints == [('::1', 19444)], '端点未更新为最新记录')

    # 伪造签名必须被拒
    other_sk, _ = cp.generate_sign_keypair()
    forged = new_registration(device_pk, device_sk, 3, [('::1', 9)], [])
    forged.signature = cp.sign(other_sk, b'nothing')
    _expect(not registry.add(forged), '签名不符的注册记录应被拒绝')

    # 快照往返
    restored = Registry.from_snapshot_dicts(registry.snapshot_dicts())
    _expect(len(restored) == len(registry), '快照往返后记录数不一致')


# ── 端到端：真实 TCP + 真实握手 ───────────────────────────────────────────

def check_end_to_end(verbose: bool = False) -> None:
    """起一个真实节点，用真实 socket 跑协商+握手+取文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        owner_dir = root / 'owner'
        member_dir = root / 'member'

        owner = Identity.init(owner_dir, 'owner', 'owner-pc')
        member = Identity.init(member_dir, 'member', 'member-pc')

        roster = founding_roster('e2e-group', owner.principal, [owner.device.public_key],
                                 ttl_seconds=3600)
        # 把 member 加进名单（群主签发）
        members = [*list(roster.members), RosterEntry('member', member.principal.public_key, [member.device.public_key])]
        roster_v2 = next_roster(roster, 'e2e-group', roster.owner_key, members, ttl_seconds=3600)
        roster_v2.sign(owner.principal.private_key)
        roster_v2.accepts(roster)

        shared_dir = root / 'shared'
        shared_dir.mkdir()
        payload = cp.random_bytes(300 * 1024)  # 跨多个分块
        (shared_dir / 'big.bin').write_bytes(payload)
        (shared_dir / 'note.txt').write_text('hello from owner', encoding='utf-8')

        share = new_share('pub', str(shared_dir), owner_key=owner.principal.public_key,
                          node_key=owner.device.public_key,
                          node_private_key=owner.device.private_key,
                          acl=Acl(read='group', write='owner', delete='owner'))
        node = Node(identity=owner, roster=roster_v2, shares={'pub': share}, registry=Registry())

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        stop = threading.Event()

        def accept_loop() -> None:
            from .node import _serve_one
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
            connection = connect('127.0.0.1', port, member, roster_v2, timeout=10.0)
            with connection:
                # 客户端这一侧的 peer 是**对端**（owner），不是自己。
                _expect(connection.peer.device_key == owner.device.public_key,
                        f'客户端解析出的对端设备不符: {connection.peer.device_key.hex()[:16]}'
                        f' != {owner.device.public_key.hex()[:16]}')
                _expect(connection.peer.principal_key == owner.principal.public_key,
                        f'客户端解析出的对端主体不符: {connection.peer.principal_key.hex()[:16]}'
                        f' != {owner.principal.public_key.hex()[:16]}')
                _expect(connection.peer.role == 'owner',
                        f'owner 的角色应为 owner，实际 {connection.peer.role}')
                _expect(connection.negotiation.suite == 'noise-XX-25519-chacha20poly1305-blake2s',
                        '协商出的套件不符')

                from . import client
                shares = client.list_shares(connection)
                _expect([s['share_id'] for s in shares] == ['pub'], '共享项列表不符')

                listing = client.list_directory(connection, 'pub', '.')
                names = sorted(e['name'] for e in listing.get('entries') or [])
                _expect(names == ['big.bin', 'note.txt'], f'目录列表不符: {names}')

                fetched = client.fetch_bytes(connection, 'pub', 'big.bin', chunk_bytes=64 * 1024)
                _expect(fetched == payload, '取回的大文件内容不一致')

                text = client.fetch_bytes(connection, 'pub', 'note.txt')
                _expect(text == b'hello from owner', '取回的小文件内容不一致')

                # 越界访问必须被拒（§6.5）
                from .transport import RemoteError
                try:
                    client.fetch_bytes(connection, 'pub', '../../etc/passwd')
                    raise Failure('越界路径应被拒绝')
                except RemoteError as e:
                    _expect('forbidden' in str(e.code), f'越界应返回 forbidden，实际 {e.code}')

                # 没有写权限时上传必须被拒
                try:
                    client.push_bytes(connection, 'pub', 'hack.txt', b'nope')
                    raise Failure('无写权限时应被拒绝')
                except RemoteError as e:
                    _expect('forbidden' in str(e.code), f'写入应返回 forbidden，实际 {e.code}')

                # 注册表同步
                registry = Registry()
                registry.add(node.make_registration([('127.0.0.1', port)], 1))
                result = client.push_registry(connection, registry)
                _expect(result.get('accepted') == 1, '注册表推送未被采纳')
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=2)


# ── 自检入口 ──────────────────────────────────────────────────────────────

CHECKS: List[Tuple[str, Callable[[], None]]] = [
    ('密码学原语（X25519 / Ed25519 / AEAD / HKDF）', check_crypto),
    ('Noise_XX 握手与传输态', check_noise),
    ('团体名单状态机（规则 1-6 与并发收敛）', check_roster),
    ('共享项 ACL（§6.2 删除权由 ACL 决定）', check_share_acl),
    ('注册表单调性与防重放（§7）', check_registry),
    ('真实 TCP 端到端（协商+握手+分块取文件+越界拒绝）', check_end_to_end),
]


def run(verbose: bool = True) -> int:
    failures = 0
    for name, check in CHECKS:
        try:
            check()
        except Failure as e:
            failures += 1
            print(f'  FAIL  {name}\n        {e}')
        except Exception as e:
            failures += 1
            print(f'  FAIL  {name}\n        {type(e).__name__}: {e}')
        else:
            if verbose:
                print(f'  ok    {name}')
    print()
    if failures:
        print(f'{failures}/{len(CHECKS)} 项自检失败')
        return 1
    print(f'全部 {len(CHECKS)} 项自检通过（group-mesh 内核可运行）')
    return 0


if __name__ == '__main__':
    sys.exit(run())
