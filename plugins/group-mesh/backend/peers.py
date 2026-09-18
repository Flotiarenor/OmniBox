"""对端发现与浏览（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。

与本文件外的唯一差异：`_pick_endpoint` 里的类名自引用改成 `PeerMixin`（分片里没有
GroupMeshPlugin 这个名字）。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from shell.backend.plugin_utils import load_sibling
from shell.groupmesh.roster import Roster

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
_opts = _common._opts

class PeerMixin:
    """设备名与端点解析、手动登记的对端、本机地址串，以及 list_peers。"""

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
        picked = PeerMixin._peer_endpoints({'endpoints': list(endpoints or [])})
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
            # 新登记的地址是发现的起点：立刻唤醒后台同步去探测。
            self._request_sync()
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
            self._request_sync()
            return {'success': True, 'peers': kept,
                    'replaced': len(entries) - len(kept) + 1}

        if action == 'remove':
            remaining = [e for e in entries
                         if not (e.get('host') == host and int(e.get('port') or 0) == port)]
            self._save_manual_peers(remaining)
            self._request_sync()
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
        if refresh and self._sync_lock.acquire(blocking=False):
            try:
                # 先确保本机的注册记录是最新的（地址/共享变化时递增 seq 重发，§7.4）。
                # 没有这一步，"我换了网络/改了共享项"会让对端一直用旧记录。
                self._refresh_own_registration()
                # 候选端点：手动登记的（**引导**）、本机自己发布的（**自举**）、
                # 以及注册表里已经学到的。后台同步用的是同一个 `_sync_candidates`。
                candidates = self._sync_candidates(identity, roster)
                # **并行**探测。为什么必须并行：设备常同时发布 IPv6 与 IPv4 端点
                # （§4.6.1 把 v4 当可达性兜底），而 IPv6 在本机常常没有路由 —— 串行
                # 等超时的话，几个端点就能把一次刷新拖到一分钟以上（实测 80 秒）。
                # 界面路径只等第一个成功，后台路径等全部（见 `_fetch_registries_parallel`）。
                self._fetch_registries_parallel(candidates, identity, roster)
            finally:
                self._sync_lock.release()
            # 拿不到锁说明后台同步正在跑：不要在这里等它，直接读它已经同步好的状态。
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
                'registry_size': len(self._registry) if self._registry is not None else 0,
                # 名单分发的可见性：各对端报出的名单版本（§5.7 要求界面能做对照）
                'roster_versions': [
                    {'endpoint': f'{host}:{port}', **note}
                    for (host, port), note in sorted(self._peer_roster_notes.items())
                ],
                'roster_adopted': self._roster_notice}
