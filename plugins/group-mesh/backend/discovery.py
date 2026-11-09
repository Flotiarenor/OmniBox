"""后台同步：定时轮询、变更时 push、候选端点与注册快照探测。

状态自持：同步线程、以及"同一时刻只允许一轮在跑"的那把 `sync_lock`。脏标志、唤醒
事件与上次同步结果在 `SyncPulse`；对端事实在 `PeerFacts`；连接在 `ConnectionPool`；
注册表在 `MeshRegistry` —— 本组件只负责"什么时候、按什么顺序去问谁"。

没有环：节点（`Node`）需要"唤醒同步"时只往脉冲里打一针，不认识本组件；本组件反过来
要"按当前端点重发本机注册记录"，因此依赖 `Node`。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from shell.groupmesh import client as mesh_client
from shell.groupmesh.identity import Identity
from shell.groupmesh.records import RecordError
from shell.groupmesh.roster import Roster
from shell.groupmesh.transport import RemoteError, TransportError

log = logging.getLogger(__name__)

#: 一次刷新的候选探测**总**等待上限（秒）。到点就带着已有结果返回，不再等剩下的
#: 端点 —— 界面上这是一次点击，不能因为几条死地址把用户按在那里。
PROBE_OVERALL_SECONDS = 4.0

#: 后台同步一轮的总等待上限（秒）。后台不面向"点一下要立刻看到"，因此等所有候选
#: 都试一遍，尽量从多个对端合并到最新注册表/名单；但也不能让一轮无限拖住下一轮。
SYNC_OVERALL_SECONDS = 10.0

Endpoint = Tuple[str, int]
Candidate = Tuple[Endpoint, str]


class Discovery:
    def __init__(self, storage: Any, registry: Any, connections: Any, pulse: Any,
                 facts: Any, node: Any) -> None:
        self._storage = storage
        self._registry_store = registry
        self._connections = connections
        self._pulse = pulse
        self._facts = facts
        self._node = node
        #: 保证"后台同步"与显式 `list_peers(refresh=True)` 不会同时探测/合并。
        self._sync_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # ── 状态与生命周期 ────────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        roster_dirty, registry_dirty = self._pulse.dirty()
        thread = self._thread
        return {
            'interval': self._pulse.interval(),
            'running': bool(thread is not None and thread.is_alive()),
            'last_sync_at': self._pulse.last_sync_at,
            'last_error': self._pulse.last_error,
            'roster_dirty': roster_dirty,
            'registry_dirty': registry_dirty,
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._pulse.stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name='group-mesh-sync', daemon=True)
        self._thread.start()
        log.debug('[group-mesh] 后台同步线程已启动')

    def stop(self) -> None:
        self._pulse.stop_event.set()
        self._pulse.wake_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        while not self._pulse.stop_event.is_set():
            woke = self._pulse.wake_event.wait(self._pulse.interval())
            if self._pulse.stop_event.is_set():
                break
            self._pulse.wake_event.clear()
            if woke:
                # 把短时间内的多次变更合并成一次同步：`add_member` 之类的连续操作
                # 会多次 `request()`，不该触发多次 push。
                self._pulse.stop_event.wait(0.3)
                if self._pulse.stop_event.is_set():
                    break
            try:
                self.run_once()
            except Exception as e:  # 后台线程不能因为一次失败退出
                self._pulse.last_error = f'{type(e).__name__}: {e}'
                log.warning('[group-mesh] 后台同步失败: %s', e)

    def run_once(self) -> None:
        if not self._sync_lock.acquire(blocking=False):
            return  # 已经有一轮在跑（另一轮或显式刷新），不叠加
        try:
            identity = self._storage.load_identity()
            roster = self._storage.load_roster()
            if identity is None or roster is None:
                return
            self._node.refresh_own()
            self._registry_store.ensure()
            candidates = self.sync_candidates(identity, roster)
            # 后台不抢"第一个成功"，尽量把多个对端的注册表/名单都合并进来。
            self.fetch_registries_parallel(candidates, identity, roster,
                                           overall_timeout=SYNC_OVERALL_SECONDS,
                                           first_only=False)
            if self._pulse.stop_event.is_set():
                return  # 卸载中：不要再发起推送
            self.push_pending_changes(identity, roster, candidates)
            self._pulse.last_sync_at = int(time.time())
            self._pulse.last_error = ''
        finally:
            self._sync_lock.release()

    def probe_round(self, identity: Identity, roster: Roster) -> bool:
        """界面侧"刷新一次"：拿不到锁就立刻返回 False（后台正在跑同一件事）。

        返回 True 表示本组件真的探测了一轮 —— 调用方据此决定要不要回报连不上的
        手动地址：拿不到锁时那些结论可能还在更新中。
        """
        if not self._sync_lock.acquire(blocking=False):
            return False
        try:
            # 先确保本机的注册记录是最新的（地址/共享变化时递增 seq 重发）。
            # 没有这一步，"我换了网络/改了共享项"会让对端一直用旧记录。
            self._node.refresh_own()
            # 候选端点：手动登记的（**引导**）、本机自己发布的（**自举**）、
            # 以及注册表里已经学到的。后台同步用的是同一个 `sync_candidates`。
            candidates = self.sync_candidates(identity, roster)
            # **并行**探测。为什么必须并行：设备常同时发布 IPv6 与 IPv4 端点
            # （v4 是可达性兜底），而 IPv6 在本机常常没有路由 —— 串行
            # 等超时的话，几个端点就能把一次刷新拖到一分钟以上（实测 80 秒）。
            # 界面路径只等第一个成功，后台路径等全部。
            self.fetch_registries_parallel(candidates, identity, roster,
                                           overall_timeout=PROBE_OVERALL_SECONDS,
                                           first_only=True)
        finally:
            self._sync_lock.release()
        return True

    # ── 候选端点 ──────────────────────────────────────────────────────────

    def sync_candidates(self, identity: Identity, roster: Roster) -> List[Candidate]:
        """后台同步与显式刷新共用的候选端点集合。"""
        candidates: List[Candidate] = []
        seen: set = set()

        def add(endpoint: Endpoint, label: str) -> None:
            if endpoint and endpoint not in seen:
                seen.add(endpoint)
                candidates.append((endpoint, label))

        for entry in self._storage.load_manual_peers():
            try:
                add((str(entry['host']), int(entry['port'])),
                    str(entry.get('name') or entry.get('host') or ''))
            except (KeyError, TypeError, ValueError):
                continue
        for endpoint, label in self._registry_store.bootstrap_endpoints(identity):
            add(endpoint, label)
        self_device_id = identity.device.public_key.hex()
        for device_id, info in self._registry_store.peer_names(roster).items():
            if device_id == self_device_id:
                continue
            for endpoint in self._registry_store.peer_endpoints(info):
                add(endpoint, str(info.get('name') or device_id))
        return candidates

    # ── 推送 ──────────────────────────────────────────────────────────────

    def push_pending_changes(self, identity: Identity, roster: Roster,
                             candidates: List[Candidate]) -> None:
        roster_dirty, registry_dirty = self._pulse.dirty()
        if not (roster_dirty or registry_dirty):
            return
        # 本机自己的端点不是"对端"，不推给自己。
        self_endpoints = {endpoint
                          for endpoint, _ in self._registry_store.bootstrap_endpoints(identity)}
        targets = [(endpoint, label) for endpoint, label in candidates
                   if endpoint not in self_endpoints]
        if roster_dirty:
            self._push_roster(identity, roster, targets)
        if registry_dirty:
            self._push_registry(identity, roster, targets)

    def _push_roster(self, identity: Identity, roster: Roster,
                     targets: List[Candidate]) -> None:
        if not targets:
            return  # 还没有对端地址可推；保持 dirty，等发现到对端再试
        local_version = roster.version
        notes = self._facts.notes_snapshot()
        pending = []
        for endpoint, label in targets:
            note = notes.get(endpoint) or {}
            try:
                known = int(note.get('version') or 0)
            except (TypeError, ValueError):
                known = 0
            if known < local_version:
                pending.append((endpoint, label))
        if not pending:
            self._pulse.clear(roster=True)
            return
        pushed = 0
        history = self._storage.load_roster_history()
        for endpoint, _label in pending:
            if self._pulse.stop_event.is_set():
                return
            try:
                connection = self._connections.open_dedicated(endpoint, identity, roster)
                try:
                    result = mesh_client.push_roster(connection, roster, history)
                finally:
                    connection.close()
                version = result.get('version', local_version) if isinstance(result, dict) else local_version
                self._facts.note_roster(endpoint, {
                    'group': roster.group,
                    'version': int(version or local_version),
                    'hash': roster.content_hash.hex()[:16],
                })
                pushed += 1
            except (RemoteError, TransportError, OSError) as e:
                self._facts.mark_unreachable(
                    endpoint, f'{endpoint[0]}:{endpoint[1]} 推送名单失败: {e}')
            except Exception as e:  # 兜底：单个端点失败不该中断整轮
                self._facts.mark_unreachable(
                    endpoint, f'{endpoint[0]}:{endpoint[1]} 推送名单异常: {e}')
        if pushed:
            log.info('[group-mesh] 已把名单 v%s 推给 %d 个对端', local_version, pushed)
            self._pulse.clear(roster=True)

    def _push_registry(self, identity: Identity, roster: Roster,
                       targets: List[Candidate]) -> None:
        if self._registry_store.published_seq is None:
            return  # 本机还没发布注册记录：等节点发布后再推
        if not targets:
            return  # 还没有对端地址可推；保持 dirty，等发现到对端再试
        self._registry_store.ensure()
        if self._registry_store.registry is None:
            return
        # 取一份当前快照再推：后台拉取线程可能同时在 merge，直接推同一个对象会在
        # 序列化到一半时被改写。
        snapshot = self._registry_store.snapshot_copy()
        pushed = 0
        for endpoint, _label in targets:
            if self._pulse.stop_event.is_set():
                return
            try:
                connection = self._connections.open_dedicated(endpoint, identity, roster)
                try:
                    mesh_client.push_registry(connection, snapshot)
                finally:
                    connection.close()
                pushed += 1
            except (RemoteError, TransportError, OSError) as e:
                self._facts.mark_unreachable(
                    endpoint, f'{endpoint[0]}:{endpoint[1]} 推送注册表失败: {e}')
            except Exception as e:
                self._facts.mark_unreachable(
                    endpoint, f'{endpoint[0]}:{endpoint[1]} 推送注册表异常: {e}')
        if pushed:
            log.info('[group-mesh] 已把注册表推给 %d 个对端（本机 seq=%s）',
                     pushed, self._registry_store.published_seq)
            self._pulse.clear(registry=True)

    # ── 拉取 ──────────────────────────────────────────────────────────────

    def fetch_registries_parallel(self, candidates: List[Candidate], identity: Identity,
                                  roster: Optional[Roster],
                                  overall_timeout: float = PROBE_OVERALL_SECONDS,
                                  first_only: bool = True) -> None:
        """并行向多个候选端点拉注册快照。

        `first_only=True`（界面点击用）：**只等第一个成功的**。为什么不是"全部跑完再
        返回"：界面上这是一次点击，用户要的是尽快看到设备列表。实测（4 条一定连不上
        的端点）：串行 21.1s → 并行等全部 6.0s → 只等第一个成功 0.0s（有一条可达时）。

        `first_only=False`（后台同步用）：等所有候选（最多 `overall_timeout`），
        以便从多个对端合并到最新注册表/名单。
        """
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        self._facts.reset_unreachable()
        if not candidates:
            return

        def probe(item: Candidate) -> bool:
            endpoint, _label = item
            try:
                self.fetch_registry_from(endpoint, identity, roster)
                return True
            except (RemoteError, TransportError, OSError) as e:
                self._facts.mark_unreachable(endpoint, f'{endpoint[0]}:{endpoint[1]} 连不上: {e}')
            except Exception as e:   # 兜底：一个端点出意外不该拖垮整次刷新
                self._facts.mark_unreachable(endpoint, f'{endpoint[0]}:{endpoint[1]} 失败: {e}')
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
                if first_only and any(future.result() for future in done):
                    break   # 已经拿到一份快照：够了，别让用户继续等
                if not first_only and not pending:
                    break   # 后台：等全部跑完（或到 deadline）
        finally:
            # 不 shutdown(wait=True)：未完成的探测线程不该阻塞调用方
            pool.shutdown(wait=False)

    def fetch_registry_from(self, endpoint: Endpoint, identity: Identity,
                            roster: Optional[Roster]) -> int:
        """连过去拉一次注册快照并合并，返回采纳条数。

        **顺带做一次名单分发**：同一条连接上再问一次 `op=roster`，
        因此"成员升级名单"不需要额外的探测预算（这条路径本来就要连对端）。
        名单验证与采纳见 `adopt_remote_roster` —— 通道不必可信，签名才是判据。

        **用专用连接**：这一条会被后台同步线程调用，不能借界面侧的复用连接池 ——
        一条 Noise 连接上的请求是严格串行的，两个线程同时 send 会把帧拼坏。
        """
        if self._pulse.stop_event.is_set():
            return 0
        connection = self._connections.open_dedicated(endpoint, identity, roster)
        try:
            remote = mesh_client.fetch_registry(connection)
            self.adopt_remote_roster(connection, endpoint)
        finally:
            connection.close()
        if self._pulse.stop_event.is_set():
            return 0  # 卸载中：不要再 merge/写盘，避免和新实例抢数据根
        return self._registry_store.merge_and_save(remote)

    def adopt_remote_roster(self, connection: Any, endpoint: Endpoint) -> None:
        """从对端拉一次名单，按规则 1–6 验证后采纳。

        三层判断缺一不可：

        1. 对端报的版本号**不大于**本机时直接跳过 —— 省一次写盘，也避免把
           "并发签发里被丢弃的那一份"当更新（`accepts` 会按规则 1/2 再判一次）；
        2. `Roster.accepts(current)` 校验签名者资格与版本链（规则 1–6）；
        3. 采纳后写盘，并记下这个对端的名单版本供界面做对照。

        任何失败都只记日志：名单没同步上不该让"刷新设备"整体失败 —— 用户至少还能
        看到设备列表，并能在界面上看到版本不一致。

        注意这里**不** `push`：本机的新名单会不会被对端采纳由对端决定，
        而主动 push 需要一个明确的时机（当前是"群主加人后另一个成员上线时会被拉走"）。
        """
        current = self._storage.load_roster()
        try:
            remote = mesh_client.fetch_roster(connection, current)
        except (RemoteError, TransportError) as e:
            # 老版本对端没有 `roster` op，会回 bad_request：只记调试日志，不噪音
            log.debug(f'[group-mesh] 向 {endpoint} 拉名单失败: {e}')
            return
        if remote is None:
            return
        self._facts.note_roster(endpoint, {
            'group': remote.group, 'version': remote.version,
            'hash': remote.content_hash.hex()[:16],
        })
        if current is not None and remote.version <= current.version:
            return
        try:
            remote.accepts(current)
        except RecordError as e:
            log.warning(f'[group-mesh] 对端 {endpoint} 给的名单未被接受: {e}')
            return
        current_version = current.version if current is not None else None
        try:
            self._storage.save_roster(remote)
        except OSError as e:
            log.warning(f'[group-mesh] 采纳的新名单写盘失败: {e}')
            return
        log.info(f'[group-mesh] 已从 {endpoint} 采纳新名单：'
                 f'v{current_version} -> v{remote.version}')
        self._facts.set_notice({
            'from': f'{endpoint[0]}:{endpoint[1]}',
            'previous_version': current_version,
            'version': remote.version,
            'group': remote.group,
        })
