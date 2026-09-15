"""group-mesh 插件后端：把协议内核包成 Shell 插件。

## 分层

    shell/backend/...           壳（Flask 路由、令牌、设置存储）
    shell/groupmesh/            协议内核（身份/名单/Noise/传输）
        └── plugins/group-mesh  本文件：插件 API 与后台节点生命周期

内核是**壳层的一部分**（`shell/groupmesh`），与 `shell/backend`、`shell/frontend`
并列。它不 import Flask / pywebview，因此可以脱离 GUI 独立运行与跨机联调
（`python -m shell.groupmesh.cli selftest`）；但它的归属是壳，不是某个插件 ——
将来文件传输、游戏联机、语音这些 Companion 插件都要通过
`PluginBase.get_dependency('group-mesh')` 复用它，把内核塞进某个插件的私有目录
会让它们依赖另一个插件的内部结构。

## 当前状态（v0.1，骨架）

已接通：身份初始化、团体创建/加入、名单查看与修改、共享项管理、节点启停与状态。
**尚未接通**：Android 轻客户端、SoftEther 游戏面、语音、回收站、内容寻址分块。

## 未决的壳侧缺口（设计文档 §12）

设计文档 §12 第 1/2 项要求"凭据到主体的映射"与"插件以受信方式读取主体"。
壳目前**没有**这个能力（`shell/backend/auth.py` 只有单一令牌），因此本插件
所有涉及主体的判定都以**本机主体**为准（本机就是这个团体的一个节点），
不接收请求参数里的身份。等壳侧补上主体上下文后，这里应改为：
    `self.current_principal()` → 由 Shell 注入的 ContextVar 读取
而不是现在的"本机身份即操作者"。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List, Optional

import shell.groupmesh as kernel
from shell.backend.plugin_base import PluginBase
from shell.groupmesh import __version__ as KERNEL_VERSION
from shell.groupmesh.identity import Identity
from shell.groupmesh.node import serve
from shell.groupmesh.records import RecordError
from shell.groupmesh.roster import Roster, RosterEntry, founding_roster, next_roster, staleness_report
from shell.groupmesh.shares import Acl, LocalShare, new_share

log = logging.getLogger(__name__)

# 默认监听端口。与设计文档 §13 的默认参数一致，可被设置项覆盖。
DEFAULT_PORT = 19443


def _opts(value: Any) -> Dict[str, Any]:
    """把"结构化参数"归一成字典。

    `/api/<plugin>__<method>` 会把 payload 的 `args`/`kwargs` 原样展开成位置/关键字
    参数（`shell/backend/file_server.py`），因此前端既可以传一个对象、也可以逐个传。
    两种形态都接受，避免以后前端或桥接实现改动时**参数静默错位**
    （例如 delete 的值跑进 max_bytes，只在运行时才炸）。
    """
    return value if isinstance(value, dict) else {}


class GroupMeshPlugin(PluginBase):
    """团体组网插件。

    数据布局（全部在 `get_data_root()` 之下，与插件设置文件分离）：

        <data_root>/group-mesh/
          identity/        主体与设备私钥（受保护路径，见 get_protected_paths）
          identity/roster.json    当前团体名单
          identity/shares.json   本机共享项声明
          identity/registry.json 注册表
    """

    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        {'key': 'port', 'label': '监听端口', 'type': 'number',
         'default': DEFAULT_PORT, 'min': 1024, 'max': 65535,
         'help': '共享节点对外提供服务的 TCP 端口。修改后需要重启节点。'},
        {'key': 'bind', 'label': '监听地址', 'type': 'text', 'default': '::',
         'help': '默认 :: 同时接受 IPv6 与 IPv4。设计文档以公网 IPv6 为目标，'
                 '只有单个 IPv6 地址时填写该地址。'},
        {'key': 'group_name', 'label': '团体名称', 'type': 'text', 'default': '',
         'help': '创建团体时使用的标识。已加入团体后修改此项不会改名。'},
        {'key': 'principal_name', 'label': '本机主体名称', 'type': 'text', 'default': '',
         'help': '界面显示用的名字。留空时取主机名。'},
        {'key': 'ttl_days', 'label': '名单有效期（天）', 'type': 'number',
         'default': 180, 'min': 1, 'max': 3650,
         'help': '仅作提示（设计文档 §5.4）：到期不阻断通信，界面会标注已过期。'},
    ]

    def __init__(self, manifest: dict, config: dict) -> None:
        super().__init__(manifest, config)
        self._lock = threading.Lock()
        self._node_thread: Optional[threading.Thread] = None
        self._node_stop = threading.Event()
        self._listener: Optional[socket.socket] = None
        self._listening: Optional[tuple] = None
        self._last_error: Optional[str] = None
        self._connection_count = 0

    # ── 路径 ──────────────────────────────────────────────────────────────

    def get_data_root(self) -> Path:
        """本插件的数据目录：`<全局数据根>/group-mesh`。

        不与全局数据根混用：那里还可能放媒体等内容，混在一起会让
        `get_protected_paths()` 的申报边界变得含糊。
        """
        return Path(self.config['directories']['data_root']).resolve() / self.name

    @property
    def identity_dir(self) -> Path:
        return self.get_data_root() / 'identity'

    def get_protected_paths(self) -> List[Path]:
        """身份目录整个申报为受保护：里面是主体的长期私钥与设备私钥。

        设计文档 §4.1 要求"设备私钥不导出"；MVP 阶段私钥是明文落盘的，
        因此**至少**要保证它不会经 `/file`、`/files`、`/thumbs` 被端出去。
        """
        return [*super().get_protected_paths(), self.identity_dir]

    # ── 内核访问 ──────────────────────────────────────────────────────────
    #
    # 内核是 `shell/groupmesh`，一个普通包，**import 层面直接可用**：
    # 既不需要往 sys.path 里塞路径，也不需要"找不到内核时降级"这种分支
    # （打包时由 HIDDEN_IMPORTS 保证它随包分发，缺失会在 check_packaging 阶段暴露）。

    def _load_identity(self) -> Optional[Identity]:
        """读取本机身份；尚未初始化时返回 None。"""
        if not (self.identity_dir / 'principal.json').is_file():
            return None
        try:
            return Identity.load(self.identity_dir)
        except RecordError as e:
            log.warning(f'[group-mesh] 身份读取失败: {e}')
            return None

    def _load_roster(self) -> Optional[Roster]:
        path = self.identity_dir / 'roster.json'
        if not path.is_file():
            return None
        return Roster.from_dict(json.loads(path.read_text(encoding='utf-8')))

    def _load_shares(self) -> Dict[str, LocalShare]:
        path = self.identity_dir / 'shares.json'
        if not path.is_file():
            return {}
        shares: Dict[str, LocalShare] = {}
        for share_id, item in (json.loads(path.read_text(encoding='utf-8')).get('shares') or {}).items():
            try:
                shares[share_id] = LocalShare.from_dict(item)
            except RecordError as e:
                log.warning(f'[group-mesh] 跳过损坏的共享项 {share_id}: {e}')
        return shares

    def _save_shares(self, shares: Dict[str, LocalShare]) -> None:
        self.identity_dir.mkdir(parents=True, exist_ok=True)
        payload = {'shares': {sid: share.to_dict() for sid, share in shares.items()}}
        (self.identity_dir / 'shares.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    # ── 状态 ──────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """界面首屏用：内核版本、身份/团体/共享项/节点的当前状态。"""
        status: Dict[str, Any] = {
            'kernel': {
                'available': True,
                'version': KERNEL_VERSION,
                'package': 'shell.groupmesh',
                'path': str(Path(kernel.__file__ or '').parent),
            },
            'data_root': str(self.get_data_root()),
            'identity_dir': str(self.identity_dir),
            'settings': {
                # 这三项都是**前端会读**的：弹窗预填团体名/主体名、端口与绑定地址展示。
                # 少给一个键的后果不是"显示为空"，而是前端读到 undefined ——
                # 曾经因为漏了 group_name，前端把它当"已有值"而走进原生 confirm
                # 分支，点击"创建团体"直接没有反应。
                'port': self.setting('port', DEFAULT_PORT),
                'bind': self.setting('bind', '::'),
                'group_name': self.setting('group_name', '') or '',
                'principal_name': self.setting('principal_name', '') or '',
            },
            'node': self.get_node_status(),
            'identity': None,
            'roster': None,
            'shares': [],
            'unsupported': [
                'Android 轻客户端（设计文档 §11.2，首版不实现）',
                'SoftEther 游戏面（§9）',
                '语音（§8.3）',
                '应用级回收站（§6.4）',
                '内容寻址分块传输（§10）',
                '壳侧主体上下文（§12 第 1/2 项，壳尚未提供）',
            ],
        }

        identity = self._load_identity()
        if identity is not None:
            roster = self._load_roster()
            status['identity'] = {
                'principal_name': identity.principal.name,
                'principal_id': identity.principal.id,
                'principal_key': identity.principal.public_key.hex(),
                'device_name': identity.device.name,
                'device_id': identity.device.id,
                'device_key': identity.device.public_key.hex(),
            }
            if roster is not None:
                role = roster.role_of(identity.principal.public_key)
                entry = roster.entry_of(identity.principal.public_key)
                status['roster'] = {
                    'group': roster.group,
                    'version': roster.version,
                    'role': role,
                    'in_roster': entry is not None,
                    'member_count': len(roster.members),
                    'admin_count': len(roster.admin_keys),
                    'stale': staleness_report(roster, None),
                    'members': [
                        {'name': m.name, 'principal_id': m.id,
                         'device_count': len(m.device_keys)}
                        for m in roster.members
                    ],
                }
            status['shares'] = [
                {'share_id': sid, 'path': share.path,
                 'acl': share.declaration.acl.to_dict(),
                 'max_bytes': share.max_bytes}
                for sid, share in self._load_shares().items()
            ]
        return status

    # ── 身份与团体 ────────────────────────────────────────────────────────

    # 下面这些公开 API 统一接受 `(opts: dict)` 或逐个位置参数两种调用形态，
    # 见 `_opts()` 的说明。

    def init_identity(self, opts: Any = None, name: str = '') -> Dict[str, Any]:
        """创建本机身份（主体 + 首台设备）。身份已存在时拒绝覆盖。"""
        options = _opts(opts)
        principal_name = (options.get('name') or name or self.setting('principal_name') or
                          socket.gethostname() or 'omnibox')
        try:
            identity = Identity.init(self.identity_dir, principal_name, socket.gethostname())
        except RecordError as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'identity': {'name': identity.principal.name,
                                              'id': identity.principal.id,
                                              'device_id': identity.device.id}}

    def create_group(self, opts: Any = None, group: str = '') -> Dict[str, Any]:
        """创建团体：调用方成为群主，创始名单里只有自己的一台设备。"""
        import json
        options = _opts(opts)
        identity = self._load_identity()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份，请先初始化身份'}
        if self._load_roster() is not None:
            return {'success': False,
                    'error': '本机已有团体名单。删除 roster.json 才能重建（会丢掉当前名单链）'}
        group_name = options.get('group') or group or self.setting('group_name') or 'omnibox-group'
        try:
            roster = founding_roster(group_name, identity.principal,
                                     [identity.device.public_key],
                                     ttl_seconds=int(self.setting('ttl_days', 180)) * 86400)
        except RecordError as e:
            return {'success': False, 'error': str(e)}

        self.identity_dir.mkdir(parents=True, exist_ok=True)
        (self.identity_dir / 'roster.json').write_text(
            json.dumps(roster.to_dict(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return {'success': True, 'group': roster.group, 'version': roster.version,
                'invite': self._invite_string(roster)}

    def join_group(self, opts: Any = None, invite: str = '') -> Dict[str, Any]:
        """用带外收到的邀请串加入团体，**或把本机名单更新到更高版本**。

        两种情形走同一条路径，因为验证规则是同一套（§5.2）：

        * 本机没有名单 → 规则 6：直接信任带外分发的创始名单；
        * 本机已有名单 → 规则 1/2 要求 `version` 前进且 `prev` 指向当前名单哈希，
          因此**只有直接后继会被接受**（乱序或伪造的名单会被拒）。

        为什么必须有"更新"这条路：群主加人后签发的是新名单，而名单靠**带外分发**
        （邀请串）。如果成员端只有一个"加入"入口、加入后再无入口，成员就会永远停在
        旧名单上 —— 表现是"我已被加进名单却连不上任何人"，且各方看到的成员表不一致。
        """
        import base64
        import json as jsonlib

        options = _opts(opts)
        if self._load_identity() is None:
            return {'success': False, 'error': '尚未创建身份，请先初始化身份'}
        text = str(options.get('invite') or invite or '').strip()
        if not text.startswith('gm1:'):
            return {'success': False, 'error': '邀请串必须以 gm1: 开头'}
        try:
            payload = base64.urlsafe_b64decode(text[4:].encode('ascii'))
            roster = Roster.from_dict(jsonlib.loads(payload.decode('utf-8')))
        except Exception as e:
            return {'success': False, 'error': f'邀请串解析失败: {e}'}

        current = self._load_roster()
        try:
            roster.accepts(current)
        except RecordError as e:
            hint = ''
            if current is not None and roster.version <= current.version:
                hint = f'（本机已是 v{current.version}，拿到的邀请串是 v{roster.version}）'
            return {'success': False, 'error': f'名单未被接受: {e}{hint}'}

        previous_version = current.version if current is not None else None
        self.identity_dir.mkdir(parents=True, exist_ok=True)
        (self.identity_dir / 'roster.json').write_text(
            jsonlib.dumps(roster.to_dict(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        identity = self._load_identity()
        entry = roster.entry_of(identity.principal.public_key)
        return {'success': True,
                'updated': previous_version is not None,
                'previous_version': previous_version,
                'group': roster.group,
                'version': roster.version,
                'role': roster.role_of(identity.principal.public_key),
                'in_roster': entry is not None,
                'warning': None if entry is not None else
                           '本主体还不在名单里，其他成员不会接受你的连接。'
                           '请把设备公钥交给群主执行 roster add。'}

    def _invite_string(self, roster) -> str:
        import base64
        import json as jsonlib

        raw = jsonlib.dumps(roster.to_dict(), separators=(',', ':'), sort_keys=True).encode('utf-8')
        return 'gm1:' + base64.urlsafe_b64encode(raw).decode('ascii')

    def get_invite(self) -> Dict[str, Any]:
        """导出当前名单的邀请串（群主分发给成员用）。"""
        roster = self._load_roster()
        if roster is None:
            return {'success': False, 'error': '本机没有团体名单'}
        return {'success': True, 'invite': self._invite_string(roster),
                'group': roster.group, 'version': roster.version}

    def get_device_keys(self) -> Dict[str, Any]:
        """给群主登记用的本机公钥（主体 + 设备）。"""
        identity = self._load_identity()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份'}
        import base64
        return {'success': True,
                'principal': base64.b64encode(identity.principal.public_key).decode('ascii'),
                'device': base64.b64encode(identity.device.public_key).decode('ascii'),
                'name': identity.principal.name}

    def add_member(self, opts: Any = None, principal: str = '', device: str = '',
                   name: str = '') -> Dict[str, Any]:
        """群主/管理员把一名主体（及其设备）加入名单。

        权限由内核的 `Roster.accepts()` 在签名后复核：管理员改群主或改管理员
        集合都会被拒绝，因此这里不需要（也不应该）在插件层重写一套角色判断。
        """
        import base64
        import json as jsonlib
        options = _opts(opts)
        principal = str(options.get('principal') or principal or '')
        device = str(options.get('device') or device or '')
        name = str(options.get('name') or name or '')
        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}

        def decode(value: str, field: str) -> bytes:
            raw = base64.b64decode((value or '').strip(), validate=True)
            if len(raw) != 32:
                raise ValueError(f'{field} 必须是 32 字节公钥的 base64')
            return raw

        try:
            principal_key = decode(principal, '主体公钥')
            device_keys = [decode(device, '设备公钥')] if device.strip() else []
        except Exception as e:
            return {'success': False, 'error': f'公钥解析失败: {e}'}

        members = [RosterEntry(m.name, m.principal_key, list(m.device_keys))
                   for m in roster.members]
        existing = next((m for m in members if m.principal_key == principal_key), None)
        if existing is not None:
            added = [k for k in device_keys if k not in existing.device_keys]
            if not added:
                return {'success': False, 'error': '该主体与其设备都已在名单里'}
            existing.device_keys.extend(added)
            action = f'补登记 {len(added)} 台设备'
        else:
            members.append(RosterEntry(name or principal_key[:8].hex(), principal_key, device_keys))
            action = '新增成员'

        candidate = next_roster(roster, group=roster.group, owner_key=roster.owner_key,
                                members=members, admin_keys=list(roster.admin_keys),
                                ttl_seconds=int(self.setting('ttl_days', 180)) * 86400)
        candidate.sign(identity.principal.private_key)
        try:
            candidate.accepts(roster)
        except RecordError as e:
            return {'success': False, 'error': f'你的角色无权做这次改动: {e}'}

        (self.identity_dir / 'roster.json').write_text(
            jsonlib.dumps(candidate.to_dict(), ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
        return {'success': True, 'action': action, 'version': candidate.version,
                'invite': self._invite_string(candidate)}

    # ── 共享项 ────────────────────────────────────────────────────────────

    def add_share(self, opts: Any = None, share_id: str = '', path: str = '',
                  read: str = 'group', write: str = 'owner', delete: str = 'owner',
                  max_bytes: int = 1024 * 1024 * 1024) -> Dict[str, Any]:
        """挂载一个本机共享项。

        §6.5 的三条硬约束在插件层就挡掉，不推给用户自觉：
          * 共享根不得落在程序数据目录 / 身份目录之内（避免把私钥或程序本身共享出去）；
          * 共享根必须是已存在的目录；
          * 授予写权限等于允许对方占用磁盘，因此容量上限显式可见（0 表示不限制）。
        """
        options = _opts(opts)
        share_id = str(options.get('share_id') or share_id or '')
        path = str(options.get('path') or path or '')
        read = str(options.get('read') or read or 'group')
        write = str(options.get('write') or write or 'owner')
        delete = str(options.get('delete') or delete or 'owner')
        if 'max_bytes' in options:
            max_bytes = options['max_bytes']

        identity = self._load_identity()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份'}

        target = Path(path).expanduser().resolve()
        if not target.is_dir():
            return {'success': False, 'error': f'共享根必须是已存在的目录: {target}'}

        forbidden = [Path(self.config['directories']['data_root']).resolve(), self.identity_dir]
        for guarded in forbidden:
            try:
                target.relative_to(guarded)
            except ValueError:
                continue
            return {'success': False,
                    'error': f'共享根不得位于 {guarded} 之内（那里是程序数据与身份私钥）'}

        try:
            limit = None if int(max_bytes) == 0 else int(max_bytes)
        except (TypeError, ValueError):
            return {'success': False, 'error': f'max_bytes 必须是整数或 0，收到 {max_bytes!r}'}

        try:
            acl = Acl(read=read, write=write, delete=delete)
            share = new_share(share_id=share_id, path=str(target),
                              owner_key=identity.principal.public_key,
                              node_key=identity.device.public_key,
                              node_private_key=identity.device.private_key,
                              acl=acl, seq=1, max_bytes=limit)
        except (RecordError, ValueError) as e:
            return {'success': False, 'error': str(e)}

        with self._lock:
            shares = self._load_shares()
            shares[share_id] = share
            self._save_shares(shares)
        return {'success': True, 'share_id': share_id, 'path': str(target),
                'acl': acl.to_dict()}

    def remove_share(self, opts: Any = None, share_id: str = '') -> Dict[str, Any]:
        """取消一个本机共享项（只删声明，不碰共享根里的文件）。"""
        options = _opts(opts)
        share_id = str(options.get('share_id') or share_id or '')
        with self._lock:
            shares = self._load_shares()
            if share_id not in shares:
                return {'success': False, 'error': f'没有共享项 {share_id}'}
            del shares[share_id]
            self._save_shares(shares)
        return {'success': True, 'share_id': share_id}

    # ── 节点 ──────────────────────────────────────────────────────────────

    def start_node(self) -> Dict[str, Any]:
        """在后台线程启动共享节点监听。

        设计文档 §7.4：Windows 防火墙默认阻止入站，需要一次性添加规则；
        这里只回报失败原因，不代替用户提权。
        """
        if self._node_thread is not None and self._node_thread.is_alive():
            return {'success': False, 'error': '节点已在运行', 'node': self.get_node_status()}

        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份'}
        if roster is None:
            return {'success': False, 'error': '尚未加入任何团体（节点无法认证对端）'}

        bind = str(self.setting('bind', '::') or '::')
        port = int(self.setting('port', DEFAULT_PORT) or DEFAULT_PORT)
        shares = self._load_shares()
        self._node_stop.clear()
        self._last_error = None
        ready = threading.Event()

        def run() -> None:
            def on_ready(listener: socket.socket) -> None:
                self._listener = listener
                self._listening = listener.getsockname()[:2]
                ready.set()

            try:
                # roster_loader：每次建连重读名单。群主在别处加了成员后，
                # 运行中的节点必须立刻认，而不是要求用户重启插件。
                serve(bind, port, identity, roster, shares=shares,
                      roster_loader=self._load_roster, ready=on_ready)
            except OSError as e:
                self._last_error = (f'无法监听 {bind}:{port} —— {e}。'
                                    'Windows 上入站连接默认被防火墙拦截，'
                                    '需要为监听端口添加一次允许规则（需管理员权限）。')
                log.warning(f'[group-mesh] {self._last_error}')
            except Exception as e:
                self._last_error = f'节点异常退出: {type(e).__name__}: {e}'
                log.error(f'[group-mesh] {self._last_error}')
            finally:
                self._listener = None
                self._listening = None
                ready.set()

        self._node_thread = threading.Thread(target=run, name='group-mesh-node', daemon=True)
        self._node_thread.start()
        ready.wait(timeout=8)
        status = self.get_node_status()
        if status['running']:
            return {'success': True, 'node': status}
        return {'success': False, 'error': status.get('error') or '节点启动失败', 'node': status}

    def stop_node(self) -> Dict[str, Any]:
        """停止监听：关掉监听套接字，accept 循环随之退出。"""
        self._node_stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._node_thread
        if thread is not None:
            thread.join(timeout=5)
        self._node_thread = None
        return {'success': True, 'node': self.get_node_status()}

    def get_node_status(self) -> Dict[str, Any]:
        thread = self._node_thread
        running = bool(thread is not None and thread.is_alive())
        return {
            'running': running,
            'listening': f'{self._listening[0]}:{self._listening[1]}' if self._listening else None,
            'connection_count': self._connection_count,
            'error': self._last_error,
        }

    # ── 生命周期与 API ────────────────────────────────────────────────────

    def on_load(self) -> None:
        self.get_data_root().mkdir(parents=True, exist_ok=True)

    def on_unload(self) -> None:
        # 插件卸载必须留不下监听线程，否则重载会撞端口占用
        self.stop_node()

    def register_api(self) -> Dict[str, Callable]:
        return {
            'get_status': self.get_status,
            'init_identity': self.init_identity,
            'get_device_keys': self.get_device_keys,
            'create_group': self.create_group,
            'join_group': self.join_group,
            'get_invite': self.get_invite,
            'add_member': self.add_member,
            'add_share': self.add_share,
            'remove_share': self.remove_share,
            'start_node': self.start_node,
            'stop_node': self.stop_node,
            'get_node_status': self.get_node_status,
        }
