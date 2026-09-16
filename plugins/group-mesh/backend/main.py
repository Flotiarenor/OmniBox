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
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, Iterable, List, Optional, Tuple

import shell.groupmesh as kernel
from shell.backend.plugin_base import PluginBase
from shell.groupmesh import __version__ as KERNEL_VERSION
from shell.groupmesh import client as mesh_client
from shell.groupmesh import registry as registry_mod
from shell.groupmesh.identity import Identity
from shell.groupmesh.node import serve
from shell.groupmesh.records import RecordError
from shell.groupmesh.roster import Roster, RosterEntry, founding_roster, next_roster, staleness_report
from shell.groupmesh.shares import Acl, LocalShare, new_share
from shell.groupmesh.transport import RemoteError, TransportError

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
          identity/               主体与设备私钥（受保护路径，见 get_protected_paths）
          identity/roster.json    当前团体名单
          identity/shares.json    本机共享项声明（协议对象，可发给对端）
          identity/share_roots.json  共享项的本地位置（本机事实，从不发出）
          identity/registry.json  注册表
          downloads/              从对端取回的文件（可用 download_dir 改到别处）
          .cache/                 可重建的派生数据（目录快照、连接待用）

    ## 四种"位置"（分工见设计文档 §6.5）

    | 位置 | 谁能读 | 性质 |
    | --- | --- | --- |
    | `identity/` | 只有本插件 | 私钥与名单，已申报受保护 |
    | 共享根（可多个） | 团体成员（经协议） | 对外提供的内容 |
    | `downloads/` | `/file?plugin=group-mesh` | 收下来的远端文件 |
    | `.cache/` | 不对外 | 目录快照等可重建数据 |

    **为什么共享根要有自己的文件**：`shares.json` 里的 `declaration` 是签名过的
    协议对象（要发给对端、多一个本机字段签名就变），而路径、容量上限、目录是否
    还在盘上都是本机事实。混在一坨里会让"位置状态"污染协议对象。因此这里把
    位置单独存 `share_roots.json`，读回时再与声明合成 `LocalShare`。
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
        # 位置之四：从对端取回的文件落点。留空时回落到 <数据根>/group-mesh/downloads。
        # 由 Shell 的 FolderPicker 渲染（`directory` 类型），值仍是字符串，
        # 因此这里不需要为它写任何解析代码。
        {'key': 'download_dir', 'label': '远端下载目录', 'type': 'directory',
         'default': '', 'placeholder': '默认：数据根/group-mesh/downloads',
         'help': '从团体成员那里取回的文件保存在这里。该目录经 /file 对界面可读，'
                 '但不对团体共享 —— 要共享它请单独挂一个共享项。'},
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
        # 注册表（§7）：由 start_node() 载入并交给内核，用于向对端发布本机端点与
        # 共享清单，以及学习其他设备的端点。None = 本节点未启用注册表。
        self._registry = None
        # 本次运行发布的注册记录序号（None = 还没发布成功）。界面据此显示
        # "本机是否已对其他设备可见" —— 发布失败时节点照常能收发，但别人发现不了。
        self._published_seq: Optional[int] = None
        # 自动启动节点的状态：只尝试一次，失败原因留给界面显示（见 _auto_start_node）。
        self._auto_start_attempted = False
        self._auto_start_error: Optional[str] = None
        # 已建立的出站连接：(设备公钥 bytes, host, port) -> Connection。
        # 复用的理由：一次请求就是一次 Noise 握手（多个往返 + 公钥运算），
        # 逐次建连会让"浏览一个目录"变成几个握手的开销。
        self._connections: Dict[Tuple[bytes, str, int], Any] = {}

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

    @property
    def cache_dir(self) -> Path:
        """可重建的派生数据（目录快照、连接待用）。**不是**缩略图目录。"""
        return self.get_data_root() / '.cache'

    @property
    def downloads_dir(self) -> Path:
        """从对端取回的文件落点：设置项 `download_dir`，留空时用数据根下的 downloads。

        刻意与 `.cache` 分开：`.cache` 可以随时删（删了只是重新拉），而下载目录里
        放的是用户明确要回来的文件，删掉就是数据丢失。
        """
        configured = str(self.setting('download_dir', '') or '').strip()
        if configured:
            return Path(configured).expanduser().resolve()
        return self.get_data_root() / 'downloads'

    @property
    def remote_cache_dir(self) -> Path:
        """远端内容的本地物化根：`<cache>/remote/<设备ID>/<共享标识>/<相对路径>`。

        目录结构与文件字节分开落地：目录树来自 `list_directory`（很轻），
        文件字节按需取回（`download_remote`），因此不会为了"看一眼"而整份同步。
        """
        return self.cache_dir / 'remote'

    def _share_roots_file(self) -> Path:
        return self.identity_dir / 'share_roots.json'

    def _load_share_roots(self) -> Dict[str, Dict[str, Any]]:
        """读共享项的本地位置表（本机事实，从不发给对端）。

        读不到 / 损坏时返回空表而不是抛异常：位置表丢了只会让共享项不可用，
        而抛异常会让整个 `get_status()` 失败 —— 界面连"哪里坏了"都显示不出来。
        """
        path = self._share_roots_file()
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            log.warning('[group-mesh] share_roots.json 无法解析，按空位置表处理')
            return {}
        roots = payload.get('roots') if isinstance(payload, dict) else None
        return roots if isinstance(roots, dict) else {}

    def _save_share_roots(self, roots: Dict[str, Dict[str, Any]]) -> None:
        self.identity_dir.mkdir(parents=True, exist_ok=True)
        (self.identity_dir / 'share_roots.json').write_text(
            json.dumps({'roots': roots}, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')

    def _root_state(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """一个共享根的可用性。只用 `is_dir()`，不遍历目录。

        容量统计（`_tree_usage`）刻意不在这里做：`G:\\图库` 那种目录走一遍可能要
        几十秒，而 `get_status()` 是首屏调用。界面要精确用量时调 `refresh_share_roots`。
        """
        raw = str(entry.get('path') or '')
        state: Dict[str, Any] = {
            'path': raw,
            'available': False,
            'max_bytes': entry.get('max_bytes'),
            'reason': None,
        }
        if not raw:
            state['reason'] = '未设置路径'
            return state
        try:
            target = Path(raw).expanduser()
            state['available'] = target.is_dir()
            if not state['available']:
                state['reason'] = '目录不存在或所在磁盘未接入'
        except OSError as e:
            state['reason'] = f'无法访问: {e}'
        return state

    def get_share_roots(self) -> List[Dict[str, Any]]:
        """本机全部共享根及其状态（界面的"共享项"页用）。"""
        roots = self._load_share_roots()
        shares = self._load_shares()
        result: List[Dict[str, Any]] = []
        for share_id, share in shares.items():
            entry = roots.get(share_id) or {'path': share.path,
                                            'max_bytes': share.max_bytes}
            result.append({'share_id': share_id,
                           'acl': share.declaration.acl.to_dict(),
                           **self._root_state(entry)})
        return result

    @staticmethod
    def _tree_usage(root: Path, limit: Optional[int] = None,
                    max_entries: int = 200_000) -> Tuple[int, bool]:
        """目录树总字节数。`limit` 非空时与它取小 —— 只关心"有没有超配额"。

        返回 `(字节数, 是否被上限截断)`：截断表示这个数字是下界而不是精确值，
        界面必须据此显示"≥"，否则用户会以为配额算错了。
        """
        total = 0
        seen = 0
        for base, _dirs, files in os.walk(root):
            for name in files:
                seen += 1
                if seen > max_entries:
                    return total, True
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    continue
                if limit is not None and total >= limit:
                    return total, False
        return total, False

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
        """把「声明的协议对象」与「本机的路径/配额」合成 `LocalShare`。

        位置以 `share_roots.json` 为准；该文件里没有条目时回落到 `shares.json`
        自带的 `path`（迁移前的布局），因此升级不需要跑任何脚本 —— 第一次
        `add_share` / `remove_share` 会把位置表补齐。
        """
        path = self.identity_dir / 'shares.json'
        if not path.is_file():
            return {}
        roots = self._load_share_roots()
        shares: Dict[str, LocalShare] = {}
        for share_id, item in (json.loads(path.read_text(encoding='utf-8')).get('shares') or {}).items():
            try:
                share = LocalShare.from_dict(item)
            except RecordError as e:
                log.warning(f'[group-mesh] 跳过损坏的共享项 {share_id}: {e}')
                continue
            entry = roots.get(share_id)
            if isinstance(entry, dict) and entry.get('path'):
                share.path = str(entry['path'])
                if 'max_bytes' in entry:
                    share.max_bytes = entry['max_bytes']
            shares[share_id] = share
        return shares

    def _save_shares(self, shares: Dict[str, LocalShare]) -> None:
        """声明写 `shares.json`，位置写 `share_roots.json`（两类数据分开存）。"""
        self.identity_dir.mkdir(parents=True, exist_ok=True)
        payload = {'shares': {sid: share.to_dict() for sid, share in shares.items()}}
        (self.identity_dir / 'shares.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self._save_share_roots({sid: {'path': share.path, 'max_bytes': share.max_bytes}
                                for sid, share in shares.items()})

    # ── 状态 ──────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """界面首屏用：内核版本、身份/团体/共享项/节点的当前状态。"""
        # 先读身份与名单并（必要时）启动共享节点，**再**组装返回的 status：
        # `status['node']` 是这一刻的快照，若先组装再启动，首次调用会报告"未运行"，
        # 而节点其实已经起来了 —— 界面因此要刷新两次才显示正常。
        identity = self._load_identity()
        roster = self._load_roster()
        # 有身份与团体就把节点跑起来（只尝试一次）：节点不跑 → 不发布注册记录
        # → 别人发现不了本机。这里正是"用户打开插件"的时刻，而自动启动是幂等的。
        self._auto_start_node(identity is not None, roster is not None)

        status: Dict[str, Any] = {
            'kernel': {
                'available': True,
                'version': KERNEL_VERSION,
                'package': 'shell.groupmesh',
                'path': str(Path(kernel.__file__ or '').parent),
            },
            'data_root': str(self.get_data_root()),
            'identity_dir': str(self.identity_dir),
            # 四种位置一览：界面直接展示，用户不必猜文件放哪了。
            'locations': {
                'identity': str(self.identity_dir),
                'downloads': str(self.downloads_dir),
                'downloads_custom': bool(str(self.setting('download_dir', '') or '').strip()),
                'cache': str(self.cache_dir),
                'remote_cache': str(self.remote_cache_dir),
            },
            'settings': {
                # 这几项都是**前端会读**的：弹窗预填团体名/主体名、端口与绑定地址展示。
                # 少给一个键的后果不是"显示为空"，而是前端读到 undefined ——
                # 曾经因为漏了 group_name，前端把它当"已有值"而走进原生 confirm
                # 分支，点击"创建团体"直接没有反应。
                'port': self.setting('port', DEFAULT_PORT),
                'bind': self.setting('bind', '::'),
                'group_name': self.setting('group_name', '') or '',
                'principal_name': self.setting('principal_name', '') or '',
                'download_dir': self.setting('download_dir', '') or '',
            },
            'node': self.get_node_status(),
            'identity': None,
            'roster': None,
            'shares': [],
            'share_roots': [],
            'unsupported': [
                'Android 轻客户端（设计文档 §11.2，首版不实现）',
                'SoftEther 游戏面（§9）',
                '语音（§8.3）',
                '应用级回收站（§6.4）',
                '内容寻址分块传输（§10）',
                '壳侧主体上下文（§12 第 1/2 项，壳尚未提供）',
            ],
        }

        if identity is not None:
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
            status['share_roots'] = self.get_share_roots()
            # 兼容字段：旧的界面按 `shares` 渲染表格（含 path / acl / max_bytes），
            # 保留它可以让前端分两步迁移，而不是一次改动同时打断后端与界面。
            status['shares'] = [
                {'share_id': root['share_id'], 'path': root['path'], 'acl': root['acl'],
                 'max_bytes': root['max_bytes'], 'available': root['available'],
                 'reason': root['reason']}
                for root in status['share_roots']
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

    def refresh_share_roots(self) -> Dict[str, Any]:
        """重扫本机共享根：更新可用性，并给出条目数与用量。

        只有用户点"刷新"时才走这里（目录树扫描可能几十秒），`get_status()` 永远
        是轻量的。用量在超过容量上限时提前停止累加，并在 `truncated` 里说明这个
        数字是下界还是精确值 —— 否则界面会把"扫到一半就够配额了"显示成精确用量。
        """
        result: List[Dict[str, Any]] = []
        roots = self._load_share_roots()
        for share_id, share in self._load_shares().items():
            entry = roots.get(share_id) or {'path': share.path, 'max_bytes': share.max_bytes}
            state = self._root_state(entry)
            item = {'share_id': share_id, **state}
            if state['available']:
                limit = state['max_bytes']
                try:
                    used, truncated = self._tree_usage(Path(state['path']).expanduser(),
                                                       limit=limit)
                except OSError as e:
                    item['available'] = False
                    item['reason'] = f'扫描失败: {e}'
                    result.append(item)
                    continue
                item['used_bytes'] = used
                item['truncated'] = truncated
                item['entries'] = self._count_entries(Path(state['path']).expanduser())
            result.append(item)
        return {'success': True, 'roots': result,
                'scanned_at': int(time.time())}

    @staticmethod
    def _count_entries(root: Path, max_entries: int = 200_000) -> int:
        """目录树里的条目数（文件 + 目录），用于"这个共享项有多大"的粗略提示。"""
        count = 0
        for _base, dirs, files in os.walk(root):
            count += len(dirs) + len(files)
            if count > max_entries:
                return max_entries
        return count

    # ── 远端：发现与浏览 ──────────────────────────────────────────────────
    #
    # 这一节把内核 `shell/groupmesh/client.py` 已经具备的能力（列共享、列目录、
    # 取文件）接到插件上。在此之前这些能力只有 CLI 有，所以"协议面通了、界面面
    # 为零"：用户在插件里既看不到对端设备，也拉不回一个文件。

    def _device_names(self, roster: Optional[Roster]) -> Dict[bytes, str]:
        """设备公钥 -> 显示名（取它所属主体的名字）。"""
        names: Dict[bytes, str] = {}
        for member in (roster.members if roster else []):
            for device in member.device_keys:
                names[device] = member.name
        return names

    @staticmethod
    def _pick_endpoint(endpoints: Iterable[Any]) -> Optional[Tuple[str, int]]:
        """从注册记录的端点里挑一个可用的。优先 IPv6 —— 那是设计文档的主路径。"""
        valid: List[Tuple[str, int]] = []
        for item in endpoints or []:
            try:
                address, port = item[0], int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if address and 1 <= port <= 65535:
                valid.append((str(address), port))
        if not valid:
            return None
        valid.sort(key=lambda pair: 0 if ':' in pair[0] else 1)
        return valid[0]

    def _peer_names(self, roster: Optional[Roster]) -> Dict[str, Dict[str, Any]]:
        """注册表里的每台设备 -> 它自称的名字与共享清单。"""
        result: Dict[str, Dict[str, Any]] = {}
        if self._registry is None:
            return result
        names = self._device_names(roster)
        for record in self._registry.snapshot():
            device_id = record.device_key.hex()
            result[device_id] = {'name': names.get(record.device_key) or f'设备 {record.device_id}',
                                 'shares': list(record.shares),
                                 'endpoints': [list(e) for e in record.endpoints],
                                 'seq': record.seq,
                                 'ts': record.ts}
        return result

    def _peers_file(self) -> Path:
        """手动登记的对端端点。放在 `.cache/` 里：它是**发现用的提示**，不是权威数据
        （权威数据是设备自签的注册记录），删掉只会让发现退回初始状态。"""
        return self.cache_dir / 'peers.json'

    def _load_manual_peers(self) -> List[Dict[str, Any]]:
        path = self._peers_file()
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            log.warning('[group-mesh] peers.json 无法解析，按空表处理')
            return []
        entries = payload.get('peers') if isinstance(payload, dict) else None
        return entries if isinstance(entries, list) else []

    def _save_manual_peers(self, entries: List[Dict[str, Any]]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self._peers_file()).write_text(
            json.dumps({'peers': entries}, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')

    def peers(self, opts: Any = None, action: str = 'list', endpoint: str = '',
              name: str = '') -> Dict[str, Any]:
        """手动登记/移除对端端点（发现的**起点**）。

        为什么必须有这个东西：注册记录里的端点是设备自己发布的，而"第一次发现
        一台设备"没有任何自动途径 —— 注册表此刻是空的。设计文档 §7.4 把这个缺口
        交给"稳定地址 + 带外交换"，插件层就得提供一个填地址的地方，否则用户会卡在
        "我知道对方 IP，但界面里没有地方输入"。
        """
        options = _opts(opts)
        action = str(options.get('action') or action or 'list')
        endpoint = str(options.get('endpoint') or endpoint or '').strip()
        name = str(options.get('name') or name or '').strip()
        entries = self._load_manual_peers()

        if action == 'list':
            return {'success': True, 'peers': entries}

        try:
            host, port = self._parse_endpoint(endpoint)
        except ValueError as e:
            return {'success': False, 'error': str(e)}

        if action == 'add':
            for entry in entries:
                if entry.get('host') == host and int(entry.get('port') or 0) == port:
                    return {'success': True, 'peers': entries, 'existed': True,
                            'message': f'{host}:{port} 已经在列表里'}
            entries.append({'host': host, 'port': port, 'name': name or '', 'added_at': int(time.time())})
            self._save_manual_peers(entries)
            return {'success': True, 'peers': entries}

        if action == 'remove':
            remaining = [e for e in entries
                         if not (e.get('host') == host and int(e.get('port') or 0) == port)]
            self._save_manual_peers(remaining)
            return {'success': True, 'peers': remaining,
                    'removed': len(entries) - len(remaining)}

        return {'success': False, 'error': f'未知的 peers action: {action!r}'}

    @staticmethod
    def _parse_endpoint(text: str) -> Tuple[str, int]:
        """解析 `host:port` 或 `[IPv6]:port`。

        方括号必须显式处理：裸 IPv6 地址里有多个冒号，`rpartition(':')` 会把地址
        尾段当成端口，得到一个"看起来合法"的错误结果（内核 CLI 里同款处理）。
        """
        raw = (text or '').strip()
        if not raw:
            raise ValueError('端点不能为空（形如 192.168.31.16:19443 或 [2409:…]:19443）')
        if raw.startswith('['):
            closing = raw.find(']')
            if closing < 0:
                raise ValueError(f'IPv6 端点缺少右方括号: {text!r}')
            host = raw[1:closing]
            rest = raw[closing + 1:]
            if not rest.startswith(':'):
                raise ValueError(f'IPv6 端点缺少端口: {text!r}')
            port_text = rest[1:]
        else:
            if ':' not in raw:
                raise ValueError(f'端点应写成 host:port，收到 {text!r}')
            if raw.count(':') > 1:
                raise ValueError(f'IPv6 端点必须写成 [地址]:端口，收到 {text!r}')
            host, _, port_text = raw.rpartition(':')
        if not host:
            raise ValueError(f'端点缺少主机部分: {text!r}')
        try:
            port = int(port_text)
        except ValueError as e:
            raise ValueError(f'端口不是整数: {port_text!r}') from e
        if not 1 <= port <= 65535:
            raise ValueError(f'端口越界: {port}')
        return host, port

    def list_peers(self, opts: Any = None, refresh: bool = True) -> Dict[str, Any]:
        """列出团体里的对端设备及其可读共享项。

        `refresh=True` 时逐台连过去拉注册快照（§7.3：任一在线节点都能提供完整
        快照），因此"别人开机后我能看到它"不需要等任何人手动同步。对端不可达
        只影响它自己那一条 —— 不能因为有一台离线就让整张列表失败。
        """
        options = _opts(opts)
        if 'refresh' in options:
            refresh = options['refresh']
        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}

        self._ensure_registry()
        errors: List[Dict[str, str]] = []
        if refresh:
            # 先确保本机的注册记录是最新的（地址变化时递增 seq 重发，§7.4）。
            # 没有这一步，"我换了网络"会让对端一直用旧端点连我。
            self._refresh_own_registration()
            # 再用手动登记的端点做**引导**：注册表初始是空的，没有这一步就永远
            # 学不到第一台设备的端点（鸡生蛋）。
            seen: List[Tuple[str, int]] = []
            for entry in self._load_manual_peers():
                try:
                    endpoint = (str(entry['host']), int(entry['port']))
                except (KeyError, TypeError, ValueError):
                    continue
                if endpoint in seen:
                    continue
                seen.append(endpoint)
                try:
                    self._fetch_registry_from(endpoint, identity, roster)
                except (RemoteError, TransportError, OSError) as e:
                    errors.append({'device': str(entry.get('name') or endpoint[0]),
                                   'error': str(e)})
            # 再按注册记录里已经学到的端点逐台刷新（§7.3）。对端不可达只影响它自己，
            # 不能因为一台离线就让整张列表失败。
            for device_id, info in list(self._peer_names(roster).items()):
                endpoint = self._pick_endpoint(info.get('endpoints'))
                if endpoint is None or endpoint in seen:
                    continue
                try:
                    self._fetch_registry_from(endpoint, identity, roster)
                except (RemoteError, TransportError, OSError) as e:
                    errors.append({'device': info.get('name') or device_id,
                                   'error': str(e)})

        peers: List[Dict[str, Any]] = []
        names = self._peer_names(roster)
        self_device_id = identity.device.public_key.hex()
        for device_id, info in names.items():
            if device_id == self_device_id:
                continue  # 本机不作为"对端"列出
            endpoint = self._pick_endpoint(info.get('endpoints'))
            peers.append({
                'device_id': device_id,
                'name': info.get('name'),
                'endpoints': info.get('endpoints') or [],
                'endpoint': list(endpoint) if endpoint else None,
                'shares': sorted(info.get('shares') or []),
                'seq': info.get('seq'),
                'last_seen': info.get('ts'),
                'online': None,   # 真正的可达性由 list_remote 首次请求时才知道
            })
        # 名单里有、注册表里没有的设备：它从没发布过注册记录（没启动过带注册表的
        # 节点），但仍在团体里 —— 必须列出来，否则用户会以为"名单里少了个人"。
        for member in roster.members:
            for device in member.device_keys:
                device_id = device.hex()
                if device_id == self_device_id or device_id in names:
                    continue
                peers.append({'device_id': device_id, 'name': member.name,
                              'endpoints': [], 'endpoint': None, 'shares': [],
                              'seq': None, 'last_seen': None, 'online': None,
                              'note': '该设备尚未发布过注册记录（未启动节点或未启用注册表）'})
        return {'success': True, 'peers': peers, 'errors': errors,
                'registry_enabled': self._registry is not None,
                'registry_size': len(self._registry) if self._registry is not None else 0}

    def _ensure_registry(self) -> None:
        """确保手上有注册表对象（节点没启动时也能读磁盘上的那份）。"""
        if self._registry is not None:
            return
        try:
            self._registry = registry_mod.load_registry(self.identity_dir)
        except Exception as e:
            self._registry = registry_mod.Registry()
            log.warning(f'[group-mesh] 注册表载入失败，按空表处理: {e}')

    def _fetch_registry_from(self, endpoint: Tuple[str, int], identity: Identity,
                             roster: Optional[Roster]) -> int:
        """连过去拉一次注册快照并合并，返回采纳条数。"""
        connection = self._connect(endpoint, identity, roster)
        try:
            remote = mesh_client.fetch_registry(connection)
        finally:
            self._release(connection)
        accepted = self._registry.merge(remote)
        if accepted:
            try:
                registry_mod.save_registry(self.identity_dir, self._registry)
            except OSError as e:
                log.warning(f'[group-mesh] 注册表写盘失败: {e}')
        return accepted

    def _connect(self, endpoint: Tuple[str, int], identity: Identity,
                 roster: Optional[Roster]) -> Any:
        """取一条到 `endpoint` 的连接（复用或新建）。

        复用是必要的：一次 `Connection` 等于一次完整 Noise 握手，而"浏览一个目录"
        在界面上是多次请求（列共享、列目录、取缩略图）。连接失效时（对端重启、
        网络断开）丢弃重连一次 —— 只重连一次，不对着不可达地址反复重试。
        """
        key = (identity.device.public_key, endpoint[0], endpoint[1])
        with self._lock:
            cached = self._connections.get(key)
            if cached is not None:
                try:
                    mesh_client.list_shares(cached)
                    return cached
                except Exception:
                    self._connections.pop(key, None)
                    try:
                        cached.close()
                    except Exception:
                        pass
        connection = mesh_client.open_connection(endpoint[0], endpoint[1], identity, roster)
        with self._lock:
            self._connections[key] = connection
        return connection

    def _release(self, connection: Any) -> None:
        """归还连接。当前实现是复用的，因此这里不做任何事。

        保留这个调用点是为了让"以后改成短连接（用完即关）"只改这一个方法，
        而不用去翻每个调用方 —— 连接的生死只应在一处决定。
        """
        return

    def _manual_endpoints(self, device_id: str, roster: Roster) -> List[Tuple[Tuple[str, int], str]]:
        """手动登记里的候选端点（含注册表学到的那一个，如果已知）。

        返回多个候选是为了"同一台设备在局域网与 IPv6 上各有一个地址"这种情形：
        第一个连不上时再试下一个。**只对连接失败重试** —— 权限或路径错误是确定性
        结论，换一台设备重试只会把真正的错误盖掉。
        """
        candidates: List[Tuple[Tuple[str, int], str]] = []
        identity = self._load_identity()
        names = self._peer_names(roster) if self._registry is not None else {}
        info = names.get(device_id) if device_id else None
        if info is not None:
            picked = self._pick_endpoint(info.get('endpoints'))
            if picked is not None:
                candidates.append((picked, str(info.get('name') or device_id)))
        for entry in self._load_manual_peers():
            if info is not None and str(entry.get('name') or '') and \
                    str(entry.get('name')) != str(info.get('name') or ''):
                # 手动条目按名字区分设备时，别把别的设备的地址拿来重试
                continue
            try:
                endpoint = (str(entry['host']), int(entry['port']))
            except (KeyError, TypeError, ValueError):
                continue
            label = str(entry.get('name') or '')
            if (endpoint, label) not in candidates:
                candidates.append((endpoint, label))
        # 引导：只有地址、还不知道设备 ID 时，手动登记的就是全部候选
        if not device_id and not candidates:
            for entry in self._load_manual_peers():
                try:
                    candidates.append(((str(entry['host']), int(entry['port'])),
                                       str(entry.get('name') or '')))
                except (KeyError, TypeError, ValueError):
                    continue
        _ = identity
        return candidates

    def list_remote(self, opts: Any = None, device_id: str = '', share_id: str = '',
                    path: str = '.') -> Dict[str, Any]:
        """列对端的共享项，或列某个共享项里的目录。

        `device_id` 可以留空：引导阶段只填了地址时，直接连那个地址，对端会在应答里
        报出自己的身份（`peer_device_id`），界面据此补上设备 ID。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
        share_id = str(options.get('share_id') or share_id or '')
        path = str(options.get('path') or path or '.')

        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}

        candidates = self._manual_endpoints(device_id, roster)
        if not candidates:
            return {'success': False,
                    'error': '还没有可用的对端地址。请先在"对端"里登记一台设备的地址，'
                             '或刷新一次让本机通过注册记录发现它'}

        last_error = ''
        for endpoint, label in candidates:
            try:
                connection = self._connect(endpoint, identity, roster)
            except (RemoteError, TransportError, OSError) as e:
                last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                continue
            peer_id = connection.peer.device_key.hex()
            try:
                if not share_id:
                    shares = mesh_client.list_shares(connection)
                    return {'success': True, 'device_id': peer_id, 'peer_device_id': peer_id,
                            'name': label or connection.peer.name,
                            'endpoint': list(endpoint), 'shares': shares}
                result = mesh_client.list_directory(connection, share_id, path)
            except RemoteError as e:
                # 确定性错误（无权限/没有这个共享项/路径不存在）：**不换设备重试**，
                # 否则真正的拒绝原因会被"另一台也连不上"覆盖掉。
                return {'success': False, 'error': str(e), 'device_id': device_id,
                        'peer_device_id': peer_id, 'share_id': share_id, 'path': path}
            except (TransportError, OSError) as e:
                last_error = f'与 {label or peer_id} 的连接中断: {e}'
                continue
            finally:
                self._release(connection)

            return {'success': True, 'device_id': peer_id, 'peer_device_id': peer_id,
                    'name': label or connection.peer.name,
                    'endpoint': list(endpoint), 'share_id': share_id,
                    'path': result.get('path') or path,
                    'dir': bool(result.get('dir')),
                    'entries': result.get('entries') or []}

        return {'success': False, 'error': last_error or '所有候选地址都连不上',
                'device_id': device_id, 'offline': True}

    def download_remote(self, opts: Any = None, device_id: str = '', share_id: str = '',
                        path: str = '', overwrite: bool = False) -> Dict[str, Any]:
        """把对端共享项里的一个文件取回本机（落进下载目录）。

        落点在 `downloads_dir`（设置项 `download_dir` 可改）。文件名保留对端的
        相对结构：`<共享标识>/<相对路径>` —— 只用文件名会把对端两个同名文件
        撞成一个，而相对路径同时也是 `list_remote` 返回的 `path`，界面据此直接
        拼出可访问的本机路径。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '')
        share_id = str(options.get('share_id') or share_id or '')
        path = str(options.get('path') or path or '')
        overwrite = bool(options.get('overwrite', overwrite))

        if not share_id or not path:
            return {'success': False, 'error': '必须给出 share_id 与 path'}

        # 目标路径必须落在下载目录之内：对端给的相对路径可能带 `..`，
        # 直接拼接会让它写到下载目录之外（内核会拒绝越界的共享内路径，
        # 但本机这一侧的目标路径同样必须校验）。
        # 这一步刻意放在解析设备之前：它是纯粹的本地校验，不该因为"设备没填/不可达"
        # 而改变结论，也让用例可以在离线状态下验证它。
        relative = Path(str(path).replace('\\', '/').lstrip('/'))
        if relative.is_absolute() or any(part == '..' for part in relative.parts):
            return {'success': False, 'error': f'路径越界: {path!r}'}
        destination = self.downloads_dir / share_id / relative
        try:
            destination.resolve().relative_to(self.downloads_dir.resolve())
        except ValueError:
            return {'success': False, 'error': f'路径越界: {path!r}'}
        if destination.exists() and not overwrite:
            return {'success': True, 'skipped': True, 'local_path': str(destination),
                    'reason': '本机已有同名文件（overwrite=true 可覆盖）'}

        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}

        candidates = self._manual_endpoints(device_id, roster)
        if not candidates:
            return {'success': False,
                    'error': '还没有可用的对端地址。请先在"对端"里登记一台设备的地址，'
                             '或刷新一次让本机通过注册记录发现它'}

        last_error = ''
        for endpoint, label in candidates:
            try:
                connection = self._connect(endpoint, identity, roster)
            except (RemoteError, TransportError, OSError) as e:
                last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                continue
            peer_id = connection.peer.device_key.hex()
            try:
                written = mesh_client.fetch_to_file(connection, share_id, path, destination)
            except RemoteError as e:
                # 确定性错误（无权限/文件不存在）：不换设备重试
                return {'success': False, 'error': str(e), 'device_id': peer_id,
                        'peer_device_id': peer_id, 'share_id': share_id, 'path': path}
            except (TransportError, OSError) as e:
                last_error = f'传输失败: {e}'
                continue
            finally:
                self._release(connection)

            return {'success': True, 'device_id': peer_id, 'peer_device_id': peer_id,
                    'name': label or connection.peer.name,
                    'share_id': share_id, 'path': path, 'local_path': str(destination),
                    'bytes': written, 'size': destination.stat().st_size}

        return {'success': False, 'error': last_error or '所有候选地址都连不上',
                'device_id': device_id, 'offline': True}

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
        # 端口必须区分"没配"与"配成 0"：0 是**合法**取值，表示让内核分配一个空闲
        # 端口（用例与临时联调都用它）。原先写成 `setting(...) or DEFAULT_PORT`，
        # 而 0 是 falsy，于是"端口 0"被静默换成 19443 —— 表现为"设了 0 却还是占
        # 默认端口"，撞端口时很难查。
        raw_port = self.setting('port', None)
        port = DEFAULT_PORT if raw_port is None or raw_port == '' else int(raw_port)
        shares = self._load_shares()
        # 注册表（§7）：不注入的话 node 的 `_op_registry` 一律回 unavailable，
        # 于是"设备自己发布端点与共享清单、其他设备据此发现它"这条链断在插件层。
        # 载入失败不能让节点起不来 —— 注册表是可重建的辅助数据，缺了只是发现能力弱。
        try:
            self._registry = registry_mod.load_registry(self.identity_dir)
        except Exception as e:
            self._registry = registry_mod.Registry()
            log.warning(f'[group-mesh] 注册表载入失败，按空表处理: {e}')
        registry = self._registry
        self._node_stop.clear()
        self._last_error = None
        ready = threading.Event()

        def run() -> None:
            def on_ready(listener: socket.socket) -> None:
                self._listener = listener
                self._listening = listener.getsockname()[:2]
                # 监听成功后才发布：端点必须是用真正绑上的端口（设置里写 0 时
                # 内核会分配一个空闲端口），发布设置里的值会指向一个没人听的端口。
                try:
                    self._published_seq = self._publish_registration(bind, self._listening[1])
                except Exception as e:  # 兜底：发布失败不能拖垮节点
                    log.warning(f'[group-mesh] on_ready 发布注册记录异常: {e}')
                ready.set()

            try:
                # roster_loader：每次建连重读名单。群主在别处加了成员后，
                # 运行中的节点必须立刻认，而不是要求用户重启插件。
                serve(bind, port, identity, roster, shares=shares, registry=registry,
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
            # 用户手动启动成功：清掉自动启动的失败记录，并标记"已尝试"，
            # 免得后续每次取状态都再试一遍（自动启动只该在需要时发生一次）。
            self._auto_start_attempted = True
            self._auto_start_error = None
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

    def _publish_registration(self, bind: str, port: int) -> Optional[int]:
        """发布本机的注册记录：端点 + 共享清单（设计文档 §7.1）。

        **注册是"设备自助"行为**（§7.2），所以这件事必须由节点自己做，而不是等人
        手动同步 —— 在此之前只有 CLI 的 `serve` 会发布（`cli.py` 的 `cmd_serve`），
        插件起的节点从不发布，于是其他设备的注册表里永远没有它的端点，
        "看得到对方"这条链在插件层就是断的。

        返回新的 `seq`；发布失败（例如注册表写不进去）不阻断节点启动 ——
        节点能收能发才是主要功能，发现能力是附加的。
        """
        identity = self._load_identity()
        if identity is None:
            return None
        try:
            state_file = self.identity_dir / 'state.json'
            state: Dict[str, Any] = {}
            if state_file.is_file():
                try:
                    state = json.loads(state_file.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    state = {}
            seq = int(state.get('registration_seq', 0) or 0) + 1
            # 绑到具体地址时只发布那个地址；绑到通配地址时发布本机全部可用地址。
            if bind in ('0.0.0.0', '::'):
                addresses = registry_mod.local_addresses() or [bind]
            else:
                addresses = [bind]
            endpoints = [(addr, int(port)) for addr in addresses]
            registration = registry_mod.new_registration(
                device_key=identity.device.public_key,
                device_private_key=identity.device.private_key,
                seq=seq, endpoints=endpoints, shares=sorted(self._load_shares()))
            self._registry.add(registration)
            registry_mod.save_registry(self.identity_dir, self._registry)
            state['registration_seq'] = seq
            state['endpoints'] = [[a, p] for a, p in endpoints]
            state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n',
                                  encoding='utf-8')
            log.info(f'[group-mesh] 已发布注册记录 seq={seq}，端点 {endpoints}')
            return seq
        except Exception as e:  # 发布失败只记日志，不让节点起不来
            log.warning(f'[group-mesh] 注册记录发布失败: {e}')
            return None

    def _refresh_own_registration(self) -> Optional[int]:
        """本机地址或共享清单变了就递增 `seq` 重新发布（设计文档 §7.4）。

        §7.4 要求的是两条：用**稳定地址**发布，以及"定期比对本机全球单播地址集合，
        变化则递增 seq 并重新发布"。原先我只在节点启动时发布一次 —— 那两条都没满足：
        换了网络（Wi-Fi 切换、DHCP 重新分配、IPv6 前缀变化）之后，对端的注册表里
        留着的是旧端点，表现为"明明两边都开着却连不上"，而且没有任何提示。

        触发点放在每次刷新设备列表时（进入插件、点"刷新设备"），不做后台定时器：
        这条路径本来就要连对端，顺手做一次本地比对几乎不增加成本，而常驻定时器会
        让插件在切到后台之后仍然干活（`onHide` 的语义是"停止轮询类工作"）。
        """
        if self._node_thread is None or not self._node_thread.is_alive():
            return None   # 节点没在跑：没有"当前端点"可发布
        bind = str(self.setting('bind', '::') or '::')
        listening = self._listening
        if not listening:
            return None
        port = int(listening[1])
        if bind in ('0.0.0.0', '::'):
            addresses = registry_mod.local_addresses() or [bind]
        else:
            addresses = [bind]
        endpoints = [[addr, port] for addr in addresses]
        try:
            state = json.loads((self.identity_dir / 'state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
        # 与上次发布的端点集合比较。共享清单变化不在这里管：共享项由
        # `add_share` / `remove_share` 之后的下一次刷新自然带上。
        if state.get('endpoints') == endpoints:
            return None
        published = self._publish_registration(bind, port)
        if published is not None:
            self._published_seq = published
            log.info(f'[group-mesh] 本机端点变化，已重新发布注册记录 seq={published}：{endpoints}')
        return published

    def _auto_start_node(self, identity_exists: bool, roster_exists: bool) -> None:
        """有身份与团体且节点没在跑时自动启动（只尝试一次，失败不重试）。

        为什么必须自动：节点不跑 → 本机不发布注册记录 → **别人永远发现不了我**；
        而"另一个成员要先手动点一次启动节点，我才能看到他"这件事没有任何提示，
        表现出来就是"明明都在同一个团体里却看不到对方"。

        为什么失败后不重试：失败原因通常是端口被占或防火墙拦截，重试不会有不同
        结果，反而会在每次刷新状态时反复起线程。失败详情留在 `_last_error` 里，
        由界面的节点卡片显示 —— 用户看到原因后可以改端口再点"启动节点"。
        """
        if self._auto_start_attempted or not (identity_exists and roster_exists):
            return
        if self._node_thread is not None and self._node_thread.is_alive():
            return
        self._auto_start_attempted = True
        result = self.start_node()
        if result.get('success'):
            log.info('[group-mesh] 已自动启动共享节点（设计文档 §7：注册是设备自助行为）')
            self._auto_start_error = None
        else:
            self._auto_start_error = result.get('error') or '自动启动失败'
            log.warning(f'[group-mesh] 自动启动节点失败：{self._auto_start_error}')

    def get_node_status(self) -> Dict[str, Any]:
        thread = self._node_thread
        running = bool(thread is not None and thread.is_alive())
        return {
            'running': running,
            'listening': f'{self._listening[0]}:{self._listening[1]}' if self._listening else None,
            'connection_count': self._connection_count,
            'error': self._last_error,
            # 本机是否已把自己发布出去：别人能不能发现我，只看这个。
            'published_seq': self._published_seq,
            'published': self._published_seq is not None,
            # 自动启动失败的原因（None = 没失败或还没试过），交给界面显示
            'auto_start_error': self._auto_start_error,
        }

    def _close_connections(self) -> None:
        """关掉全部出站连接。改设置、停节点、卸载时必须调用。

        不关的后果：端口或身份变了以后旧连接仍在用旧参数（表现为"设置改了但
        行为没变"），进程退出时还会留下半开的套接字。
        """
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass

    def on_settings_changed(self, changed_keys: set) -> None:
        """设置变更后作废依赖旧设置的状态。

        壳保存设置后会整页重载插件（`base.js` 的 openSettingsModal），因此这里
        不需要重建界面；但**运行期状态**不会自动跟着变 —— 连接是按
        `(设备, 地址, 端口)` 缓存的，改了监听端口或下载目录之后必须丢弃。
        """
        if {'port', 'bind', 'download_dir'} & set(changed_keys or ()):
            self._close_connections()
        if {'port', 'bind'} & set(changed_keys or ()):
            # 端口/绑定变了：允许重新自动尝试一次（节点已停，旧尝试的结论作废）
            self._auto_start_attempted = False
            self._auto_start_error = None

    # ── 生命周期与 API ────────────────────────────────────────────────────

    def on_load(self) -> None:
        self.get_data_root().mkdir(parents=True, exist_ok=True)

    def on_unload(self) -> None:
        # 插件卸载必须留不下监听线程与出站连接，否则重载会撞端口占用
        self._close_connections()
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
            'refresh_share_roots': self.refresh_share_roots,
            'peers': self.peers,
            'list_peers': self.list_peers,
            'list_remote': self.list_remote,
            'download_remote': self.download_remote,
            'start_node': self.start_node,
            'stop_node': self.stop_node,
            'get_node_status': self.get_node_status,
        }
