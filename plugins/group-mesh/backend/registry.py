"""注册表对象与"设备 -> 端点/共享清单"的派生视图。

状态自持：注册表对象、发布序号、以及所有"从注册表算出来的"东西（每台设备的名称、
端点、候选地址）。写入方是 Node（发布本机记录）与 Discovery（合并对端快照），
读取方是 Peers / Discovery / StatusReport —— 全部经由本类的方法。

## 为什么这几件事在同一个组件里

`peers` / `discovery` 都要回答"这台设备有哪些端点""它自称什么名字"，而两者又互相
调用（发现要列对端，列对端要先探测）。把这些只读派生视图放在注册表旁边，两个上层
组件就都只依赖本类，环因此不存在。

合并与发布沿用装配期注入的那把 `lock`：迁移前 `Registry.merge` 的 seq 单调性判定、
共享项声明的读改写、连接池共锁是同一把锁，拆开会改变并发行为。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

from shell.groupmesh import registry as registry_mod
from shell.groupmesh.roster import Roster

log = logging.getLogger(__name__)

Endpoint = Tuple[str, int]


class MeshRegistry:
    def __init__(self, storage: Any, lock: threading.Lock) -> None:
        self._storage = storage
        self._lock = lock
        # None = 本节点未启用注册表。类型是 Any：注册表在运行期才建起来，
        # 而所有调用点都先 `ensure()` 或判空（与迁移前一致）。
        self.registry: Any = None
        # 本次运行发布的注册记录序号（None = 还没发布成功）。界面据此显示
        # "本机是否已对其他设备可见" —— 发布失败时节点照常能收发，但别人发现不了。
        self.published_seq: Optional[int] = None

    # ── 对象生命周期 ──────────────────────────────────────────────────────

    def ensure(self) -> None:
        """确保手上有注册表对象（节点没启动时也能读磁盘上的那份）。"""
        if self.registry is not None:
            return
        try:
            self.registry = registry_mod.load_registry(self._storage.identity_dir)
        except Exception as e:
            self.registry = registry_mod.Registry()
            log.warning(f'[group-mesh] 注册表载入失败，按空表处理: {e}')

    def reload(self) -> None:
        """重新读盘（节点启动时无条件重读一次，不沿用内存里的旧对象）。

        载入失败不能让节点起不来 —— 注册表是可重建的辅助数据，缺了只是发现能力弱。
        """
        try:
            self.registry = registry_mod.load_registry(self._storage.identity_dir)
        except Exception as e:
            self.registry = registry_mod.Registry()
            log.warning(f'[group-mesh] 注册表载入失败，按空表处理: {e}')

    def enabled(self) -> bool:
        return self.registry is not None

    def size(self) -> int:
        return len(self.registry) if self.registry is not None else 0

    def get(self, device_key: bytes) -> Any:
        return self.registry.get(device_key) if self.registry is not None else None

    # ── 写入 ──────────────────────────────────────────────────────────────

    def add_and_save(self, registration: Any) -> None:
        """加入一条本机发布的记录并落盘（调用方持外层 `_registration_lock`）。"""
        with self._lock:
            self.registry.add(registration)
            registry_mod.save_registry(self._storage.identity_dir, self.registry)

    def merge_and_save(self, remote: Any) -> int:
        """合并一份对端快照并落盘，返回采纳条数。

        并行探测时多个线程会同时合并：`Registry.merge` 的 seq 单调性判定必须
        串行化，否则同一设备的两条记录可能互相覆盖。
        """
        with self._lock:
            accepted = self.registry.merge(remote)
            if accepted:
                try:
                    registry_mod.save_registry(self._storage.identity_dir, self.registry)
                except OSError as e:
                    log.warning(f'[group-mesh] 注册表写盘失败: {e}')
        return accepted

    def snapshot_copy(self) -> Any:
        """取一份当前快照的独立副本。

        后台拉取线程可能同时在 merge，直接推同一个对象会在序列化到一半时被改写。
        """
        with self._lock:
            return registry_mod.Registry.from_snapshot_dicts(self.registry.snapshot_dicts())

    # ── 派生视图 ──────────────────────────────────────────────────────────

    @staticmethod
    def device_names(roster: Optional[Roster]) -> Dict[bytes, str]:
        """设备公钥 -> 显示名（取它所属主体的名字）。"""
        names: Dict[bytes, str] = {}
        for member in (roster.members if roster else []):
            for device in member.device_keys:
                names[device] = member.name
        return names

    @staticmethod
    def peer_endpoints(info: Dict[str, Any]) -> List[Endpoint]:
        """一台设备发布的**全部**可用端点（IPv6 在前，然后 IPv4）。

        为什么不能只返回一个：IPv4 是可达性兜底，设备因此常常同时
        发布两种端点。只挑一个的话，首选那个不通（IPv6 没路由、对端只在 v4 上可达）
        就会把整台设备判成离线 —— 而它其实就在那儿。
        """
        valid: List[Endpoint] = []
        for item in info.get('endpoints') or []:
            try:
                address, port = str(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if address and 1 <= port <= 65535 and (address, port) not in valid:
                valid.append((address, port))
        valid.sort(key=lambda pair: 0 if ':' in pair[0] else 1)
        return valid

    @classmethod
    def pick_endpoint(cls, endpoints: Optional[Iterable[Any]]) -> Optional[Endpoint]:
        """从端点列表里挑一个**首选**（用于界面展示）。优先 IPv6 —— 那是主路径。"""
        picked = cls.peer_endpoints({'endpoints': list(endpoints or [])})
        return picked[0] if picked else None

    def peer_names(self, roster: Optional[Roster]) -> Dict[str, Dict[str, Any]]:
        """注册表里的每台设备 -> 它自称的名字与共享清单。"""
        result: Dict[str, Dict[str, Any]] = {}
        if self.registry is None:
            return result
        names = self.device_names(roster)
        for record in self.registry.snapshot():
            device_id = record.device_key.hex()
            result[device_id] = {'name': names.get(record.device_key) or f'设备 {record.device_id}',
                                 'shares': list(record.shares),
                                 'endpoints': [list(e) for e in record.endpoints],
                                 'seq': record.seq,
                                 'ts': record.ts}
        return result

    def bootstrap_endpoints(self, identity: Any) -> List[Tuple[Endpoint, str]]:
        """自举候选：**用本机自己发布的端点去问一次注册快照**。

        为什么这一招成立：注册数据是"任一在线共享节点都能提供完整
        快照"。而两台机器可能都还没跟对方说过话 —— 此时谁也不知道对方地址，但**本机
        注册记录里写着本机自己的地址**，拿它去问"任何在线节点"（包括自己），就能把
        对方的记录（含对方的端点）一起拿回来。

        这正是"同一局域网里两台设备互相看不见"的成因：双方都只认识自己。实测
        复现过 —— 本机注册表里只有本机那一条（seq=43），名单里有对方但没有地址，
        于是界面只能如实显示"没有可用端点"。

        只在本机端点未知时才有用；已经学到对方端点之后这条自然命中不了新东西。
        安全性上没有新增暴露面：这些地址本来就是本机自己发布出去、供团体成员连的。
        """
        self.ensure()
        candidates: List[Tuple[Endpoint, str]] = []
        if self.registry is None:
            return candidates
        record = self.registry.get(identity.device.public_key)
        if record is None:
            return candidates
        for endpoint in self.peer_endpoints({'endpoints': record.endpoints}):
            candidates.append((endpoint, '本机'))
        return candidates

    def candidates_for(self, device_id: str, roster: Roster) -> List[Tuple[Endpoint, str]]:
        """某台设备的候选端点（设备**发布过的全部端点** + 手动登记的那些）。

        返回多个候选有两个来源，都需要：同一台设备可能同时发布 IPv6 与 IPv4
        （v4 是可达性兜底），以及用户在引导期手工填过一个地址。
        第一个连不上时再试下一个；**只对连接失败重试** —— 权限或路径错误是确定性
        结论，换一台设备重试只会把真正的错误盖掉。
        """
        candidates: List[Tuple[Endpoint, str]] = []
        names = self.peer_names(roster) if self.registry is not None else {}
        info = names.get(device_id) if device_id else None
        if info is not None:
            label = str(info.get('name') or device_id)
            for endpoint in self.peer_endpoints(info):
                candidates.append((endpoint, label))
        for entry in self._storage.load_manual_peers():
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
