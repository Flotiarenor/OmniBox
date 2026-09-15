#!/usr/bin/env python3
"""group-mesh 命令行入口。

    python -m shell.groupmesh.cli <子命令> [参数]      # 从仓库根运行

Windows 与 Linux 用同一份代码，`python` 换成各自解释器即可（见 README.md）。
内核不依赖 Shell 的其余部分（不 import Flask / pywebview），因此这条命令
在没有任何图形环境、甚至没装桌面依赖的机器上照样能跑。

子命令一览：

    init      新建主体与首台设备
    whoami    显示本机身份
    create    创建团体（创始人即群主）
    roster    查看/管理名单（show / add / remove / promote / demote / transfer）
    join      用带外收到的名单加入团体
    share     管理本机共享项（add / list / remove）
    serve     作为共享节点监听，接受团体成员连接
    peers     列出对端可见的共享项
    ls        列出对端共享项里的目录
    stat      查看对端某个文件的大小
    get       从对端取回文件
    put       向对端共享项上传文件（需要写权限）
    rm        删除对端共享项里的文件（需要删除权限）
    registry  注册表同步（pull / push / show）
    selftest  本机自检：密码学 + 握手 + 名单状态机
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # 作为包运行：python -m shell.groupmesh.cli（推荐）
    from . import __version__ as KERNEL_VERSION
    from . import client
    from . import crypto_prims as cp
    from .identity import Identity
    from .records import RecordError, pretty
    from .registry import Registry, local_addresses, new_registration
    from .roster import Roster, RosterEntry, founding_roster, next_roster, staleness_report
    from .shares import Acl, new_share
    from .transport import RemoteError, TransportError
except ImportError:  # pragma: no cover - 直接执行脚本文件时回落到绝对导入
    # shell/groupmesh/cli.py -> parents[2] 才是仓库根，仓库根里才有 shell/ 这个
    # namespace package
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from groupmesh import __version__ as KERNEL_VERSION
    from groupmesh import client
    from groupmesh import crypto_prims as cp
    from groupmesh.identity import Identity
    from groupmesh.records import RecordError, pretty
    from groupmesh.registry import Registry, local_addresses, new_registration
    from groupmesh.roster import Roster, RosterEntry, founding_roster, next_roster, staleness_report
    from groupmesh.shares import Acl, new_share
    from groupmesh.transport import RemoteError, TransportError

DEFAULT_IDENTITY = 'identity'
ROSTER_FILE = 'roster.json'
SHARES_FILE = 'shares.json'
REGISTRY_FILE = 'registry.json'
STATE_FILE = 'state.json'


# ── 状态读写 ──────────────────────────────────────────────────────────────

def _state_path(root: Path) -> Path:
    return Path(root) / STATE_FILE


def load_state(root: Path) -> Dict[str, Any]:
    path = _state_path(root)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def save_state(root: Path, state: Dict[str, Any]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def load_roster(root: Path) -> Optional[Roster]:
    path = Path(root) / ROSTER_FILE
    if not path.exists():
        return None
    return Roster.from_dict(json.loads(path.read_text(encoding='utf-8')))


def save_roster(root: Path, roster: Roster) -> None:
    path = Path(root) / ROSTER_FILE
    path.write_text(json.dumps(roster.to_dict(), ensure_ascii=False, indent=2) + '\n',
                    encoding='utf-8')


def load_shares(root: Path, identity: Identity) -> Dict[str, Any]:
    from .shares import LocalShare
    path = Path(root) / SHARES_FILE
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding='utf-8'))
    shares = {}
    for share_id, item in (raw.get('shares') or {}).items():
        try:
            shares[share_id] = LocalShare.from_dict(item)
        except RecordError as e:
            print(f'[!] 跳过损坏的共享项 {share_id}: {e}', file=sys.stderr)
    return shares


def save_shares(root: Path, shares: Dict[str, Any]) -> None:
    path = Path(root) / SHARES_FILE
    payload = {'shares': {sid: share.to_dict() for sid, share in shares.items()}}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def load_registry(root: Path) -> Registry:
    path = Path(root) / REGISTRY_FILE
    if not path.exists():
        return Registry()
    return Registry.from_snapshot_dicts(json.loads(path.read_text(encoding='utf-8')).get('records'))


def save_registry(root: Path, registry: Registry) -> None:
    path = Path(root) / REGISTRY_FILE
    payload = {'records': registry.snapshot_dicts()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def require_identity(args: argparse.Namespace) -> Identity:
    try:
        return Identity.load(Path(args.dir), args.device)
    except RecordError as e:
        die(f'{e}\n提示：先跑 `init --dir {args.dir} --name <你的名字>`')


def require_roster(args: argparse.Namespace) -> Roster:
    roster = load_roster(Path(args.dir))
    if roster is None:
        die(f'{args.dir} 下没有 {ROSTER_FILE}。先跑 create 或 join。')
    return roster


def die(message: str, code: int = 1) -> None:
    print(f'错误: {message}', file=sys.stderr)
    raise SystemExit(code)


def info(message: str) -> None:
    print(message)


# ── 子命令实现 ────────────────────────────────────────────────────────────

def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    if args.force and (root / 'principal.json').exists():
        # 覆盖私钥是不可逆操作（等于销毁身份），必须走显式确认路径
        die('拒绝用 --force 覆盖已有身份：私钥一旦丢失，该主体签发的共享项与名单角色都不可恢复。'
            '请删除目录后重建，或换一个 --dir。')
    identity = Identity.init(root, args.name, args.device_name)
    state = load_state(root)
    state.setdefault('registration_seq', 0)
    save_state(root, state)
    info(f'身份已创建于 {root}')
    info(identity.dump())
    info('')
    info('下一步：')
    info(f'  python -m shell.groupmesh.cli create --dir {root} --group <团体名>')
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    identity = require_identity(args)
    roster = load_roster(Path(args.dir))
    info(identity.dump())
    if roster is not None:
        entry = roster.entry_of(identity.principal.public_key)
        info(json.dumps({'group': roster.group, 'role': roster.role_of(identity.principal.public_key),
                         'in_roster': entry is not None,
                         'roster_version': roster.version,
                         'stale': staleness_report(roster, None)}, ensure_ascii=False, indent=2))
    else:
        info('（尚无团体名单）')
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    identity = require_identity(args)
    if load_roster(root) is not None:
        die(f'{root} 下已有 {ROSTER_FILE}。删除它才能重建团体（这会丢掉当前名单链）。')
    roster = founding_roster(args.group, identity.principal, [identity.device.public_key],
                             ttl_seconds=args.ttl_days * 86400)
    save_roster(root, roster)
    info(f'团体 {args.group!r} 已创建，你是群主。')
    info(pretty(roster.to_dict()))
    info('')
    info('把这行「邀请串」通过可信渠道（面对面/二维码/局域网配对）交给成员，让他们 join：')
    info('  ' + invite_string(roster))
    return 0


def invite_string(roster: Roster) -> str:
    """把名单压成一行便于复制的邀请串（带外分发用）。"""
    raw = json.dumps(roster.to_dict(), separators=(',', ':'), sort_keys=True).encode('utf-8')
    return 'gm1:' + base64.urlsafe_b64encode(raw).decode('ascii')


def parse_invite(text: str) -> Roster:
    text = text.strip()
    if not text.startswith('gm1:'):
        die('邀请串必须以 gm1: 开头')
    try:
        raw = base64.urlsafe_b64decode(text[4:].encode('ascii'))
        return Roster.from_dict(json.loads(raw.decode('utf-8')))
    except Exception as e:
        die(f'邀请串解析失败: {e}')


def cmd_join(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    identity = require_identity(args)
    roster = parse_invite(args.invite)
    current = load_roster(root)
    try:
        # §5.2 规则 6：本地无名单时直接信任带外名单；有名单时走完整规则链
        roster.accepts(current)
    except RecordError as e:
        die(f'名单未被接受: {e}')
    save_roster(root, roster)
    state = load_state(root)
    state.setdefault('registration_seq', 0)
    save_state(root, state)
    entry = roster.entry_of(identity.principal.public_key)
    info(f'已加入团体 {roster.group!r}（名单版本 {roster.version}）')
    if entry is None:
        info('[!] 注意：本主体还不在名单里，因此其他成员不会接受你的连接。')
        info('    请群主/管理员在你的设备上执行：roster add --principal <你的主体公钥> '
             '--dev <你的设备公钥>')
    else:
        info(f'你的角色：{roster.role_of(identity.principal.public_key)}')
    return 0


def cmd_roster(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    identity = require_identity(args)
    roster = require_roster(args)

    if args.action == 'show':
        info(json.dumps(roster.to_dict(), ensure_ascii=False, indent=2))
        info('')
        info('宽限提示（§5.4，仅提示不阻断）:')
        info(json.dumps(staleness_report(roster, None), ensure_ascii=False, indent=2))
        return 0

    # 下面都是"改名单"：构造后继 -> 用本机私钥签名 -> accepts 自检 -> 落盘
    members = [RosterEntry(m.name, m.principal_key, list(m.device_keys)) for m in roster.members]
    admins = list(roster.admin_keys)
    owner_key = roster.owner_key
    changed = None

    if args.action == 'add':
        principal = cp.b64d(args.principal)
        if len(principal) != 32:
            die('--principal 必须是 32 字节公钥的 base64')
        device_keys = [cp.b64d(d) for d in (args.device_keys or [])]
        for key in device_keys:
            if len(key) != 32:
                die('--dev 必须是 32 字节公钥的 base64')
        existing = next((m for m in members if m.principal_key == principal), None)
        if existing is not None:
            # 幂等：主体已在名单里时，把新设备并进它的 devices 列表。
            # 为什么需要这条：成员可能先被加入（还没登记设备），之后才把自己的
            # 设备公钥给到群主；直接报"已在名单里"会让群主无法补登记。
            added = [k for k in device_keys if k not in existing.device_keys]
            if not added:
                die('该主体已在名单里，且给出的设备也都已登记（无需改动）')
            existing.device_keys.extend(added)
            changed = f'已为 {existing.name} 补登记 {len(added)} 台设备'
        else:
            members.append(RosterEntry(args.name or principal[:8].hex(), principal, device_keys))
            changed = f'已添加成员 {args.name or principal[:8].hex()}'
            if not device_keys:
                info('[!] 该成员尚未登记任何设备，因此它连不上其他节点。'
                     '拿到对方的设备公钥后再跑一次 roster add 即可补登记。')

    elif args.action == 'remove':
        target = cp.b64d(args.principal)
        if target == roster.owner_key:
            die('不能移除群主。若要换人，请用 roster transfer。')
        before = len(members)
        members = [m for m in members if m.principal_key != target]
        if len(members) == before:
            die('该主体不在名单里')
        admins = [a for a in admins if a != target]
        changed = '已移除成员（注意 §5.4：移除生效窗口没有上界）'

    elif args.action in ('promote', 'demote'):
        target = cp.b64d(args.principal)
        if target == roster.owner_key:
            die('群主不需要管理员身份')
        if not any(m.principal_key == target for m in members):
            die('该主体不在名单里')
        if args.action == 'promote':
            if target not in admins:
                admins.append(target)
            changed = '已任命管理员'
        else:
            admins = [a for a in admins if a != target]
            changed = '已撤销管理员'

    elif args.action == 'transfer':
        # §5.6：群主密钥失效前唯一的自救手段
        target = cp.b64d(args.principal)
        if not any(m.principal_key == target for m in members):
            die('新群主必须是名单里的成员')
        owner_key = target
        admins = [a for a in admins if a != target]
        changed = '已转移群主（原群主降为普通成员）'

    else:
        die(f'未知的 roster 动作: {args.action}')

    candidate = next_roster(roster, group=roster.group, owner_key=owner_key, members=members,
                            admin_keys=admins, ttl_seconds=args.ttl_days * 86400)
    candidate.sign(identity.principal.private_key)
    try:
        candidate.accepts(roster)
    except RecordError as e:
        die(f'本机自检拒绝了这次改动（说明你的角色无权这么做）: {e}')
    save_roster(root, candidate)
    info(f'{changed}；名单版本 {roster.version} -> {candidate.version}')
    info('新的邀请串（分发给全体成员）：')
    info('  ' + invite_string(candidate))
    return 0


def cmd_share(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    identity = require_identity(args)
    shares = load_shares(root, identity)

    if args.action == 'list':
        if not shares:
            info('（本机没有共享项）')
            return 0
        for share_id, share in shares.items():
            info(f'{share_id:20s} {share.path}  (max_bytes={share.max_bytes})')
            info('    ' + json.dumps(share.declaration.acl.to_dict(), ensure_ascii=False))
        return 0

    if args.action == 'remove':
        if args.share_id not in shares:
            die(f'没有共享项 {args.share_id!r}')
        del shares[args.share_id]
        save_shares(root, shares)
        info(f'已移除共享项 {args.share_id}')
        return 0

    if args.action == 'add':
        path = Path(args.path).resolve()
        if not path.exists():
            die(f'路径不存在: {path}')
        acl = Acl(read=args.read, write=args.write, delete=args.delete)
        share = new_share(share_id=args.share_id, path=str(path),
                          owner_key=identity.principal.public_key,
                          node_key=identity.device.public_key,
                          node_private_key=identity.device.private_key,
                          acl=acl, seq=1,
                          max_bytes=None if args.max_bytes == 0 else args.max_bytes)
        shares[args.share_id] = share
        save_shares(root, shares)
        info(f'共享项 {args.share_id} -> {path}')
        info('  ' + json.dumps(share.declaration.acl.to_dict(), ensure_ascii=False))
        info('  安全提醒（§6.5）：不要把程序目录、配置目录作为共享根；'
             '授予写权限等于允许对方在该目录内占用磁盘。')
        return 0

    die(f'未知的 share 动作: {args.action}')
    return 1


def _serve_loop(args: argparse.Namespace, identity: Identity, roster: Roster,
                shares: Dict[str, Any], registry: Registry,
                roster_loader=None) -> None:
    from .node import serve

    port = args.port
    bind = args.bind
    addresses = local_addresses()

    def publish(listener) -> None:
        actual_port = listener.getsockname()[1]
        # 通配地址无法上报，换成实际的本机地址列表；显式指定的地址照原样上报
        advertised = addresses if bind in ('0.0.0.0', '::') else [bind]
        endpoints = [(addr, actual_port) for addr in advertised]
        state = load_state(Path(args.dir))
        seq = int(state.get('registration_seq', 0)) + 1
        registration = new_registration(identity.device.public_key, identity.device.private_key,
                                        seq=seq, endpoints=endpoints, shares=sorted(shares))
        registry.add(registration)
        save_registry(Path(args.dir), registry)
        state['registration_seq'] = seq
        state['last_endpoints'] = endpoints
        save_state(Path(args.dir), state)
        info(f'共享节点已监听 {bind}:{actual_port}')
        info(f'注册记录 seq={seq}，端点：{endpoints}')
        info(f'共享项：{sorted(shares) or "（无）"}')
        info(f'注册表内 {len(registry)} 条记录')
        info('按 Ctrl+C 停止。')

    try:
        serve(bind, port, identity, roster, shares=shares, registry=registry,
              roster_loader=roster_loader, ready=publish)
    except KeyboardInterrupt:
        info('\n已停止监听。')
    except OSError as e:
        die(f'无法监听 {bind}:{port}：{e}')


def cmd_serve(args: argparse.Namespace) -> int:
    identity = require_identity(args)
    roster = require_roster(args)
    shares = load_shares(Path(args.dir), identity)
    registry = load_registry(Path(args.dir))
    # 让节点在每次建连时重读名单：群主在另一处执行 roster add 后，
    # 运行中的节点必须立刻认新成员，而不是要求重启。
    def roster_loader() -> Optional[Roster]:
        return load_roster(Path(args.dir))

    _serve_loop(args, identity, roster, shares, registry, roster_loader=roster_loader)
    return 0


def parse_target(target: str) -> tuple:
    """把 `--target` 解析成 (host, port)。

    必须同时接受两种形态：

        host:port                —— IPv4 或主机名
        [2409:8a60::1]:19443     —— IPv6（地址自带冒号，必须带方括号）

    不解析方括号时 `rpartition(':')` 会把 IPv6 地址切在最后一个冒号上，
    把地址尾段当成端口 —— 表现为"端口不是整数"这种与真正原因无关的报错。
    """
    text = (target or '').strip()
    if not text:
        raise ValueError('--target 不能为空（形如 host:port 或 [IPv6]:port）')
    if text.startswith('['):
        closing = text.find(']')
        if closing < 0:
            raise ValueError(f'IPv6 目标缺少右方括号: {target!r}')
        host = text[1:closing]
        rest = text[closing + 1:]
        if not rest.startswith(':'):
            raise ValueError(f'IPv6 目标缺少端口: {target!r}')
        port_text = rest[1:]
    else:
        if ':' not in text:
            raise ValueError(f'--target 应为 host:port，收到 {target!r}')
        # 未加方括号的 IPv6：地址里本来就有多个冒号，按最后一个切会得到一个
        # 看似合法的 (地址前缀, 端口) 组合，直到 DNS 解析才以无关的报错失败。
        # 这里直接拒绝，并把正确写法写进错误信息。
        if text.count(':') > 1:
            raise ValueError(
                f'IPv6 目标必须写成 [地址]:端口，收到 {target!r}'
                f'（例如 [2409:8a60::1]:19443）')
        host, _, port_text = text.rpartition(':')
    if not host:
        raise ValueError(f'--target 缺少主机部分: {target!r}')
    try:
        port = int(port_text)
    except ValueError as e:
        raise ValueError(f'端口不是整数: {port_text!r}') from e
    if not 1 <= port <= 65535:
        raise ValueError(f'端口越界: {port}')
    return host, port


def _require_target(args: argparse.Namespace):
    """按 --target host:port 连接对端。"""
    if not args.target:
        die('需要 --target host:port（对端 serve 输出的监听地址）')
    try:
        host, port = parse_target(args.target)
    except ValueError as e:
        die(str(e))
    identity = require_identity(args)
    roster = require_roster(args)
    try:
        connection = client.open_connection(host, port, identity, roster, timeout=args.timeout)
    except (TransportError, OSError) as e:
        die(f'连接失败: {e}')
    except RemoteError as e:
        die(f'对端拒绝: {e}')
    info(f'已连接 {host}:{port} —— 对端主体 {connection.peer.principal_id}'
         f'（{connection.peer.name}，角色 {connection.peer.role}）')
    return connection


def cmd_peers(args: argparse.Namespace) -> int:
    connection = _require_target(args)
    with connection:
        shares = client.list_shares(connection)
        if not shares:
            info('对端没有你可读的共享项。')
            return 0
        for share in shares:
            info(json.dumps(share, ensure_ascii=False))
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    connection = _require_target(args)
    with connection:
        result = client.list_directory(connection, args.share_id, args.path)
        for entry in result.get('entries') or []:
            kind = 'd' if entry['dir'] else '-'
            info(f"{kind} {entry['size']:>12} {entry['name']}")
        if not result.get('entries'):
            info('（空目录）')
    return 0


def cmd_stat(args: argparse.Namespace) -> int:
    connection = _require_target(args)
    with connection:
        info(json.dumps(client.stat_share(connection, args.share_id, args.path),
                        ensure_ascii=False, indent=2))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    connection = _require_target(args)
    with connection:
        def progress(done: int, total: int) -> None:
            if total:
                print(f'\r  下载 {done}/{total} 字节（{done * 100 // max(total, 1)}%）',
                      end='', file=sys.stderr)

        destination = Path(args.output).resolve()
        written = client.fetch_to_file(connection, args.share_id, args.path, destination,
                                       progress=progress)
        print(file=sys.stderr)
        info(f'已保存 {written} 字节 -> {destination}')
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    connection = _require_target(args)
    with connection:
        local = Path(args.file).resolve()
        if not local.is_file():
            die(f'本地文件不存在: {local}')
        written = client.push_file(connection, args.share_id, local,
                                   args.remote_path or local.name)
        info(f'已上传 {written} 字节 -> {args.share_id}:{args.remote_path or local.name}')
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    connection = _require_target(args)
    with connection:
        info(json.dumps(client.delete_remote(connection, args.share_id, args.path),
                        ensure_ascii=False))
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    registry = load_registry(root)
    if args.action == 'show':
        info(registry.describe())
        return 0
    connection = _require_target(args)
    with connection:
        if args.action == 'pull':
            remote = client.fetch_registry(connection)
            accepted = registry.merge(remote)
            save_registry(root, registry)
            info(f'拉取完成：对端 {len(remote)} 条，采纳 {accepted} 条，本地现有 {len(registry)} 条')
        elif args.action == 'push':
            result = client.push_registry(connection, registry)
            info(f'推送完成：对端采纳 {result.get("accepted")} 条，共 {result.get("total")} 条')
        else:
            die(f'未知的 registry 动作: {args.action}')
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """本机自检：不联网也能验证密码学、握手与名单状态机。"""
    from . import selftest
    return selftest.run(verbose=not args.quiet)


# ── 参数解析 ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='groupmesh', description='group-mesh 协议内核 MVP')
    parser.add_argument('--version', action='version', version=f'group-mesh MVP {KERNEL_VERSION}')
    subparsers = parser.add_subparsers(dest='command', required=True)

    def add_common(sub: argparse.ArgumentParser, with_target: bool = False) -> None:
        sub.add_argument('--dir', default=DEFAULT_IDENTITY, help='身份/状态目录（默认 ./identity）')
        sub.add_argument('--device', default=None, help='本机设备 ID（有多台设备时必填）')
        if with_target:
            sub.add_argument('--target', required=True, help='对端 host:port')
            sub.add_argument('--timeout', type=float, default=20.0, help='连接超时秒数')

    sub = subparsers.add_parser('init', help='新建主体与首台设备')
    add_common(sub)
    sub.add_argument('--name', required=True, help='主体名字')
    sub.add_argument('--device-name', default=None, help='设备名字（默认取主机名）')
    sub.add_argument('--force', action='store_true', help='（保留）显式确认，目前仍会拒绝覆盖')
    sub.set_defaults(func=cmd_init)

    sub = subparsers.add_parser('whoami', help='显示本机身份')
    add_common(sub)
    sub.set_defaults(func=cmd_whoami)

    sub = subparsers.add_parser('create', help='创建团体（成为群主）')
    add_common(sub)
    sub.add_argument('--group', required=True, help='团体标识')
    sub.add_argument('--ttl-days', type=int, default=180, help='名单有效期（天，仅提示）')
    sub.set_defaults(func=cmd_create)

    sub = subparsers.add_parser('join', help='用带外邀请串加入团体')
    add_common(sub)
    sub.add_argument('--invite', required=True, help='gm1: 开头的邀请串')
    sub.set_defaults(func=cmd_join)

    sub = subparsers.add_parser('roster', help='查看或修改团体名单')
    add_common(sub)
    sub.add_argument('action', choices=['show', 'add', 'remove', 'promote', 'demote', 'transfer'])
    sub.add_argument('--principal', help='目标主体公钥（base64）')
    # 注意：不能叫 --device，那个名字已被 add_common 用作"选本机哪台设备"
    sub.add_argument('--dev', dest='device_keys', action='append',
                     help='该主体的设备公钥（base64，可重复）')
    sub.add_argument('--name', help='成员显示名')
    sub.add_argument('--ttl-days', type=int, default=180)
    sub.set_defaults(func=cmd_roster)

    sub = subparsers.add_parser('share', help='管理本机共享项')
    add_common(sub)
    sub.add_argument('action', choices=['add', 'list', 'remove'])
    sub.add_argument('--share-id', help='共享标识（字母数字与 . _ -）')
    sub.add_argument('--path', help='本机目录')
    sub.add_argument('--read', default='group', help='read ACL：owner / group / 公钥')
    sub.add_argument('--write', default='owner', help='write ACL：owner / group / 公钥')
    sub.add_argument('--delete', default='owner', help='delete ACL：owner / group / 公钥')
    sub.add_argument('--max-bytes', type=int, default=1024 * 1024 * 1024,
                     help='容量上限字节数；0 表示不限制')
    sub.set_defaults(func=cmd_share)

    sub = subparsers.add_parser('serve', help='作为共享节点监听')
    add_common(sub)
    sub.add_argument('--bind', default='::', help='监听地址（默认 :: 即所有 IPv6/IPv4）')
    sub.add_argument('--port', type=int, default=19443, help='监听端口（0 表示自动分配）')
    sub.set_defaults(func=cmd_serve)

    sub = subparsers.add_parser('peers', help='列出对端可读的共享项')
    add_common(sub, with_target=True)
    sub.set_defaults(func=cmd_peers)

    sub = subparsers.add_parser('ls', help='列出对端目录')
    add_common(sub, with_target=True)
    sub.add_argument('--share-id', required=True)
    sub.add_argument('--path', default='.')
    sub.set_defaults(func=cmd_ls)

    sub = subparsers.add_parser('stat', help='查看对端文件信息')
    add_common(sub, with_target=True)
    sub.add_argument('--share-id', required=True)
    sub.add_argument('--path', required=True)
    sub.set_defaults(func=cmd_stat)

    sub = subparsers.add_parser('get', help='从对端取回文件')
    add_common(sub, with_target=True)
    sub.add_argument('--share-id', required=True)
    sub.add_argument('--path', required=True)
    sub.add_argument('--output', required=True)
    sub.set_defaults(func=cmd_get)

    sub = subparsers.add_parser('put', help='向对端上传文件')
    add_common(sub, with_target=True)
    sub.add_argument('--share-id', required=True)
    sub.add_argument('--file', required=True)
    sub.add_argument('--remote-path', default=None)
    sub.set_defaults(func=cmd_put)

    sub = subparsers.add_parser('rm', help='删除对端共享项里的文件')
    add_common(sub, with_target=True)
    sub.add_argument('--share-id', required=True)
    sub.add_argument('--path', required=True)
    sub.set_defaults(func=cmd_rm)

    sub = subparsers.add_parser('registry', help='注册表同步')
    add_common(sub, with_target=False)
    sub.add_argument('action', choices=['show', 'pull', 'push'])
    sub.add_argument('--target', default=None, help='对端 host:port（pull/push 需要）')
    sub.add_argument('--timeout', type=float, default=20.0)
    sub.set_defaults(func=cmd_registry)

    sub = subparsers.add_parser('selftest', help='本机自检')
    sub.add_argument('--quiet', action='store_true')
    sub.set_defaults(func=cmd_selftest)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RecordError as e:
        die(str(e))
    except RemoteError as e:
        die(f'对端返回错误: {e}')
    except TransportError as e:
        die(f'协议错误: {e}')
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
