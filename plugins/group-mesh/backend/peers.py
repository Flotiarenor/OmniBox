"""对端设备：手工登记的地址、本机地址串，以及 `list_peers` 那张表。

状态自持：手工登记的对端落在 `Storage`（`.cache/peers.json`，见那里的说明），
本类不另存一份；`list_peers` 的刷新动作委托给 `Discovery.probe_round`，探测结论
读 `PeerFacts`。所以本组件自身没有可变状态 —— 它是"把对端事实组织成界面要的形状"。
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Dict, List, Tuple

log = logging.getLogger(__name__)

Endpoint = Tuple[str, int]


class Peers:
    def __init__(self, storage: Any, registry: Any, discovery: Any, node: Any,
                 facts: Any, pulse: Any, rules: Any) -> None:
        self._storage = storage
        self._registry_store = registry
        self._discovery = discovery
        self._node = node
        self._facts = facts
        self._pulse = pulse
        self._rules = rules

    # ── 手工登记 ──────────────────────────────────────────────────────────

    def action(self, opts: Any = None, action: str = 'list', endpoint: str = '',
               name: str = '', device_id: str = '') -> Dict[str, Any]:
        """手工登记 / 移除 / 更新对端端点（发现的**起点**）。

        为什么必须有这个东西：注册记录里的端点是设备自己发布的，而"第一次发现
        一台设备"没有任何自动途径 —— 注册表此刻是空的。这个缺口由"稳定地址 + 带外交换"
        来补，插件层就得提供一个填地址的地方，否则用户会卡在
        "我知道对方 IP，但界面里没有地方输入"。

        `action` 取值：
          * `list` —— 列出已登记的条目；
          * `add` —— 按地址登记（同一地址重复添加会返回 `existed`）；
          * `update` —— **按设备更新地址**（对方换了网络/端口后，把它改到新地址；
            条目按 `device_id` 认，因此不会留下一堆指向同一台设备的旧地址）；
          * `remove` —— 按地址移除。
        """
        options = self._rules.opts(opts)
        action = str(options.get('action') or action or 'list')
        endpoint = str(options.get('endpoint') or endpoint or '').strip()
        name = str(options.get('name') or name or '').strip()
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
        entries = self._storage.load_manual_peers()

        if action == 'list':
            return {'success': True, 'peers': entries}

        try:
            host, port = self.parse_endpoint(endpoint)
        except ValueError as e:
            return {'success': False, 'error': str(e)}

        if action == 'add':
            for entry in entries:
                if entry.get('host') == host and int(entry.get('port') or 0) == port:
                    return {'success': True, 'peers': entries, 'existed': True,
                            'message': f'{host}:{port} 已经在列表里'}
            entries.append({'host': host, 'port': port, 'name': name or '',
                            'device_id': device_id, 'added_at': int(time.time())})
            self._storage.save_manual_peers(entries)
            # 新登记的地址是发现的起点：立刻唤醒后台同步去探测。
            self._pulse.request()
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
            self._storage.save_manual_peers(kept)
            self._pulse.request()
            return {'success': True, 'peers': kept,
                    'replaced': len(entries) - len(kept) + 1}

        if action == 'remove':
            remaining = [e for e in entries
                         if not (e.get('host') == host and int(e.get('port') or 0) == port)]
            self._storage.save_manual_peers(remaining)
            self._pulse.request()
            return {'success': True, 'peers': remaining,
                    'removed': len(entries) - len(remaining)}

        return {'success': False, 'error': f'未知的 peers action: {action!r}'}

    @staticmethod
    def parse_endpoint(text: str) -> Endpoint:
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

    # ── 本机地址串 ────────────────────────────────────────────────────────

    def my_endpoint(self, opts: Any = None) -> Dict[str, Any]:
        """本机的可分享地址串（给对方粘贴用）。

        为什么需要"复制"这个动作：地址无法自动跨机传播 —— 对方不问我、我也不知道
        它换了地址；而带外交换（当面/聊天工具粘一次）是设计里就承认的引导手段。
        因此界面要能一键拿到这串东西，让对方粘进"更新对方地址"。
        """
        identity = self._storage.load_identity()
        roster = self._storage.load_roster()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份'}
        self._registry_store.ensure()
        record = self._registry_store.get(identity.device.public_key)
        endpoints: List[Any] = []
        node = self._node.status()
        if record is not None:
            endpoints = [list(e) for e in record.endpoints]
        listening = self._node.listening
        if listening:
            live = [listening[0], int(listening[1])]
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

    # ── 设备与共享项 ──────────────────────────────────────────────────────

    def list_peers(self, opts: Any = None, refresh: bool = True) -> Dict[str, Any]:
        """列出团体里的对端设备及其可读共享项。

        `refresh=True` 时逐台连过去拉注册快照（任一在线节点都能提供完整
        快照），因此"别人开机后我能看到它"不需要等任何人手动同步。对端不可达
        只影响它自己那一条 —— 不能因为有一台离线就让整张列表失败。
        """
        options = self._rules.opts(opts)
        if 'refresh' in options:
            refresh = options['refresh']
        identity = self._storage.load_identity()
        roster = self._storage.load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}

        self._registry_store.ensure()
        self_device_id = identity.device.public_key.hex()
        errors: List[Dict[str, str]] = []
        # 拿不到锁说明后台同步正在跑：不要在这里等它，直接读它已经同步好的状态。
        # 手动登记的端点若仍然连不上，单独回报（用户刚填的地址，失败要说清楚）。
        if refresh and self._discovery.probe_round(identity, roster):
            for entry in self._storage.load_manual_peers():
                try:
                    endpoint = (str(entry['host']), int(entry['port']))
                except (KeyError, TypeError, ValueError):
                    continue
                if endpoint in self._facts.unreachable:
                    errors.append({'device': str(entry.get('name') or endpoint[0]),
                                   'error': self._facts.unreachable[endpoint]})

        peers: List[Dict[str, Any]] = []
        names = self._registry_store.peer_names(roster)
        for device_id, info in names.items():
            if device_id == self_device_id:
                continue  # 本机不作为"对端"列出
            endpoint = self._registry_store.pick_endpoint(info.get('endpoints'))
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
                'registry_enabled': self._registry_store.enabled(),
                'registry_size': self._registry_store.size(),
                # 名单分发的可见性：各对端报出的名单版本（界面需要能做对照）
                'roster_versions': self._facts.version_list(),
                'roster_adopted': self._facts.notice}
