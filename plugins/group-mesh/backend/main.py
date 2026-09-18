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

## 当前状态

已接通：身份初始化、团体创建/加入、名单查看与修改、共享项管理、节点启停与状态，
双向传输（取回、物化、上传含进度/取消/续传，任务表落盘可跨重载），
名单/注册的后台同步（定时轮询 + 变更时 push，间隔见设置 `sync_interval_seconds`）。
私钥由 `secret_store` 保护（Windows DPAPI / 桌面 keyring / 口令 AES-GCM）。
**尚未接通**：内容寻址分块、Android 轻客户端。
房间/语音/游戏面由未来的 Companion 子插件承担，不在本插件范围内。

## 壳侧主体上下文（设计文档 §12）

设计文档 §12 第 1/2 项要求"凭据到主体的映射"与"插件以受信方式读取主体"。
壳侧**已经落地**：`shell/backend/principal.py` 在鉴权通过后把主体写入
`ContextVar`，插件用 `self.current_principal()` / `self.require_principal()`
读取；请求参数里的 `principal` / `role` 字段影响不了它，后台线程读不到主体
（后台任务需要显式携带主体，见 `shell/backend/principal.use_principal()`）。

**本插件尚未接入**：下面所有涉及主体的判定仍以**本机主体**为准（本机就是这个
团体里的一个节点），访问控制只到"有效令牌"这一层。收尾时应在有副作用的方法
（`add_member` / `add_share` / `start_node` / `stop_node` / `upload_remote` /
`mirror_share` / `clear_remote_cache` 等）上调用 `require_principal()` 并按角色
判定；背景与风险见 `docs/group-mesh-implementation-path.md` §4.9。

## 代码布局

本文件只留类骨架与生命周期：常量与纯函数在 `common.py`，方法按职责拆到 12 个 mixin
分片（storage / status / group / shares / peers / sync / remote / upload /
materialize / mirror / fetch / node），各分片由 `shell.backend.plugin_utils.load_sibling`
加载。**mixin 必须排在 PluginBase 之前**：`get_data_root` / `get_protected_paths` /
`on_settings_changed` 都是对基类的覆写，排在基类后面会被基类实现盖掉。
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling
from shell.groupmesh import roster_history as roster_history_mod

log = logging.getLogger(__name__)

_storage = load_sibling(__file__, 'storage', 'group_mesh')
_status = load_sibling(__file__, 'status', 'group_mesh')
_group = load_sibling(__file__, 'group', 'group_mesh')
_shares = load_sibling(__file__, 'shares', 'group_mesh')
_peers = load_sibling(__file__, 'peers', 'group_mesh')
_sync = load_sibling(__file__, 'sync', 'group_mesh')
_remote = load_sibling(__file__, 'remote', 'group_mesh')
_upload = load_sibling(__file__, 'upload', 'group_mesh')
_materialize = load_sibling(__file__, 'materialize', 'group_mesh')
_mirror = load_sibling(__file__, 'mirror', 'group_mesh')
_fetch = load_sibling(__file__, 'fetch', 'group_mesh')
_node = load_sibling(__file__, 'node', 'group_mesh')

# 常量与纯函数在 common.py：入口只绑定自己用到的那些（分片各自绑定自己用的）
_common = load_sibling(__file__, 'common', 'group_mesh')
DEFAULT_PORT = _common.DEFAULT_PORT


# 分片顺序即 MRO：覆写基类的方法必须排在 PluginBase 之前。
class GroupMeshPlugin(
    _storage.StorageMixin,
    _status.StatusMixin,
    _group.GroupMixin,
    _shares.ShareMixin,
    _peers.PeerMixin,
    _sync.SyncMixin,
    _remote.RemoteMixin,
    _upload.UploadMixin,
    _materialize.MaterializeMixin,
    _mirror.MirrorMixin,
    _fetch.FetchMixin,
    _node.NodeMixin,
    PluginBase,
):
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
        # `local_only`：本字段**只接受本机目录**，不显示「🌐 网络位置」按钮 ——
        # 那是"把远端共享项取回本地"的入口，而这里正是取回后的落点，语义上不能用它
        # 自己当自己的来源（而且取回的中间目录不是用户想要的下载目录）。
        {'key': 'download_dir', 'label': '远端下载目录', 'type': 'directory',
         'local_only': True,
         'default': '', 'placeholder': '默认：数据根/group-mesh/downloads',
         'help': '从团体成员那里取回的文件保存在这里。该目录经 /file 对界面可读，'
                 '但不对团体共享 —— 要共享它请单独挂一个共享项。'},
        {'key': 'max_fetch_mb', 'label': '按需取回单文件上限（MiB）', 'type': 'number',
         'default': 1024, 'min': 0, 'max': 102400,
         'help': '浏览远端目录时，超过这个大小的文件不会自动取回（0 表示不限制）。'
                 '远端目录先物化成目录结构与占位文件，字节在第一次取回时才取 —— '
                 '这个上限用来避免"点开一个目录就把磁盘写满"。'},
        {'key': 'sync_interval_seconds', 'label': '后台同步间隔（秒）', 'type': 'number',
         'default': 60, 'min': 5, 'max': 3600,
         'help': '节点运行期间每隔这么久拉取一次注册表与团体名单，并把本机变更推给对端。'
                 '默认 60 秒；调小更及时，但会更频繁地连接对端。'},
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
        # 自动启动节点的状态：失败只进入冷却，不永久放弃（见 _auto_start_node）。
        self._auto_start_attempted = False
        self._auto_start_error: Optional[str] = None
        self._auto_start_retry_at = 0.0
        # 远端物化索引的进程内缓存：`is_content_placeholder()` 每个 /file 请求都要查
        # 一次，逐次解析 JSON 太贵（见 _cached_remote_index）。
        self._remote_index_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # 按需取字节的按路径锁：并发请求同一个远端文件时只让一个线程去取
        # （见 _fetch_lock）。键是 (设备ID, 共享标识, 相对路径)。
        self._fetch_locks: Dict[Tuple[str, str, str], threading.Lock] = {}
        self._fetch_locks_guard = threading.Lock()
        # 上一次刷新里连不上的端点 -> 失败原因（手动登记的地址失败时要回报给用户）
        self._unreachable: Dict[Tuple[str, int], str] = {}
        # 名单历史（`roster-history.json`）：准入判定要回答"本机认不认得对端手里
        # 那份"，只有当前名单说明不了"见过它前面的那些"（见 roster_history.py）。
        # 惰性构造：不是每个实例都会跑节点。
        self._roster_history_store: Optional[roster_history_mod.RosterHistory] = None
        # 对端名单版本记录（端点 -> {group, version, hash}）：界面据此做
        # "本机 v3 / 对方 v2" 的对照（§5.4 的宽限提示、§5.7 的分发可见性）。
        self._peer_roster_notes: Dict[Tuple[str, int], Dict[str, Any]] = {}
        # 最近一次自动采纳的新名单（界面提示"你的名单已从对方升级"）。
        self._roster_notice: Optional[Dict[str, Any]] = None
        # 已建立的出站连接：(设备公钥 bytes, host, port) -> Connection。
        # 复用的理由：一次请求就是一次 Noise 握手（多个往返 + 公钥运算），
        # 逐次建连会让"浏览一个目录"变成几个握手的开销。
        self._connections: Dict[Tuple[bytes, str, int], Any] = {}
        # 上传任务表（见 upload_remote）：task_id -> 任务字典（进度、状态、取消标志）。
        # 后台线程写、界面线程读，因此用一把独立的锁 —— 与 _lock（连接/共享项）分开，
        # 避免上传线程持锁时把界面请求堵住。
        self._uploads: Dict[str, Dict[str, Any]] = {}
        self._upload_order: List[str] = []
        self._uploads_guard = threading.Lock()
        # 卸载后老实例的上传线程可能还在收尾（取消要等到分块边界）。它**不得**再写
        # 任务表：同一个数据根已经交给新实例了，写回去会把新实例刚记下的任务覆盖掉。
        self._unloading = False
        # 后台同步（设计文档 §5.7 / §7.3）：
        #   - 定时轮询：每 `sync_interval_seconds` 拉一次注册表与名单；
        #   - 变更时 push：名单/注册变更后立即唤醒循环，把新版本推给对端。
        # `_sync_lock` 保证"后台同步"与显式 `list_peers(refresh=True)` 不会同时
        # 探测/合并；`_sync_state_lock` 只保护两个 dirty 标志。
        self._sync_thread: Optional[threading.Thread] = None
        self._sync_stop = threading.Event()
        self._sync_wake = threading.Event()
        self._sync_lock = threading.Lock()
        self._sync_state_lock = threading.Lock()
        # 注册记录的"读 seq → 写注册表 → 写 state.json"必须串行化：节点启动、
        # 后台同步、共享项变更都会触发发布。
        self._registration_lock = threading.Lock()
        self._roster_dirty = False
        self._registry_dirty = False
        self._last_sync_at: Optional[int] = None
        self._last_sync_error = ''

    # ── 生命周期与 API ────────────────────────────────────────────────────

    def on_load(self) -> None:
        self.get_data_root().mkdir(parents=True, exist_ok=True)
        self._unloading = False
        self._restore_uploads()
        # 后台同步随插件加载启动；`on_unload` 必须把它停掉，否则重载会留下线程。
        self._start_sync()

    def on_unload(self) -> None:
        # 插件卸载必须留不下监听线程、上传线程与出站连接，否则重载会撞端口占用。
        # 先立起卸载标志：取消是"到分块边界才生效"，而新实例可能已经开始读同一个
        # 数据根了。
        self._unloading = True
        self._stop_sync()
        self._cancel_all_uploads()
        self._close_connections()
        self.stop_node()

    def get_extensions(self) -> List[dict]:
        """把自己注册成"网络位置"提供方（壳共享目录组件的「🌐 网络位置」按钮用它）。

        为什么是扩展而不是依赖：共享目录组件出现在**任意插件**的设置里
        （image-viewer 的图片文件夹、media-player 的媒体目录…），壳不可能知道
        group-mesh 这个名字，image-viewer 也不该为"可能有个远端图库"去声明依赖。
        扩展机制里提供方与宿主互不认识，只认这一条 placement 约定。

        刻意**不写 `host`**：组件会对所有宿主展示，提供方应当对所有宿主可用。

        约定（见 `shell/frontend/public/shell/folder-picker.js`）：
        * 组件用 `system_get_plugin_extensions(null, 'network-location')` 发现提供方；
        * 提供方页面渲染在弹窗 iframe 里，选中后 `postMessage` 回填一个**本地目录**
          （`{type:'omnibox:network-location', action:'picked', path, label}`）——
          路径是本地的是关键：所有消费方的后端都只认本地路径。
        """
        return [{
            'placement': 'network-location',
            'id': 'group-mesh',
            'label': '团体组网',
            'icon': '🕸️',
            'description': '把某台成员设备上的共享项取到本地一个目录，再加进列表',
            'embedUrl': '/plugins/group-mesh/frontend/network-location.html',
        }]

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
            'my_endpoint': self.my_endpoint,
            'list_peers': self.list_peers,
            'list_remote': self.list_remote,
            'download_remote': self.download_remote,
            'upload_remote': self.upload_remote,
            'upload_status': self.upload_status,
            'cancel_upload': self.cancel_upload,
            'materialize_remote': self.materialize_remote,
            'mirror_share': self.mirror_share,
            'remote_cache': self.remote_cache,
            'clear_remote_cache': self.clear_remote_cache,
            'start_node': self.start_node,
            'stop_node': self.stop_node,
            'get_node_status': self.get_node_status,
        }
