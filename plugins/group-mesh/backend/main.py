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

已接通：身份初始化、团体创建/加入、名单查看与修改、共享项管理、节点启停与状态，
以及双向传输（取回、物化、上传含进度/取消/续传）。
**尚未接通**：内容寻址分块、上传任务的跨重载持久化、Android 轻客户端。
房间/语音/游戏面由未来的 Companion 子插件承担，不在本插件范围内。

## 未决的壳侧缺口（设计文档 §12）

设计文档 §12 第 1/2 项要求"凭据到主体的映射"与"插件以受信方式读取主体"。
壳目前**没有**这个能力（`shell/backend/auth.py` 只有单一令牌），因此本插件
所有涉及主体的判定都以**本机主体**为准（本机就是这个团体的一个节点），
不接收请求参数里的身份。等壳侧补上主体上下文后，这里应改为：
    `self.current_principal()` → 由 Shell 注入的 ContextVar 读取
而不是现在的"本机身份即操作者"。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import threading
import time
import uuid
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

# 自动启动节点失败后的冷却秒数。取 30 秒是因为最常见的失败原因是"旧进程还占着
# 端口"（服务重启后的窗口期），而启动流程自身还有 8 秒的 ready 等待。
AUTO_START_RETRY_SECONDS = 30

# 按需取回单个文件的默认上限（1 GiB）。浏览远端目录时不该因为点开一个目录就把
# 磁盘写满；超过上限的文件不自动取，界面会提示。
DEFAULT_MAX_FETCH_BYTES = 1024 * 1024 * 1024

# 探测端点的连接超时（秒）。刷新设备时要试多个端点，而设备常同时发布 IPv6 与
# IPv4 —— IPv6 在本机没有路由时每次都要等满超时。内核默认 20 秒，实测 5 个端点
# 串行等下来是 80 秒，界面看起来就是"卡住不动"。探测用短超时，真正的文件传输
# 仍走内核默认值。
PROBE_TIMEOUT_SECONDS = 3.0

# 一次刷新的候选探测**总**等待上限（秒）。到点就带着已有结果返回，不再等剩下的
# 端点 —— 界面上这是一次点击，不能因为几条死地址把用户按在那里。
PROBE_OVERALL_SECONDS = 4.0

# 上传的 socket 超时（秒）。比探测用的 3 秒宽得多：一个分块要走 Noise 加密 + 落盘，
# 慢盘或大分块下 3 秒会误判成"传输失败"。取消不靠超时兜底 —— 客户端在分块边界
# 检查取消标志（见 _upload_worker）。
UPLOAD_TIMEOUT_SECONDS = 30.0

# 上传任务表里保留多少个**已结束**的任务。界面要能看到最后一次的结果（成功/失败原因），
# 但也不能无限攒 —— 每个任务只是一小段字典，留 20 个足够翻看。
UPLOAD_KEEP = 20


def _opts(value: Any) -> Dict[str, Any]:
    """把"结构化参数"归一成字典。

    `/api/<plugin>__<method>` 会把 payload 的 `args`/`kwargs` 原样展开成位置/关键字
    参数（`shell/backend/file_server.py`），因此前端既可以传一个对象、也可以逐个传。
    两种形态都接受，避免以后前端或桥接实现改动时**参数静默错位**
    （例如 write 的值跑进 max_bytes，只在运行时才炸）。
    """
    return value if isinstance(value, dict) else {}


# 镜像（"网络位置"）判定"这个文件已经一致"时 mtime 的容差（纳秒）。
# 取 2 秒是因为 FAT/exFAT 的 mtime 粒度就是 2 秒，而本地文件系统会量化 os.utime
# 写下的值：要求逐 ns 相等会把同一份文件判成"需要重下"，每次执行都全量重传。
MIRROR_MTIME_WINDOW_NS = 2_000_000_000


def _mtime_ns(payload: Any) -> Optional[int]:
    """从对端应答的一条记录里取 mtime（纳秒整数）；缺失或非法时返回 None。

    None 的语义是"对端没给"（老版本内核不带这个字段），此时物化的变更判定退化为
    **只看大小** —— 与加入这个字段之前的能力一致，不假装自己能发现同大小的替换。
    显式排除 bool：`True` 是 int 的子类，放过去会变成一个 mtime=1 的荒谬值。
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get('mtime_ns')
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


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
        {'key': 'max_fetch_mb', 'label': '按需取回单文件上限（MiB）', 'type': 'number',
         'default': 1024, 'min': 0, 'max': 102400,
         'help': '浏览远端目录时，超过这个大小的文件不会自动取回（0 表示不限制）。'
                 '远端目录先物化成目录结构与占位文件，字节在第一次读取时才取 —— '
                 '这个上限用来避免"点开一个目录就把磁盘写满"。'},
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

    @property
    def staging_dir(self) -> Path:
        """按需取字节的暂存根：`<cache>/staging/<设备ID>/<共享标识>/<相对路径>`。

        刻意与 `remote_cache_dir` **平级**而不是放它里面：物化根下的路径会被
        `is_content_placeholder()` / `ensure_file()` 按 `<设备ID>/<共享标识>/…`
        反解，暂存文件混进去就会被当成一条物化条目来查索引（`.tmp` 会被当成设备 ID）。

        为什么不用用户下载目录（改动前的做法）：那是"用户明确要回来的文件"，
        浏览一张图就顺手往里扔一份，既污染用户目录，又让 `os.replace` 依赖
        "下载目录与数据根同卷"这个默认配置（跨卷会抛 OSError，见木已成舟的
        `.dsh/group-mesh-materialize.md` §2.2）。暂存根与被替换目标同在 `.cache` 下，
        同卷由构造保证。
        """
        return self.cache_dir / 'staging'

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
                '内容寻址分块传输（§10）',
                '上传任务在插件重载后的持久化（§6.4）',
                'Android 轻客户端（设计文档 §11.2，首版不实现）',
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
                  read: str = 'group', write: str = 'owner',
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
            acl = Acl(read=read, write=write)
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
    def _peer_endpoints(info: Dict[str, Any]) -> List[Tuple[str, int]]:
        """一台设备发布的**全部**可用端点（IPv6 在前，然后 IPv4）。

        为什么不能只返回一个：§4.6.1 把 IPv4 定位为可达性兜底，设备因此常常同时
        发布两种端点。只挑一个的话，首选那个不通（IPv6 没路由、对端只在 v4 上可达）
        就会把整台设备判成离线 —— 而它其实就在那儿。
        """
        valid: List[Tuple[str, int]] = []
        for item in info.get('endpoints') or []:
            try:
                address, port = str(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if address and 1 <= port <= 65535 and (address, port) not in valid:
                valid.append((address, port))
        valid.sort(key=lambda pair: 0 if ':' in pair[0] else 1)
        return valid

    @staticmethod
    def _pick_endpoint(endpoints: Iterable[Any]) -> Optional[Tuple[str, int]]:
        """从端点列表里挑一个**首选**（用于界面展示）。优先 IPv6 —— 那是主路径。"""
        picked = GroupMeshPlugin._peer_endpoints({'endpoints': list(endpoints or [])})
        return picked[0] if picked else None

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

    def _bootstrap_endpoints(self, identity: Any) -> List[Tuple[Tuple[str, int], str]]:
        """自举候选：**用本机自己发布的端点去问一次注册快照**。

        为什么这一招成立（设计文档 §7.3）：注册数据是"任一在线共享节点都能提供完整
        快照"。而两台机器可能都还没跟对方说过话 —— 此时谁也不知道对方地址，但**本机
        注册记录里写着本机自己的地址**，拿它去问"任何在线节点"（包括自己），就能把
        对方的记录（含对方的端点）一起拿回来。

        这正是"同一局域网里两台设备互相看不见"的成因：双方都只认识自己。实测
        复现过 —— 本机注册表里只有本机那一条（seq=43），名单里有对方但没有地址，
        于是界面只能如实显示"没有可用端点"。

        只在本机端点未知时才有用；已经学到对方端点之后这条自然命中不了新东西。
        安全性上没有新增暴露面：这些地址本来就是本机自己发布出去、供团体成员连的。
        """
        self._ensure_registry()
        candidates: List[Tuple[Tuple[str, int], str]] = []
        if self._registry is None:
            return candidates
        record = self._registry.get(identity.device.public_key)
        if record is None:
            return candidates
        for endpoint in self._peer_endpoints({'endpoints': record.endpoints}):
            candidates.append((endpoint, '本机'))
        return candidates

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
              name: str = '', device_id: str = '') -> Dict[str, Any]:
        """手工登记 / 移除 / 更新对端端点（发现的**起点**）。

        为什么必须有这个东西：注册记录里的端点是设备自己发布的，而"第一次发现
        一台设备"没有任何自动途径 —— 注册表此刻是空的。设计文档 §7.4 把这个缺口
        交给"稳定地址 + 带外交换"，插件层就得提供一个填地址的地方，否则用户会卡在
        "我知道对方 IP，但界面里没有地方输入"。

        `action` 取值：
          * `list` —— 列出已登记的条目；
          * `add` —— 按地址登记（同一地址重复添加会返回 `existed`）；
          * `update` —— **按设备更新地址**（对方换了网络/端口后，把它改到新地址；
            条目按 `device_id` 认，因此不会留下一堆指向同一台设备的旧地址）；
          * `remove` —— 按地址移除。
        """
        options = _opts(opts)
        action = str(options.get('action') or action or 'list')
        endpoint = str(options.get('endpoint') or endpoint or '').strip()
        name = str(options.get('name') or name or '').strip()
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
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
            entries.append({'host': host, 'port': port, 'name': name or '',
                            'device_id': device_id, 'added_at': int(time.time())})
            self._save_manual_peers(entries)
            return {'success': True, 'peers': entries}

        if action == 'update':
            # 按设备更新：先把同一设备（或同一地址）的旧条目清掉，再写入新地址。
            # 只按地址更新的话，对方换端口后会留下一条永远连不上的死地址。
            kept = [e for e in entries
                    if not ((device_id and str(e.get('device_id') or '').lower() == device_id)
                            or (e.get('host') == host and int(e.get('port') or 0) == port))]
            updated = name or next((str(e.get('name') or '') for e in entries
                                    if device_id and
                                    str(e.get('device_id') or '').lower() == device_id), '')
            kept.append({'host': host, 'port': port, 'name': updated,
                         'device_id': device_id, 'updated_at': int(time.time())})
            self._save_manual_peers(kept)
            return {'success': True, 'peers': kept,
                    'replaced': len(entries) - len(kept) + 1}

        if action == 'remove':
            remaining = [e for e in entries
                         if not (e.get('host') == host and int(e.get('port') or 0) == port)]
            self._save_manual_peers(remaining)
            return {'success': True, 'peers': remaining,
                    'removed': len(entries) - len(remaining)}

        return {'success': False, 'error': f'未知的 peers action: {action!r}'}

    def my_endpoint(self, opts: Any = None) -> Dict[str, Any]:
        """本机的可分享地址串（给对方粘贴用）。

        为什么需要"复制"这个动作：地址无法自动跨机传播 —— 对方不问我、我也不知道
        它换了地址；而带外交换（当面/聊天工具粘一次）是设计里就承认的引导手段
        （§7.4）。因此界面要能一键拿到这串东西，让对方粘进"更新对方地址"。
        """
        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份'}
        self._ensure_registry()
        record = self._registry.get(identity.device.public_key) if self._registry else None
        endpoints: List[Any] = []
        node = self.get_node_status()
        if record is not None:
            endpoints = [list(e) for e in record.endpoints]
        if self._listening:
            live = [self._listening[0], int(self._listening[1])]
            if live not in endpoints:
                endpoints.insert(0, live)
        lines: List[str] = []
        device_name = identity.principal.name
        if roster is not None:
            entry = roster.entry_of(identity.principal.public_key)
            if entry is not None:
                device_name = entry.name
        lines.append(f'设备名: {device_name}')
        lines.append(f'设备公钥: {base64.b64encode(identity.device.public_key).decode("ascii")}')
        if endpoints:
            lines.append('地址:')
            for host, port in endpoints:
                text = f'[{host}]:{port}' if ':' in str(host) else f'{host}:{port}'
                lines.append(f'  {text}')
        else:
            lines.append('地址: （节点未运行，或还没发布注册记录）')
        return {'success': True, 'text': '\n'.join(lines),
                'endpoints': endpoints, 'device_id': identity.device.public_key.hex(),
                'name': device_name, 'running': node['running'],
                'published': node['published']}

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
        self_device_id = identity.device.public_key.hex()
        errors: List[Dict[str, str]] = []
        if refresh:
            # 先确保本机的注册记录是最新的（地址变化时递增 seq 重发，§7.4）。
            # 没有这一步，"我换了网络"会让对端一直用旧端点连我。
            self._refresh_own_registration()
            # 收集候选端点：手动登记的（**引导**，注册表初始为空时唯一的起点）、
            # 本机自己发布的（**自举**，用本机地址去问任一在线节点拿完整快照，
            # §7.3）、以及注册表里已经学到的。
            candidates: List[Tuple[Tuple[str, int], str]] = []
            seen: set = set()

            def add(endpoint: Tuple[str, int], label: str) -> None:
                if endpoint and endpoint not in seen:
                    seen.add(endpoint)
                    candidates.append((endpoint, label))

            for entry in self._load_manual_peers():
                try:
                    add((str(entry['host']), int(entry['port'])),
                        str(entry.get('name') or entry.get('host') or ''))
                except (KeyError, TypeError, ValueError):
                    continue
            for endpoint, label in self._bootstrap_endpoints(identity):
                add(endpoint, label)
            for device_id, info in self._peer_names(roster).items():
                if device_id == self_device_id:
                    continue
                for endpoint in self._peer_endpoints(info):
                    add(endpoint, str(info.get('name') or device_id))

            # **并行**探测。为什么必须并行：设备常同时发布 IPv6 与 IPv4 端点
            # （§4.6.1 把 v4 当可达性兜底），而 IPv6 在本机常常没有路由 —— 串行
            # 等超时的话，几个端点就能把"刷新设备"拖到一分钟以上（实测 80 秒，
            # 界面一直停在"正在读取设备"）。并行 + 短超时后总耗时就接近单次超时。
            self._fetch_registries_parallel(candidates, identity, roster)
            # 手动登记的端点若仍然连不上，单独回报（用户刚填的地址，失败要说清楚）
            for entry in self._load_manual_peers():
                try:
                    endpoint = (str(entry['host']), int(entry['port']))
                except (KeyError, TypeError, ValueError):
                    continue
                if endpoint in self._unreachable:
                    errors.append({'device': str(entry.get('name') or endpoint[0]),
                                   'error': self._unreachable[endpoint]})

        peers: List[Dict[str, Any]] = []
        names = self._peer_names(roster)
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

    def _fetch_registries_parallel(self, candidates: List[Tuple[Tuple[str, int], str]],
                                   identity: Identity, roster: Optional[Roster],
                                   overall_timeout: float = PROBE_OVERALL_SECONDS) -> None:
        """并行向多个候选端点拉注册快照，**只等第一个成功的**。

        为什么不是"全部跑完再返回"：界面上这是一次点击，用户要的是尽快看到设备列表。
        实测（4 条一定连不上的端点）：串行 21.1s → 并行等全部 6.0s → 只等第一个成功
        0.0s（有一条可达时）。因此这里按"谁先成功就用谁"收尾，其余探测在
        `overall_timeout` 之后不再等待（线程是 daemon，未完成的会自己结束）。
        全部失败时最多等 `overall_timeout`，不会无限拖住界面。
        """
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        self._unreachable: Dict[Tuple[str, int], str] = {}
        if not candidates:
            return

        def probe(item: Tuple[Tuple[str, int], str]) -> bool:
            endpoint, _label = item
            try:
                self._fetch_registry_from(endpoint, identity, roster)
                return True
            except (RemoteError, TransportError, OSError) as e:
                self._unreachable[endpoint] = f'{endpoint[0]}:{endpoint[1]} 连不上: {e}'
            except Exception as e:   # 兜底：一个端点出意外不该拖垮整次刷新
                self._unreachable[endpoint] = f'{endpoint[0]}:{endpoint[1]} 失败: {e}'
            return False

        pool = ThreadPoolExecutor(max_workers=min(8, len(candidates)))
        try:
            pending = {pool.submit(probe, item) for item in candidates}
            deadline = time.monotonic() + overall_timeout
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
                if any(future.result() for future in done):
                    break   # 已经拿到一份快照：够了，别让用户继续等
        finally:
            # 不 shutdown(wait=True)：未完成的探测线程不该阻塞调用方
            pool.shutdown(wait=False)

    def _fetch_registry_from(self, endpoint: Tuple[str, int], identity: Identity,
                             roster: Optional[Roster]) -> int:
        """连过去拉一次注册快照并合并，返回采纳条数。"""
        connection = self._connect(endpoint, identity, roster)
        try:
            remote = mesh_client.fetch_registry(connection)
        finally:
            self._release(connection)
        with self._lock:
            # 并行探测时多个线程会同时合并：Registry.merge 的 seq 单调性判定必须
            # 串行化，否则同一设备的两条记录可能互相覆盖。
            accepted = self._registry.merge(remote)
            if accepted:
                try:
                    registry_mod.save_registry(self.identity_dir, self._registry)
                except OSError as e:
                    log.warning(f'[group-mesh] 注册表写盘失败: {e}')
        return accepted

    def _connect(self, endpoint: Tuple[str, int], identity: Identity,
                 roster: Optional[Roster], timeout: float = PROBE_TIMEOUT_SECONDS) -> Any:
        """取一条到 `endpoint` 的连接（复用或新建）。

        复用是必要的：一次 `Connection` 等于一次完整 Noise 握手，而"浏览一个目录"
        在界面上是多次请求（列共享、列目录、取缩略图）。连接失效时（对端重启、
        网络断开）丢弃重连一次 —— 只重连一次，不对着不可达地址反复重试。

        `timeout` 默认取"探测用"的短超时：设备可能同时发布 IPv6 与 IPv4 端点，而
        IPv6 在本机常常没有路由 —— 用内核默认的 20 秒去试，几条不可达端点就能把
        "刷新设备"拖到一分钟以上（实测 80 秒，界面一直停在"正在读取设备"）。
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
        connection = mesh_client.open_connection(endpoint[0], endpoint[1], identity, roster,
                                                timeout=timeout)
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
        """候选端点（设备**发布过的全部端点** + 手动登记的那些）。

        返回多个候选有两个来源，都需要：同一台设备可能同时发布 IPv6 与 IPv4
        （§4.6.1 把 v4 当可达性兜底），以及用户在引导期手工填过一个地址。
        第一个连不上时再试下一个；**只对连接失败重试** —— 权限或路径错误是确定性
        结论，换一台设备重试只会把真正的错误盖掉。
        """
        candidates: List[Tuple[Tuple[str, int], str]] = []
        names = self._peer_names(roster) if self._registry is not None else {}
        info = names.get(device_id) if device_id else None
        if info is not None:
            label = str(info.get('name') or device_id)
            for endpoint in self._peer_endpoints(info):
                candidates.append((endpoint, label))
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

        return self._fetch_to(destination, device_id, share_id, str(path))

    def _fetch_to(self, destination: Path, device_id: str, share_id: str,
                  path: str) -> Dict[str, Any]:
        """把对端共享项里的一个文件取到**指定路径**（不做越界校验，调用方负责）。

        为什么从 `download_remote` 里抽出来：按需取字节（`ensure_file`）要落到物化
        暂存目录，而用户显式下载要落到下载目录 —— 除了目标路径，连接、候选端点重试、
        "确定性错误不换设备重试"这些分支完全一样。各写一份的话两边迟早漂移
        （一边记得不重试、另一边忘了，真正的拒绝原因被"另一台也连不上"盖掉）。
        """
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

            try:
                size = destination.stat().st_size
            except OSError:
                # 落位成功但读不到（被杀毒/索引服务短暂占用等）：字节数还准，别把一次
                # 成功的取回报成失败。
                size = written
            return {'success': True, 'device_id': peer_id, 'peer_device_id': peer_id,
                    'name': label or connection.peer.name,
                    'share_id': share_id, 'path': path, 'local_path': str(destination),
                    'bytes': written, 'size': size}

        return {'success': False, 'error': last_error or '所有候选地址都连不上',
                'device_id': device_id, 'offline': True}

    # ── 上传：把一个本机文件投放到对端共享项 ──────────────────────────────
    #
    # 与取回对称，但方向相反：写权限由**对端**按 ACL 判定，落点也由对端决定。
    # 本机这一侧只做三件事：拒掉不该上传的来源、把相对路径的越界拒在连接之前、
    # 逐个尝试候选端点。

    def upload_remote(self, opts: Any = None, device_id: str = '', share_id: str = '',
                      local_path: str = '', remote_path: str = '',
                      overwrite: bool = False) -> Dict[str, Any]:
        """把本机文件上传到对端共享项（需要该共享项授予本机主体 `write`）。

        `remote_path` 留空时取本地文件名；可以带子目录（`sub/a.bin`），对端会自动建目录。
        目标已存在时默认**拒绝**，要覆盖必须显式 `overwrite=True` —— 写权限是可加的
        （设计文档 §6.2），覆盖等于让属主少一份内容，不能由上传方默认决定。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '')
        share_id = str(options.get('share_id') or share_id or '')
        local_path = str(options.get('local_path') or local_path or '')
        remote_path = str(options.get('remote_path') or remote_path or '')
        overwrite = bool(options.get('overwrite', overwrite))

        if not share_id:
            return {'success': False, 'error': '必须给出 share_id'}
        if not local_path:
            return {'success': False, 'error': '必须给出要上传的本机文件路径'}

        source = Path(local_path).expanduser()
        if not source.is_file():
            return {'success': False, 'error': f'本机文件不存在: {source}'}
        # 两类不许上传的来源：身份目录（明文私钥）与远端物化缓存（那是**其他成员**
        # 的数据，传出去等于二次分发，见 .dsh/group-mesh-materialize.md §2.2）。
        resolved = source.resolve()
        for guarded in (self.identity_dir, self.remote_cache_dir, self.staging_dir):
            try:
                resolved.relative_to(guarded.resolve())
            except ValueError:
                continue
            return {'success': False, 'error': f'不允许上传 {guarded} 之内的文件'}

        target = remote_path.strip().replace('\\', '/') or source.name
        if target.startswith('/') or target.endswith('/') \
                or any(part == '..' for part in Path(target).parts):
            return {'success': False, 'error': f'目标路径非法: {target!r}'}

        try:
            size = source.stat().st_size
        except OSError as e:
            return {'success': False, 'error': f'读不到本机文件: {e}'}

        task: Dict[str, Any] = {
            'task_id': uuid.uuid4().hex[:12],
            'state': 'running',
            'device_id': device_id,
            'share_id': share_id,
            'local_path': str(source),
            'remote_path': target,
            'overwrite': overwrite,
            'total': size,
            'sent': 0,
            'resumed_from': 0,
            'peer_device_id': '',
            'peer_name': '',
            'error': '',
            'started_at': int(time.time()),
            'finished_at': None,
            'cancel': threading.Event(),
        }
        self._register_upload(task)
        threading.Thread(target=self._upload_worker, args=(task,), daemon=True,
                         name=f'group-mesh-upload-{task["task_id"]}').start()
        return {'success': True, 'task_id': task['task_id'], 'state': 'running',
                'size': size, 'share_id': share_id, 'path': target,
                'local_path': str(source)}

    # ── 上传任务表 ────────────────────────────────────────────────────────

    def _register_upload(self, task: Dict[str, Any]) -> None:
        """登记任务，并只保留最近 `UPLOAD_KEEP` 个已结束的（界面轮询要看结果）。"""
        with self._uploads_guard:
            self._uploads[task['task_id']] = task
            self._upload_order.append(task['task_id'])
            while len(self._upload_order) > UPLOAD_KEEP:
                oldest = self._upload_order[0]
                if self._uploads.get(oldest, {}).get('state') in ('running', 'cancelling'):
                    break            # 还在传的不淘汰
                self._upload_order.pop(0)
                self._uploads.pop(oldest, None)

    def _upload_view(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """给界面看的副本：Event / 内部字段不能进 JSON。"""
        total = int(task.get('total') or 0)
        sent = int(task.get('sent') or 0)
        view = {key: task.get(key) for key in
                ('task_id', 'state', 'device_id', 'share_id', 'local_path', 'remote_path',
                 'overwrite', 'total', 'sent', 'resumed_from', 'peer_device_id', 'peer_name',
                 'error', 'started_at', 'finished_at')}
        view['percent'] = int(sent * 100 / total) if total else 100
        return view

    def upload_status(self, opts: Any = None, task_id: str = '') -> Dict[str, Any]:
        """上传进度：不传 `task_id` 时返回本次运行的全部任务（新的在前）。"""
        options = _opts(opts)
        task_id = str(options.get('task_id') or task_id or '')
        with self._uploads_guard:
            if task_id:
                task = self._uploads.get(task_id)
                if task is None:
                    return {'success': False, 'error': f'没有这个上传任务: {task_id}'}
                return {'success': True, 'task': self._upload_view(task)}
            tasks = [self._upload_view(self._uploads[t]) for t in reversed(self._upload_order)
                     if t in self._uploads]
        return {'success': True, 'tasks': tasks}

    def cancel_upload(self, opts: Any = None, task_id: str = '') -> Dict[str, Any]:
        """取消一次上传。

        取消在**分块边界**生效：已经传给对端的字节留在 `<目标>.part` 里，不清除 ——
        它就是下次续传的起点（协议里没有远程删除，这也是刻意的）。因此取消的代价
        最多是"少传一点"，不是"白传一遍"。
        """
        options = _opts(opts)
        task_id = str(options.get('task_id') or task_id or '')
        if not task_id:
            return {'success': False, 'error': '必须给出 task_id'}
        with self._uploads_guard:
            task = self._uploads.get(task_id)
            if task is None:
                return {'success': False, 'error': f'没有这个上传任务: {task_id}'}
            if task['state'] in ('done', 'failed', 'cancelled'):
                return {'success': False, 'state': task['state'],
                        'error': f'任务已经结束（{task["state"]}）'}
            task['state'] = 'cancelling'
            task['cancel'].set()
            view = self._upload_view(task)
        return {'success': True, 'task': view}

    # ── 续传记录（本机） ──────────────────────────────────────────────────

    def _upload_record_file(self) -> Path:
        return self.get_data_root() / 'uploads.json'

    def _upload_key(self, device_id: str, share_id: str, remote_path: str,
                    local_path: str) -> str:
        return '|'.join((device_id, share_id, remote_path, local_path))

    def _load_upload_records(self) -> Dict[str, Any]:
        path = self._upload_record_file()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_upload_record(self, key: str, size: int, mtime_ns: int, sent: int) -> None:
        """记下"这个本地文件传到哪了"，供下次续传判断是不是同一份内容的前缀。"""
        with self._uploads_guard:
            records = self._load_upload_records()
            records[key] = {'size': size, 'mtime_ns': mtime_ns, 'sent': sent,
                            'ts': int(time.time())}
            self._write_upload_records(records)

    def _clear_upload_record(self, key: str) -> None:
        with self._uploads_guard:
            records = self._load_upload_records()
            if records.pop(key, None) is not None:
                self._write_upload_records(records)

    def _write_upload_records(self, records: Dict[str, Any]) -> None:
        try:
            self.get_data_root().mkdir(parents=True, exist_ok=True)
            self._upload_record_file().write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as e:
            log.warning(f'[group-mesh] 续传记录写盘失败: {e}')

    def _resume_offset(self, key: str, source: Path, staged: int) -> int:
        """决定这次从哪儿开始传。

        三个条件缺一不可：本机有记录、记录与**当前**本地文件的 (大小, mtime) 一致、
        对端暂存字节数与记录一致。任何一个不符都从头传 —— 拿一份对不上的 `.part`
        去续，产出的是拼接垃圾，而且没人会发现。
        """
        if staged <= 0:
            return 0
        record = self._load_upload_records().get(key)
        if not isinstance(record, dict):
            return 0
        size, mtime_ns = self._local_stamp(source)
        if record.get('size') != size or record.get('mtime_ns') != mtime_ns:
            return 0
        if record.get('sent') != staged or staged >= size:
            return 0
        return staged

    def _local_stamp(self, source: Path) -> tuple:
        """本地文件的 (大小, mtime_ns)：续传时必须与记录一致，否则从头传。"""
        try:
            stat = source.stat()
        except OSError:
            return 0, 0
        return stat.st_size, stat.st_mtime_ns

    # ── 上传线程 ──────────────────────────────────────────────────────────

    def _upload_worker(self, task: Dict[str, Any]) -> None:
        """后台把文件推上去：续传起点 → 分块 → 提交；进度写回 task。

        用**专用连接**（`open_connection`，不进复用池）：复用池里的连接会被界面线程
        同时用于列目录等请求，而一条 Noise 连接上的请求是严格串行的，两个线程交错
        发送会把帧拼坏。用完即关，代价只有一次握手。
        """
        key = self._upload_key(task['device_id'], task['share_id'], task['remote_path'],
                               task['local_path'])
        source = Path(task['local_path'])
        connection = None
        try:
            identity = self._load_identity()
            roster = self._load_roster()
            if identity is None or roster is None:
                self._fail_upload(task, '需要先创建身份与团体')
                return
            candidates = self._manual_endpoints(task['device_id'], roster)
            if not candidates:
                self._fail_upload(task, '还没有可用的对端地址。请先在"对端"里登记一台设备的'
                                        '地址，或刷新一次让本机通过注册记录发现它')
                return

            last_error = ''
            for endpoint, label in candidates:
                if task['cancel'].is_set():
                    self._finish_upload(task, 'cancelled')
                    self._save_upload_record(key, *self._local_stamp(source), task['sent'])
                    return
                try:
                    connection = mesh_client.open_connection(
                        endpoint[0], endpoint[1], identity, roster,
                        timeout=UPLOAD_TIMEOUT_SECONDS)
                except (RemoteError, TransportError, OSError) as e:
                    last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                    continue
                task['peer_device_id'] = connection.peer.device_key.hex()
                task['peer_name'] = label or connection.peer.name

                try:
                    probe = mesh_client.probe_upload(connection, task['share_id'],
                                                     task['remote_path'])
                except RemoteError as e:
                    self._fail_upload(task, str(e))
                    return

                offset = self._resume_offset(key, source, int(probe.get('staged') or 0))
                task['resumed_from'] = offset
                task['sent'] = offset
                task['saved'] = offset

                def on_progress(sent: int, total: int) -> None:
                    task['sent'] = sent
                    task['total'] = total
                    # 每 4 MiB 落一次盘：断在"进程被杀"上时也能续，而每分块写一次
                    # JSON 会把时间都花在无关的 IO 上。
                    if sent - int(task.get('saved') or 0) >= 4 * 1024 * 1024:
                        task['saved'] = sent
                        self._save_upload_record(key, *self._local_stamp(source), sent)

                try:
                    sent = mesh_client.push_file(
                        connection, task['share_id'], source, task['remote_path'],
                        overwrite=task['overwrite'], offset=offset,
                        progress=on_progress, cancelled=task['cancel'].is_set)
                except mesh_client.UploadCancelled:
                    self._finish_upload(task, 'cancelled')
                    self._save_upload_record(key, *self._local_stamp(source), task['sent'])
                    return
                except RemoteError as e:
                    # 无权限 / 已存在 / 超配额 / 越界 / 游标不符：对端的确定性拒绝
                    self._fail_upload(task, str(e))
                    return
                except (TransportError, OSError) as e:
                    last_error = f'传输失败: {e}'
                    # 传输断了但字节留在了对端：记下进度，下次续传
                    self._save_upload_record(key, *self._local_stamp(source), task['sent'])
                    continue
                finally:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    connection = None

                task['sent'] = sent
                self._clear_upload_record(key)
                self._finish_upload(task, 'done')
                return

            self._fail_upload(task, last_error or '所有候选地址都连不上', offline=True)
        except Exception as e:      # 兜底：后台线程的异常不能让任务永远停在 running
            self._fail_upload(task, f'{type(e).__name__}: {e}')
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _finish_upload(self, task: Dict[str, Any], state: str) -> None:
        with self._uploads_guard:
            task['state'] = state
            task['finished_at'] = int(time.time())

    def _fail_upload(self, task: Dict[str, Any], error: str, offline: bool = False) -> None:
        with self._uploads_guard:
            task['state'] = 'failed'
            task['error'] = error
            task['offline'] = bool(offline)
            task['finished_at'] = int(time.time())

    def _cancel_all_uploads(self) -> None:
        with self._uploads_guard:
            running = [t for t in self._uploads.values()
                       if t['state'] in ('running', 'cancelling')]
        for task in running:
            task['cancel'].set()

    # ── 远端：物化与按需取字节 ────────────────────────────────────────────
    # 「物化」= 在本地 `.cache/remote/<设备ID>/<共享标识>/…` 造出与远端同形的目录树：
    # 目录真的建出来、文件先放 0 字节占位，真实大小记在 <共享标识>.index.json 里；
    # 字节在**第一次真去读这个文件**时才取回（由 `ensure_file` 钩子驱动）。
    #
    # 为什么必须这样分两步：消费方插件（image-viewer / media-player 等）是按
    # "本地路径"工作的 —— 它们 `os.scandir` 建索引、按路径取字节。远端目录如果
    # 不落地，它们连"有这个文件"都看不到；而如果订阅时就把字节全拉下来，看一个
    # 共享相册要先下几百张图。

    def _remote_index_file(self, device_id: str, share_id: str) -> Path:
        return self.remote_cache_dir / device_id / f'{share_id}.index.json'

    def _load_remote_index(self, device_id: str, share_id: str) -> Dict[str, Any]:
        path = self._remote_index_file(device_id, share_id)
        if not path.is_file():
            return {'share': share_id, 'device': device_id, 'entries': {}}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'share': share_id, 'device': device_id, 'entries': {}}
        if not isinstance(data.get('entries'), dict):
            data['entries'] = {}
        return data

    def _cached_remote_index(self, device_id: str, share_id: str) -> Dict[str, Any]:
        """带缓存的索引读取。

        `is_content_placeholder()` 会在**每个** `/file` 请求上被调用，而索引文件
        可能不小（几千条就是几百 KB），逐次解析会让每个文件请求都多一次磁盘 I/O +
        JSON 解析。缓存的失效点有三处：物化（重写索引）、取字节成功、清理缓存 ——
        所以只在这三处清空，不做 TTL。
        """
        key = (device_id, share_id)
        cached = self._remote_index_cache.get(key)
        if cached is not None:
            return cached
        index = self._load_remote_index(device_id, share_id)
        self._remote_index_cache[key] = index
        # 简单的上界，免得浏览很多共享项后缓存无限增长
        if len(self._remote_index_cache) > 32:
            self._remote_index_cache.pop(next(iter(self._remote_index_cache)))
        return index

    def _invalidate_remote_index(self, device_id: str = '', share_id: str = '') -> None:
        if not device_id:
            self._remote_index_cache.clear()
            return
        if not share_id:
            for key in [k for k in self._remote_index_cache if k[0] == device_id]:
                del self._remote_index_cache[key]
            return
        self._remote_index_cache.pop((device_id, share_id), None)

    def _save_remote_index(self, device_id: str, share_id: str, index: Dict[str, Any]) -> None:
        path = self._remote_index_file(device_id, share_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def materialize_remote(self, opts: Any = None, device_id: str = '', share_id: str = '',
                           path: str = '.', max_depth: int = 4,
                           max_entries: int = 5000) -> Dict[str, Any]:
        """把对端共享项在本地物化成目录树（目录 + 0 字节占位文件 + 索引）。

        限制深度与条目数是刻意的：远端可能是一个几十万文件的目录，全量遍历既慢又会
        把内存和索引文件撑爆。默认 4 层 / 5000 条，调用方可传更大的值。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
        share_id = str(options.get('share_id') or share_id or '')
        try:
            max_depth = int(options.get('max_depth', max_depth))
            max_entries = int(options.get('max_entries', max_entries))
        except (TypeError, ValueError):
            return {'success': False, 'error': 'max_depth / max_entries 必须是整数'}
        if not share_id:
            return {'success': False, 'error': '必须给出 share_id'}

        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}
        candidates = self._manual_endpoints(device_id, roster)
        if not candidates:
            return {'success': False, 'error': '还没有可用的对端地址（见"添加对端"）'}

        last_error = ''
        for endpoint, label in candidates:
            try:
                connection = self._connect(endpoint, identity, roster)
            except (RemoteError, TransportError, OSError) as e:
                last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                continue
            peer_id = connection.peer.device_key.hex()
            # 物化目录必须按**真实设备 ID**建：`ensure_file()` 之后要从路径反解出
            # 设备 ID 去取字节，若这里写成调用方传入的（可能为空）就会落到别的目录，
            # 取字节时找不到对应共享项。
            root = self.remote_cache_dir / peer_id / share_id
            index = self._load_remote_index(peer_id, share_id)
            previous: Dict[str, Any] = dict(index.get('entries') or {})
            try:
                walked = self._walk_remote(connection, share_id, path, max_depth, max_entries)
            except RemoteError as e:
                return {'success': False, 'error': str(e), 'device_id': peer_id,
                        'share_id': share_id, 'path': path}
            except (TransportError, OSError) as e:
                last_error = f'遍历中断: {e}'
                continue
            finally:
                self._release(connection)

            # 落盘：目录建出来，文件写 0 字节占位。
            # 占位而不是"什么都不建"：`os.scandir` 能看到条目、消费方插件的索引因此
            # 完整；`ensure_file` 用"实际大小 != 索引里的真实大小"判定哪些还没取回。
            created_dirs = 0
            created_files = 0
            invalidated = 0
            entries: Dict[str, Any] = {}
            for rel, info in walked.items():
                target = root / rel
                if info['dir']:
                    target.mkdir(parents=True, exist_ok=True)
                    created_dirs += 1
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    old = previous.get(rel)
                    # 对端的内容换过了（大小或 mtime 变了）：本机若已经把**真字节**
                    # 取回来了，必须丢掉它 —— 否则"同大小的替换"会永远读到旧内容，
                    # 而缩略图/列表缓存又都以本地 (size, mtime) 为键，看不出任何异常。
                    # 丢掉之后它重新变回占位文件，下次读取按需取回。
                    if old is not None and self._content_changed(old, info) \
                            and self._is_fetched(target):
                        target.unlink()
                        invalidated += 1
                    if not target.is_file():
                        target.touch()
                        created_files += 1
                    # 占位与已取回的文件都写成对端的 mtime：消费方的缓存失效读的是
                    # **本地**元数据，留着"物化时刻"的时间会让"对端换了图"看起来没变。
                    self._apply_remote_mtime(target, info.get('mtime_ns'))
                entries[rel] = {'dir': info['dir'], 'size': info['size'],
                                'mtime_ns': info.get('mtime_ns')}

            truncated = len(walked) >= max_entries
            # 对账（对端已删/改名 → 本地也删）**只在遍历完整时做**：达到条目上限时
            # `walked` 只是一个子集，照着它删会把没遍历到的内容整片抹掉。
            removed = 0 if truncated else self._prune_absent(root, previous, entries)

            index.update({'share': share_id, 'device': peer_id, 'entries': entries,
                          'root': str(root),
                          'materialized_at': int(time.time()),
                          'truncated': truncated})
            self._save_remote_index(peer_id, share_id, index)
            self._invalidate_remote_index(peer_id, share_id)
            return {'success': True, 'device_id': peer_id, 'name': label,
                    'share_id': share_id, 'root': str(root),
                    'dirs': created_dirs, 'files': created_files,
                    'invalidated': invalidated, 'removed': removed,
                    'entries': len(entries), 'truncated': truncated}

        return {'success': False, 'error': last_error or '所有候选地址都连不上',
                'device_id': device_id, 'offline': True}

    @staticmethod
    def _content_changed(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
        """对端这一条的内容是否变了（大小或 mtime 任一不同）。

        对端没给 mtime（老内核）时只看大小：这正是加入 mtime 之前的能力，
        不假装能发现同大小的替换。
        """
        if int(old.get('size') or 0) != int(new.get('size') or 0):
            return True
        old_mtime, new_mtime = old.get('mtime_ns'), new.get('mtime_ns')
        if old_mtime is None or new_mtime is None:
            return False
        return int(old_mtime) != int(new_mtime)

    @staticmethod
    def _is_fetched(target: Path) -> bool:
        """本机这个物化条目是否已经持有**真字节**（而不是 0 字节占位）。

        判据只有"大小非 0"：0 字节的远端文件本来就永远停在占位状态（`ensure_file`
        对 remote_size==0 直接返回，见 `.dsh/group-mesh-materialize.md` §2.2），
        对它做"丢弃重取"没有任何意义。
        """
        try:
            return target.is_file() and target.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _apply_remote_mtime(target: Path, mtime_ns: Optional[int]) -> None:
        """把对端的 mtime 写到本地这份文件上（占位或已取回的真字节都要写）。

        写不进去不是错误：老对端没有该字段、文件系统可能不支持、值可能越界。
        这里**不**回读比对 —— 全仓的远端判定只比较"对端报过的值"与"对端现在报的值"，
        从不拿本地 stat 去比远端值（本地文件系统会量化，比了只会永远不相等）。
        """
        if mtime_ns is None:
            return
        try:
            os.utime(target, ns=(mtime_ns, mtime_ns))
        except (OSError, OverflowError, ValueError) as e:
            log.info(f'[group-mesh] 无法把对端 mtime 写到 {target}: {e}')

    def _prune_absent(self, root: Path, previous: Dict[str, Any],
                      keep: Dict[str, Any]) -> int:
        """删掉"索引里有、本次遍历里没有"的条目，返回删掉的文件数。

        改动前这里只增不减：对端删掉或改名之后，本地那份会永远留着（界面上表现为
        "对方已经删了的文件我还看得到"）。目录按深度倒序处理，先删文件再删因此变空的
        目录；非空目录不删（里面还可能有本次没遍历到的内容）。
        """
        removed = 0
        gone = sorted(set(previous) - set(keep), key=lambda rel: rel.count('/'), reverse=True)
        for rel in gone:
            target = root / rel
            try:
                if target.is_dir():
                    target.rmdir()
                elif target.exists():
                    target.unlink()
                    removed += 1
            except OSError:
                # 目录非空、或已被并发的取字节线程删掉：都不是错误
                continue
        return removed

    def _walk_remote(self, connection: Any, share_id: str, path: str,
                     max_depth: int, max_entries: int) -> Dict[str, Dict[str, Any]]:
        """广度优先遍历远端目录，返回 `{相对路径: {dir, size, mtime_ns}}`（不含根自身）。

        `mtime_ns` 是对端 `stat` 的真实值（纳秒整数），也是本机判定"对端换过没有"
        的**唯一依据**：本机自己那份 mtime 会被文件系统量化，拿它去比远端值只会
        得出"永远不相等"。老对端不带该字段时为 None。
        """
        import collections

        found: Dict[str, Dict[str, Any]] = {}
        pending = collections.deque([(path or '.', 0)])
        while pending:
            current, depth = pending.popleft()
            result = mesh_client.list_directory(connection, share_id, current)
            if not result.get('dir'):
                # 调用方给的是个文件路径：直接当作单个条目
                name = str(result.get('path') or current).lstrip('./')
                found[name] = {'dir': False, 'size': int(result.get('size') or 0),
                               'mtime_ns': _mtime_ns(result)}
                continue
            for entry in result.get('entries') or []:
                name = str(entry.get('name') or '')
                if not name or name in ('.', '..'):
                    continue
                rel = name if current in ('.', '') else f"{current.strip('/')}/{name}"
                if rel in found:
                    continue
                if len(found) >= max_entries:
                    return found
                found[rel] = {'dir': bool(entry.get('dir')),
                              'size': int(entry.get('size') or 0),
                              'mtime_ns': _mtime_ns(entry)}
                if entry.get('dir') and depth + 1 < max_depth:
                    pending.append((rel, depth + 1))
        return found

    def mirror_share(self, opts: Any = None, device_id: str = '', share_id: str = '',
                     destination: str = '', max_entries: int = 20000,
                     max_bytes: int = 0) -> Dict[str, Any]:
        """把一个共享项**完整取到用户指定的本地目录**（"网络位置"用）。

        与 `materialize_remote()` 的三点区别，每一点都是"网络位置"能不能用的前提：

        1. **目标目录由用户指定**（在他自己的图库/媒体盘里），不是插件的 `.cache`；
        2. **字节全部取回，不留占位**：指定目录是普通目录，消费方（image-viewer 等）按
           本地文件工作、**没有** `ensure_file` 钩子可依赖 —— 留 0 字节占位就是宽高 0×0
           与整片 404 缩略图（实测见 `.dsh/group-mesh-materialize.md` §2.2）；
        3. **mtime 写成对端的值**：消费方的缓存失效读的是本地元数据，否则"对端换过"
           永远看不出来。

        幂等：大小与 mtime 都一致的文件直接跳过，所以重复执行只补差异。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
        share_id = str(options.get('share_id') or share_id or '')
        destination = str(options.get('destination') or destination or '')
        try:
            max_entries = int(options.get('max_entries', max_entries))
            max_bytes = int(options.get('max_bytes', max_bytes))
        except (TypeError, ValueError):
            return {'success': False, 'error': 'max_entries / max_bytes 必须是整数'}
        if not share_id or not destination:
            return {'success': False, 'error': '必须给出 share_id 与 destination'}

        raw_path = Path(destination).expanduser()
        # 必须是**绝对**路径：`C:Users...`（Windows 驱动器相对路径）与相对路径都会在
        # resolve() 之后变成某个"看起来正常"的位置，然后就在那里建目录 —— 实测踩到过
        # 一次分隔符被吃光的路径（`C:UsersADMINI~1AppDataLocalTemp...`），resolve 后
        # 落在盘符根上。校验必须在 resolve **之前**做，否则拿到的是已经被解析过的路径。
        if not raw_path.is_absolute():
            return {'success': False,
                    'error': f'目标目录必须是绝对路径（形如 D:\\图库\\相册 或 /home/me/pics）: '
                             f'{destination!r}'}
        try:
            root = raw_path.resolve()
        except OSError as e:
            return {'success': False, 'error': f'目标目录无法解析: {e}'}
        # 边界只挡**本插件自己的**数据目录与身份目录，不挡全局数据根：默认配置下
        # 全局数据根（`./data`）正是 image-viewer 的图片库，用户把"网络位置"放进
        # 自己的图库是完全正当的用法（那样它就是一个普通文件夹，原图也能直接打开）。
        # `add_share` 那条"不得位于数据根之内"是另一个问题（对外**暴露**文件），
        # 不要照抄到这里 —— 照抄会让默认配置下最自然的目标目录被拒。
        forbidden = [self.get_data_root(), self.identity_dir]
        for guarded in forbidden:
            try:
                root.relative_to(guarded)
            except ValueError:
                continue
            return {'success': False,
                    'error': f'目标目录不得位于 {guarded} 之内（程序数据与身份私钥）'}
        if self.is_protected_path(root):
            return {'success': False, 'error': f'目标目录是受保护路径: {root}'}

        # 目录可以不存在：用户很自然会说"放到图库下这个新文件夹里"。**但只有上级目录
        # 存在时才建** —— 否则一个手滑/粘贴丢了分隔符的路径会在盘符根上造出一个莫名其妙的
        # 目录（实测踩到：粘贴丢反斜杠后报"目标目录不存在"，用户完全看不出哪里错了）。
        if not root.is_dir():
            if not root.parent.is_dir():
                return {'success': False,
                        'error': f'目标目录不存在，且它的上级目录也不存在: {root}\n'
                                 f'（请用「浏览…」选一个已存在的目录，或先创建上级目录 '
                                 f'{root.parent}）'}
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {'success': False, 'error': f'无法创建目标目录 {root}: {e}'}

        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}
        candidates = self._manual_endpoints(device_id, roster)
        if not candidates:
            return {'success': False, 'error': '还没有可用的对端地址（见"添加对端"）'}

        last_error = ''
        for endpoint, label in candidates:
            try:
                connection = self._connect(endpoint, identity, roster)
            except (RemoteError, TransportError, OSError) as e:
                last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                continue
            try:
                walked = self._walk_remote(connection, share_id, '.', 64, max_entries)
                result = self._mirror_files(connection, share_id, root, walked, max_bytes)
            except RemoteError as e:
                return {'success': False, 'error': str(e), 'share_id': share_id}
            except (TransportError, OSError) as e:
                last_error = f'传输中断: {e}'
                continue
            finally:
                self._release(connection)
            device = connection.peer.device_key.hex()
            return {'success': True, 'device_id': device, 'name': label or connection.peer.name,
                    'share_id': share_id, 'root': str(root), 'entries': len(walked),
                    'truncated': len(walked) >= max_entries, **result}

        return {'success': False, 'error': last_error or '所有候选地址都连不上',
                'device_id': device_id, 'offline': True}

    def _mirror_files(self, connection: Any, share_id: str, root: Path,
                      walked: Dict[str, Dict[str, Any]], max_bytes: int) -> Dict[str, Any]:
        """把 `walked` 里的文件逐个取到 `root`（增量：一致就跳过），返回统计。

        连接由调用方持有并复用：一次取几十上百个文件时，逐文件建连会把"一次拷贝"
        变成几十次 Noise 握手。单个文件失败不中断整批 —— 汇报出来比整个失败有用。
        """
        created_dirs = 0
        fetched = 0
        unchanged = 0
        written_bytes = 0
        truncated = False
        errors: List[str] = []
        for rel, info in sorted(walked.items()):
            target = root / rel
            if info['dir']:
                try:
                    target.mkdir(parents=True, exist_ok=True)
                    created_dirs += 1
                except OSError as e:
                    errors.append(f'{rel}: {e}')
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                errors.append(f'{rel}: {e}')
                continue
            if self._mirror_file_current(target, info):
                unchanged += 1
                continue
            if max_bytes and written_bytes + int(info['size']) > max_bytes:
                truncated = True
                continue
            try:
                written_bytes += mesh_client.fetch_to_file(connection, share_id, rel, target)
            except (RemoteError, TransportError, OSError) as e:
                errors.append(f'{rel}: {e}')
                continue
            # 真字节到位后再写 mtime：写入本身会把 mtime 刷成"现在"
            self._apply_remote_mtime(target, info.get('mtime_ns'))
            fetched += 1
        return {'dirs': created_dirs, 'fetched': fetched, 'unchanged': unchanged,
                'bytes': written_bytes, 'truncated_bytes': truncated,
                'errors': errors[:20], 'error_count': len(errors)}

    def _mirror_file_current(self, target: Path, info: Dict[str, Any]) -> bool:
        """镜像目标是否已经与对端一致（大小相同，且 mtime 相同或对端没给 mtime）。

        mtime 用窗口比较：本地文件系统会量化 `os.utime` 写下的纳秒值（FAT 只有 2 秒），
        要求逐 ns 相等会把"本来就是同一份"的文件判成需要重下，每次执行都全量重传。
        """
        try:
            local = target.stat()
        except OSError:
            return False
        if local.st_size != int(info.get('size') or 0):
            return False
        remote_mtime = info.get('mtime_ns')
        if remote_mtime is None:
            return True   # 对端没给 mtime（老内核）：只能靠大小
        return abs(local.st_mtime_ns - int(remote_mtime)) < MIRROR_MTIME_WINDOW_NS

    def is_content_placeholder(self, path: Any) -> bool:
        """该路径是否"已物化但字节还没取回"（`/file` 每次请求都会问）。

        判定依据是"实际大小 != 索引里的远端大小"：物化时写下的是 0 字节占位，
        而索引里记的是远端真实大小。必须廉价 —— 一次 `stat` 加路径归属判断，
        索引走进程内缓存。

        **刻意不在这里比对 mtime**：拿本地文件的 mtime 去比索引里对端的 mtime，
        会因为文件系统的量化（`os.utime` 写进去的 ns 未必原样存下）而永远不相等，
        于是每个请求都触发一次取回。"对端换过内容"的发现点只有一个 ——
        `materialize_remote()` 的对账，那里比较的两个值**都来自对端**。
        """
        try:
            target = Path(path)
            rel = target.resolve().relative_to(self.remote_cache_dir.resolve())
        except (OSError, ValueError):
            return False
        parts = rel.parts
        if len(parts) < 3:
            return False
        index = self._cached_remote_index(parts[0], parts[1])
        meta = (index.get('entries') or {}).get('/'.join(parts[2:]))
        if not isinstance(meta, dict) or meta.get('dir'):
            return False
        remote_size = int(meta.get('size') or 0)
        if remote_size <= 0:
            return False   # 远端本来就是空文件：不必取
        try:
            return target.stat().st_size != remote_size
        except OSError:
            return True

    def ensure_file(self, path: Any) -> None:
        """`/file` 找不到内容时由 Shell 回调：把远端对应的那个文件取回本地。

        判定"这是不是一个还没取回的占位文件"：拿实际大小与索引里的真实大小比。
        占位文件是 0 字节，而索引里的真实大小来自远端 —— 两者不一致（或文件不存在）
        就取一次。

        **同大小的替换不在本方法里发现**：那需要"对端现在报的值"与"索引里的值"对比，
        而本方法只看本机磁盘。它由 `materialize_remote()` 的对账负责（mtime 变了就丢掉
        本地真字节，于是下次读到这里时本方法自然会重新取回）。
        """
        try:
            target = Path(path)
            rel = target.resolve().relative_to(self.remote_cache_dir.resolve())
        except (OSError, ValueError):
            return   # 不是远端缓存里的东西：本钩子不管
        parts = rel.parts
        if len(parts) < 3:
            return   # 期望 <设备ID>/<共享标识>/<相对路径…>
        device_id, share_id = parts[0], parts[1]
        relative = '/'.join(parts[2:])
        index = self._cached_remote_index(device_id, share_id)
        meta = (index.get('entries') or {}).get(relative)
        if not isinstance(meta, dict) or meta.get('dir'):
            return
        remote_size = int(meta.get('size') or 0)
        try:
            if target.is_file() and target.stat().st_size == remote_size:
                return   # 已经取回过了（大小一致）
        except OSError:
            pass
        if remote_size > self.max_fetch_bytes:
            log.info(f'[group-mesh] 跳过按需取回：{relative} 大小 {remote_size} 超过上限 '
                     f'{self.max_fetch_bytes}')
            return

        # 并发去重。Flask 是 threaded=True，浏览器一次会并发多个请求；内核的
        # `fetch_to_file` 用的是**按目标路径推导**的 `.part` 临时名，两个线程为同一个
        # 路径同时取字节就会撞在同一个临时文件上（交错写入 → 内容损坏）。
        # 锁内复核一次"是不是已经被别人取回了"，重复传输也一并省掉。
        with self._fetch_lock(device_id, share_id, relative):
            try:
                if target.is_file() and target.stat().st_size == remote_size:
                    return
            except OSError:
                pass
            staging = self.staging_dir / device_id / share_id / relative
            result = self._fetch_to(staging, device_id, share_id, relative)
            if not result.get('success'):
                self._discard_staging(staging)
                log.info(f'[group-mesh] 按需取回失败 {relative}: {result.get("error")}')
                return
            # 暂存文件与物化目标同在 `.cache` 下（同卷由构造保证），因此 os.replace 是
            # 原子的；不再经过用户下载目录（改动前那里会被浏览行为污染）。
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, target)
                self._apply_remote_mtime(target, meta.get('mtime_ns'))
                self._invalidate_remote_index(device_id, share_id)
            except OSError as e:
                self._discard_staging(staging)
                log.warning(f'[group-mesh] 物化落位失败 {relative}: {e}')

    def _fetch_lock(self, device_id: str, share_id: str, relative: str) -> threading.Lock:
        """取 (设备ID, 共享标识, 相对路径) 对应的一把锁（进程内）。

        锁对象不做回收：条目数就是"被并发读过的远端文件数"，与物化缓存同量级；
        回收需要引用计数，漏一处就退化成"锁静默失效"，不值得。
        """
        key = (device_id, share_id, relative)
        with self._fetch_locks_guard:
            lock = self._fetch_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._fetch_locks[key] = lock
            return lock

    @staticmethod
    def _discard_staging(staging: Path) -> None:
        """清掉失败的暂存文件（`fetch_to_file` 失败时会留下 `<目标>.part`）。"""
        for candidate in (staging, staging.with_suffix(staging.suffix + '.part')):
            try:
                candidate.unlink()
            except OSError:
                continue

    @property
    def max_fetch_bytes(self) -> int:
        """按需取回的单文件上限，避免一次浏览把磁盘塞满。"""
        try:
            configured = int(self.setting('max_fetch_mb', 0) or 0)
        except (TypeError, ValueError):
            configured = 0
        return configured * 1024 * 1024 if configured > 0 else DEFAULT_MAX_FETCH_BYTES

    def remote_cache(self) -> Dict[str, Any]:
        """已物化的远端内容一览（界面显示用了多少磁盘、可以清哪些）。"""
        root = self.remote_cache_dir
        items: List[Dict[str, Any]] = []
        if root.is_dir():
            for device_dir in sorted(root.iterdir()):
                if not device_dir.is_dir():
                    continue
                for share_dir in sorted(device_dir.iterdir()):
                    if not share_dir.is_dir():
                        continue
                    index = self._load_remote_index(device_dir.name, share_dir.name)
                    entries = index.get('entries') or {}
                    files = [rel for rel, meta in entries.items() if not meta.get('dir')]
                    fetched = 0
                    for rel in files:
                        candidate = share_dir / rel
                        try:
                            meta_size = int((entries.get(rel) or {}).get('size') or 0)
                            if candidate.is_file() and meta_size and \
                                    candidate.stat().st_size == meta_size:
                                fetched += 1
                        except OSError:
                            continue
                    items.append({
                        'device_id': device_dir.name,
                        'share_id': share_dir.name,
                        'entries': len(entries),
                        'files': len(files),
                        'fetched': fetched,
                        'pending': len(files) - fetched,
                        'bytes': self._tree_usage(share_dir)[0],
                        'materialized_at': index.get('materialized_at'),
                        'truncated': bool(index.get('truncated')),
                        'root': str(share_dir),
                    })
        return {'success': True, 'root': str(root), 'items': items,
                'total_bytes': self._tree_usage(root)[0] if root.is_dir() else 0}

    def clear_remote_cache(self, opts: Any = None, device_id: str = '',
                           share_id: str = '') -> Dict[str, Any]:
        """删除已物化的远端内容（磁盘回收）。不传参数时清空全部。

        暂存目录（`staging`）一起清：它是取字节的中转区，失败的取回会留下
        `<目标>.part`，不属于任何一份物化内容，留着就是无人回收的垃圾。
        """
        import shutil

        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
        share_id = str(options.get('share_id') or share_id or '')
        root = self.remote_cache_dir
        freed = 0
        if root.exists():
            if device_id and share_id:
                targets = [root / device_id / share_id]
            elif device_id:
                targets = [root / device_id]
            else:
                targets = [child for child in root.iterdir()]
            removed: List[str] = []
            for target in targets:
                if not target.exists():
                    continue
                freed += self._tree_usage(target)[0]
                shutil.rmtree(target, ignore_errors=True)
                removed.append(str(target))
        else:
            removed = []
        staging = self.staging_dir
        if staging.exists():
            freed += self._tree_usage(staging)[0]
            shutil.rmtree(staging, ignore_errors=True)
        self._invalidate_remote_index()
        return {'success': True, 'removed': removed, 'freed_bytes': freed}

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
                      roster_loader=self._load_roster, ready=on_ready,
                      stop_requested=self._node_stop.is_set)
            except OSError as e:
                self._last_error = self._listen_error(bind, port, e)
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
        """停止监听：请求 accept 循环退出，**并确认线程真的结束了**。

        为什么必须确认：原先是无条件 `_node_thread = None` + 返回 success —— 而
        "另一个线程 close 监听套接字"在 Windows 上并不保证解开阻塞中的 `accept()`，
        于是线程可能一直卡在那里、套接字也关不掉：界面显示"已停止"，端口却仍被占，
        再点启动就是 EADDRINUSE（实测症状）。现在按 0.5 秒轮询（见内核 serve()），
        最多等 `timeout` 秒；仍没停就**如实报失败**，并把线程引用留着 ——
        谎报成功会让用户以为端口已经腾出来了。
        """
        self._node_stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._node_thread
        if thread is not None:
            thread.join(timeout=8)
            if thread.is_alive():
                # 不置空 _node_thread：留着它，后续 start_node 的"已在运行"判定与
                # 状态显示才与事实一致。
                self._last_error = ('停止节点超时：监听线程没有在 8 秒内退出，'
                                    '端口可能仍被占用。')
                log.warning(f'[group-mesh] {self._last_error}')
                return {'success': False, 'error': self._last_error,
                        'node': self.get_node_status()}
        self._node_thread = None
        self._listener = None
        self._listening = None
        self._published_seq = None
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
        """有身份与团体且节点没在跑时自动启动（失败后冷却一小段时间再试）。

        为什么必须自动：节点不跑 → 本机不发布注册记录 → **别人永远发现不了我**；
        而"另一个成员要先手动点一次启动节点，我才能看到他"这件事没有任何提示，
        表现出来就是"明明都在同一个团体里却看不到对方"。

        为什么失败后还要再试：**服务重启瞬间端口经常还被旧进程占着**（实测：远端
        omnibox-web 重启后自动启动失败一次，之后端口空出来了，但"只试一次"的策略
        让它永远不再启动，节点就一直挂着）。因此失败只进入冷却，不永久放弃 ——
        冷却避免每次取状态都去抢一次端口和写日志。
        """
        if not (identity_exists and roster_exists):
            return
        if self._node_thread is not None and self._node_thread.is_alive():
            return
        if time.time() < self._auto_start_retry_at:
            return
        result = self.start_node()
        if result.get('success'):
            self._auto_start_attempted = True
            self._auto_start_error = None
            self._auto_start_retry_at = 0.0
            log.info('[group-mesh] 已自动启动共享节点（设计文档 §7：注册是设备自助行为）')
        else:
            self._auto_start_error = result.get('error') or '自动启动失败'
            self._auto_start_retry_at = time.time() + AUTO_START_RETRY_SECONDS
            log.warning(f'[group-mesh] 自动启动节点失败（{AUTO_START_RETRY_SECONDS}s 后重试）：'
                        f'{self._auto_start_error}')

    @staticmethod
    def _listen_error(bind: str, port: int, error: OSError) -> str:
        """把绑定失败翻译成"下一步该做什么"，并按平台给不同的排查方向。

        原先无论什么平台都提示"Windows 上入站连接默认被防火墙拦截" —— 在 Linux 上
        这条提示会把用户引到完全错误的方向（实测就发生在远端 Linux：真实原因是
        `EADDRINUSE`，端口被占，与防火墙无关）。
        """
        import errno
        import sys

        head = f'无法监听 {bind}:{port} —— {error}。'
        # 端口不可用有两种成因，提示要分开（**实测确认**）：
        #   * 真被占用：Linux 上是 `errno.EADDRINUSE`；Windows 上 Python 把
        #     `WSAEADDRINUSE`(10048) 映射到 `errno.EADDRINUSE`（该常量本身就是 10048）。
        #   * Windows 上还有 `WSAEACCES`(10013)：Python 把它的 `errno` 映射成 **13
        #     (EACCES)**、只有 `winerror` 才是 10013（实测 args=(13, …, None, 10013)）。
        #     因此**必须同时看 winerror** —— 只查 errno 会把它误判成"权限不足、
        #     请改用 1024 以上"（实测占用端口 4701 时走的就是这条错路）。
        #     10013 的成因不止一种（端口被别的进程占着、或落在 Hyper-V/WinNAT 的
        #     排除段里），所以提示同时列出两种可能，不武断。
        codes = {getattr(error, 'errno', None), getattr(error, 'winerror', None)}
        in_use_codes = {errno.EADDRINUSE, 10048, 10013}
        wsa_eacces = getattr(errno, 'WSAEACCES', None)
        if isinstance(wsa_eacces, int):
            in_use_codes.add(wsa_eacces)
        if codes & in_use_codes:
            return (head + f'该端口当前不可用：可能被别的进程占着（例如上一个节点还没退出、'
                           '同一台机器上另一个 OmniBox 实例），'
                           '也可能落在 Windows 的保留/排除端口段里（Hyper-V / WinNAT）。'
                           '在插件设置里把「监听端口」改成别的值通常即可；'
                           '想确认是谁占的：'
                           f'Linux `ss -ltnp | grep :{port}`，'
                           f'Windows `netstat -ano | findstr :{port}`（再查排除段 '
                           '`netsh int ipv4 show excludedportrange protocol=tcp`）。')
        if errno.EACCES in codes:
            return head + '权限不足：1024 以下的端口需要管理员/root 权限，请改用 1024 以上的端口。'
        if error.errno == errno.EADDRNOTAVAIL:
            return (head + f'本机没有 {bind} 这个地址：监听地址填的是本机不存在的网卡地址。'
                           '把它改成 :: （同时接受 IPv6/IPv4）或本机的实际地址。')
        if sys.platform.startswith('win'):
            return (head + 'Windows 上入站连接默认被防火墙拦截，需要为监听端口添加一次'
                           '允许规则（需管理员权限）。')
        return head + '（Linux 上请检查端口占用与监听地址是否正确。）'

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
        # 插件卸载必须留不下监听线程、上传线程与出站连接，否则重载会撞端口占用
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
