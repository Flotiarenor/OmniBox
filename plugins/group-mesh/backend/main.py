"""group-mesh 插件入口：装配、API 表与壳钩子。

本文件只做三件事：把组件装配起来、声明对壳暴露的 API、把壳按名调用的钩子一行委派
给组件。业务逻辑不在这里（分层门禁 R2 把入口限制在 250 行以内），常量与设置项 schema
在 `const.py`，其余按职责拆成组件。

装配顺序就是依赖顺序：脉冲 → 对端事实 → 存储 → 注册表 → 连接池 → 节点 → 发现 →
对端 / 团体 / 共享项 → 状态汇总 → 远端取回链 → 上传。反向依赖一律由构造参数注入。

接缝说明：`load_sibling` 返回 `ModuleType`，装配出来的组件属性在静态检查里是 `Any`。
这是有意的取舍 —— 需要检查的东西都在组件里；为了让接缝有类型而把插件目录塞进
`sys.path` 会换来模块名冲突，代价更大。

`get_data_root` / `get_protected_paths` 直接转发给组件，不用 `super()` 之外的东西：
即使已无 mixin，显式写 `PluginBase.x(self)` 仍是"不依赖 MRO 顺序"的保证。

数据布局（全部在 `get_data_root()` 之下，与插件设置文件分离）：

    <data_root>/group-mesh/
      identity/               主体与设备私钥（受保护路径，见 get_protected_paths）
      identity/roster.json    当前团体名单
      identity/shares.json    本机共享项声明（协议对象，可发给对端）
      identity/share_roots.json  共享项的本地位置（本机事实，从不发出）
      identity/registry.json  注册表
      downloads/              从对端取回的文件（可用 download_dir 改到别处）
      .cache/                 可重建的派生数据（目录快照、连接待用）

**尚未接通**：内容寻址分块、Android 轻客户端；涉及主体的判定仍以本机主体为准
（访问控制只到"有效令牌"这一层）。
"""

import threading
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, List

from shell.backend.freshness import engine_for
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

_const = load_sibling(__file__, 'const', 'group_mesh')
_pulse = load_sibling(__file__, 'pulse', 'group_mesh')
_peer_facts = load_sibling(__file__, 'peer_facts', 'group_mesh')
_storage = load_sibling(__file__, 'storage', 'group_mesh')
_registry = load_sibling(__file__, 'registry', 'group_mesh')
_connections = load_sibling(__file__, 'connections', 'group_mesh')
_node = load_sibling(__file__, 'node', 'group_mesh')
_discovery = load_sibling(__file__, 'discovery', 'group_mesh')
_peers = load_sibling(__file__, 'peers', 'group_mesh')
_group = load_sibling(__file__, 'group', 'group_mesh')
_shares = load_sibling(__file__, 'shares', 'group_mesh')
_share_paths = load_sibling(__file__, 'share_paths', 'group_mesh')
_status = load_sibling(__file__, 'status', 'group_mesh')
_remote = load_sibling(__file__, 'remote', 'group_mesh')
_materialize = load_sibling(__file__, 'materialize', 'group_mesh')
_mirror = load_sibling(__file__, 'mirror', 'group_mesh')
_fetch = load_sibling(__file__, 'fetch', 'group_mesh')
_uploads = load_sibling(__file__, 'uploads', 'group_mesh')
_upload = load_sibling(__file__, 'upload', 'group_mesh')


class GroupMeshPlugin(PluginBase):
    """团体组网插件。

    ## 四种"位置"

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

    #: 设置项声明是纯数据，放在 const.py（入口只做装配与委派）。
    settings_schema: ClassVar[List[Dict[str, Any]]] = _const.SETTINGS_SCHEMA

    def __init__(self, manifest: dict, config: dict) -> None:
        super().__init__(manifest, config)
        rules = _const.RULES
        # 一把共用的锁：迁移前它就同时保护连接池、注册表合并与共享项读改写，
        # 拆成多把会改变并发行为（见各组件顶部的说明）。
        shared_lock = threading.Lock()
        global_data_root = Path(self.config['directories']['data_root']).resolve()
        root = global_data_root / self.name
        self.pulse = _pulse.SyncPulse(self)
        self.facts = _peer_facts.PeerFacts(shared_lock)
        self.storage = _storage.Storage(root, self, self.pulse)
        self.registry_store = _registry.MeshRegistry(self.storage, shared_lock)
        self.connections = _connections.ConnectionPool(shared_lock)
        self.node = _node.Node(self.storage, self.registry_store, self.connections,
                               self.pulse, self, _const.DEFAULT_PORT)
        self.discovery = _discovery.Discovery(
            self.storage, self.registry_store, self.connections, self.pulse,
            self.facts, self.node)
        self.peers = _peers.Peers(self.storage, self.registry_store, self.discovery,
                                  self.node, self.facts, self.pulse, rules)
        self.identity = _group.Group(self.storage, self, rules)
        self.shares = _shares.Shares(
            self.storage, self.node, self, shared_lock, rules,
            global_data_root, lambda: engine_for(self),
            paths=_share_paths.SharePaths())
        self.report = _status.StatusReport(self.storage, self.node, self.discovery,
                                           self.pulse, self.shares, self.facts, self,
                                           _const.DEFAULT_PORT)
        self.remote = _remote.Remote(self.storage, self.registry_store, self.connections,
                                     rules)
        self.materialize = _materialize.Materialize(
            self.storage, self.registry_store, self.connections, rules)
        self.mirror = _mirror.Mirror(self.storage, self.registry_store, self.connections,
                                     self.materialize, rules, self.is_protected_path)
        self.fetch = _fetch.Fetch(self.storage, self.materialize, self.remote, self, rules)
        self.uploads = _uploads.UploadQueue(self.storage, rules)
        self.upload = _upload.Uploader(self.storage, self.registry_store, self.uploads, rules)

    def register_api(self) -> Dict[str, Callable]:
        return {
            'get_status': self.report.get_status,
            'init_identity': self.identity.init_identity,
            'get_device_keys': self.identity.get_device_keys,
            'create_group': self.identity.create_group,
            'join_group': self.identity.join_group,
            'get_invite': self.identity.get_invite,
            'add_member': self.identity.add_member,
            'add_share': self.shares.add_share,
            'remove_share': self.shares.remove_share,
            'refresh_share_roots': self.shares.refresh_share_roots,
            'peers': self.peers.action,
            'my_endpoint': self.peers.my_endpoint,
            'list_peers': self.peers.list_peers,
            'list_remote': self.remote.list_remote,
            'download_remote': self.remote.download_remote,
            'upload_remote': self.upload.upload_remote,
            'upload_status': self.uploads.status,
            'cancel_upload': self.uploads.cancel,
            'materialize_remote': self.materialize.materialize_remote,
            'mirror_share': self.mirror.mirror_share,
            'remote_cache': self.fetch.remote_cache,
            'clear_remote_cache': self.fetch.clear_remote_cache,
            'start_node': self.node.start,
            'stop_node': self.node.stop,
            'get_node_status': self.node.status,
            # 前端的「设置」按钮走壳的统一设置弹窗（`frontend/js/app.js` 的
            # openSettingsModal），它内部用 `Bridge.call('get_settings')` 取值、
            # `Bridge.call('save_settings')` 落盘，即 `POST /api/group-mesh__<方法>`；
            # 而 PluginManager 只会额外登记 `<插件>__get_settings_schema`。此前两个都没
            # 登记，于是弹窗里全是 schema 默认值、点保存必然"保存失败"（与 image-cleaner
            # 同一类缺陷，守卫见 tools/check_plugins.py 的设置 API 登记检查）。
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    # ===== 壳按名调用的钩子：名字与签名与迁移前完全一致 =====

    def get_data_root(self) -> Path:
        return self.storage.root

    def get_protected_paths(self) -> List[Path]:
        return self.storage.protected_paths(PluginBase.get_protected_paths(self))

    def get_extensions(self) -> List[dict]:
        """把自己注册成"网络位置"提供方（壳共享目录组件的「网络位置」按钮用它）。

        为什么是扩展而不是依赖：共享目录组件出现在**任意插件**的设置里
        （image-viewer 的图片文件夹、media-player 的媒体目录…），壳不可能知道
        group-mesh 这个名字，image-viewer 也不该为"可能有个远端图库"去声明依赖。
        扩展机制里提供方与宿主互不认识，只认这一条 placement 约定。

        刻意**不写 `host`**：组件会对所有宿主展示，提供方应当对所有宿主可用。

        约定（见 `shell/frontend/public/shell/folder-picker.js`）：
        * 组件用 `system_get_plugin_extensions(null, 'network-location')` 发现提供方；
        * 提供方页面渲染在弹窗 iframe 里，选中后 `postMessage` 回填一个**本地目录**
          （`{type:'cubby:network-location', action:'picked', path, label}`）——
          路径是本地的是关键：所有消费方的后端都只认本地路径。
        """
        return [{
            'placement': 'network-location',
            'id': 'group-mesh',
            'label': '团体组网',
            'icon': 'icon:network',
            'description': '把某台成员设备上的共享项取到本地一个目录，再加进列表',
            'embedUrl': '/plugins/group-mesh/frontend/network-location.html',
        }]

    def is_content_placeholder(self, path: Any) -> bool:
        return self.fetch.is_content_placeholder(path)

    def ensure_file(self, path: Any) -> None:
        return self.fetch.ensure_file(path)

    def freshness_spec(self) -> Dict[str, Any]:
        return self.shares.spec()

    def get_node_status(self) -> Dict[str, Any]:
        return self.node.status()

    def get_sync_status(self) -> Dict[str, Any]:
        return self.discovery.status()

    def get_share_roots(self) -> List[Dict[str, Any]]:
        return self.shares.get_share_roots()

    @property
    def cache_dir(self) -> Path:
        """可重建派生数据的目录（实例上的路径访问器）。"""
        return self.storage.cache_dir

    @property
    def downloads_dir(self) -> Path:
        """取回文件的落点（实例上的路径访问器）。"""
        return self.storage.downloads_dir

    def on_settings_changed(self, changed_keys) -> None:
        self.node.on_settings_changed(changed_keys)

    def on_load(self) -> None:
        self.storage.root.mkdir(parents=True, exist_ok=True)
        self.uploads.begin_load()
        self.uploads.restore()
        # 后台同步随插件加载启动；`on_unload` 必须把它停掉，否则重载会留下线程。
        self.discovery.start()

    def on_unload(self) -> None:
        # 插件卸载必须留不下监听线程、上传线程与出站连接，否则重载会撞端口占用。
        # 先立起卸载标志：取消是"到分块边界才生效"，而新实例可能已经开始读同一个
        # 数据根了。
        self.uploads.begin_unload()
        self.discovery.stop()
        self.uploads.cancel_all()
        self.connections.close_all()
        self.node.stop()
