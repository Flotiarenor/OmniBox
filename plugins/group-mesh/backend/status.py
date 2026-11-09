"""首屏状态汇总：内核版本、身份、团体、共享项、节点与同步状态。

状态自持：无。它只把各组件已经算好的结论拼成界面要的形状 —— 唯一的副作用是
"必要时自动启动节点"，那是 `Node.auto_start` 的职责，不是这里的状态。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

import shell.groupmesh as kernel
from shell.backend.ports import SettingsPort
from shell.groupmesh import __version__ as KERNEL_VERSION
from shell.groupmesh import secret_store
from shell.groupmesh.roster import staleness_report

log = logging.getLogger(__name__)


class StatusReport:
    def __init__(self, storage: Any, node: Any, discovery: Any, pulse: Any,
                 shares: Any, facts: Any, settings: SettingsPort,
                 default_port: int) -> None:
        self._storage = storage
        self._node = node
        self._discovery = discovery
        self._pulse = pulse
        self._shares = shares
        self._facts = facts
        self._settings = settings
        self._default_port = default_port

    def get_status(self) -> Dict[str, Any]:
        """界面首屏用：内核版本、身份/团体/共享项/节点的当前状态。"""
        # 先读身份与名单并（必要时）启动共享节点，**再**组装返回的 status：
        # `status['node']` 是这一刻的快照，若先组装再启动，首次调用会报告"未运行"，
        # 而节点其实已经起来了 —— 界面因此要刷新两次才显示正常。
        identity = self._storage.load_identity()
        roster = self._storage.load_roster()
        # 有身份与团体就把节点跑起来（只尝试一次）：节点不跑 → 不发布注册记录
        # → 别人发现不了本机。这里正是"用户打开插件"的时刻，而自动启动是幂等的。
        self._node.auto_start(identity is not None, roster is not None)

        settings = self._settings
        status: Dict[str, Any] = {
            'kernel': {
                'available': True,
                'version': KERNEL_VERSION,
                'package': 'shell.groupmesh',
                'path': str(Path(kernel.__file__ or '').parent),
            },
            'data_root': str(self._storage.root),
            'identity_dir': str(self._storage.identity_dir),
            # 四种位置一览：界面直接展示，用户不必猜文件放哪了。
            'locations': {
                'identity': str(self._storage.identity_dir),
                'downloads': str(self._storage.downloads_dir),
                'downloads_custom': bool(str(settings.setting('download_dir', '') or '').strip()),
                'cache': str(self._storage.cache_dir),
                'remote_cache': str(self._storage.remote_cache_dir),
            },
            'settings': {
                # 这几项都是**前端会读**的：弹窗预填团体名/主体名、端口与绑定地址展示。
                # 少给一个键的后果不是"显示为空"，而是前端读到 undefined ——
                # 曾经因为漏了 group_name，前端把它当"已有值"而走进原生 confirm
                # 分支，点击"创建团体"直接没有反应。
                'port': settings.setting('port', self._default_port),
                'bind': settings.setting('bind', '::'),
                'group_name': settings.setting('group_name', '') or '',
                'principal_name': settings.setting('principal_name', '') or '',
                'download_dir': settings.setting('download_dir', '') or '',
                'sync_interval_seconds': self._pulse.interval(),
            },
            'node': self._node.status(),
            # `sync` 由两部分组成：后台同步线程自己的状态，以及"最近一次跑得好不好"——
            # 文件同步的读取错误与最近一轮探测连不上的端点。后者是界面判断"共享根
            # 读不到/对端离线"的唯一来源，缺了它只能去看日志。
            'sync': {**self._discovery.status(),
                     'errors': self._shares.sync_errors(),
                     'unreachable': self._facts.unreachable_list()},
            'identity': None,
            'roster': None,
            'shares': [],
            'share_roots': [],
            'unsupported': [
                '内容寻址分块传输',
                'Android 轻客户端（首版不实现）',
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
                # 私钥保护级别（DPAPI / keyring / passphrase / plain）：
                # 界面据此告警；`plain` 是旧版行为，不是"已保护"。
                'secret_protection': secret_store.describe(),
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
                    # 自动分发的可见性：最近一次采纳的新名单 + 各对端版本。
                    # 没有这两个字段，用户无法判断"我是不是还停在旧名单上"。
                    'adopted_notice': self._facts.notice,
                    'peer_versions': self._facts.version_list(),
                    'members': [
                        {'name': m.name, 'principal_id': m.id,
                         'device_count': len(m.device_keys)}
                        for m in roster.members
                    ],
                }
            status['share_roots'] = self._shares.get_share_roots()
            # 兼容字段：旧的界面按 `shares` 渲染表格（含 path / acl / max_bytes），
            # 保留它可以让前端分两步迁移，而不是一次改动同时打断后端与界面。
            # 用量字段与 `share_roots` 同源（`null` 表示还没算出）。
            status['shares'] = [
                {'share_id': root['share_id'], 'path': root['path'], 'acl': root['acl'],
                 'max_bytes': root['max_bytes'], 'available': root['available'],
                 'reason': root['reason'], 'used_bytes': root['used_bytes'],
                 'entries': root['entries'], 'truncated': root['truncated'],
                 'usage_at': root['usage_at'], 'usage_stale': root['usage_stale']}
                for root in status['share_roots']
            ]
        return status
