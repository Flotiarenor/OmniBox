"""后台同步与连接（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.plugin_utils import load_sibling
from shell.groupmesh import client as mesh_client
from shell.groupmesh import registry as registry_mod
from shell.groupmesh.identity import Identity
from shell.groupmesh.records import RecordError
from shell.groupmesh.roster import Roster
from shell.groupmesh.transport import RemoteError, TransportError

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
PROBE_TIMEOUT_SECONDS = _common.PROBE_TIMEOUT_SECONDS
PROBE_OVERALL_SECONDS = _common.PROBE_OVERALL_SECONDS
SYNC_OVERALL_SECONDS = _common.SYNC_OVERALL_SECONDS

class SyncMixin:
    """注册表 / 名单的后台轮询与变更时 push、出站连接（复用池 / 专用连接）与候选端点。"""

    # ── 后台同步（定时轮询 + 变更时 push）───────────────────────────────

    def _sync_interval_seconds(self) -> int:
        """后台同步间隔；设置越界或非法时回落到默认 60 秒。"""
        try:
            value = int(self.setting('sync_interval_seconds', 60) or 60)
        except (TypeError, ValueError):
            value = 60
        return max(5, min(3600, value))

    def get_sync_status(self) -> Dict[str, Any]:
        with self._sync_state_lock:
            roster_dirty = self._roster_dirty
            registry_dirty = self._registry_dirty
        return {
            'interval': self._sync_interval_seconds(),
            'running': bool(self._sync_thread is not None and self._sync_thread.is_alive()),
            'last_sync_at': self._last_sync_at,
            'last_error': self._last_sync_error,
            'roster_dirty': roster_dirty,
            'registry_dirty': registry_dirty,
        }

    def _request_sync(self, *, roster_dirty: bool = False,
                      registry_dirty: bool = False) -> None:
        """标记"有变更待同步"并立刻唤醒后台循环（不等下一个轮询周期）。"""
        with self._sync_state_lock:
            if roster_dirty:
                self._roster_dirty = True
            if registry_dirty:
                self._registry_dirty = True
        self._sync_wake.set()

    def _clear_sync_dirty(self, *, roster: bool = False, registry: bool = False) -> None:
        with self._sync_state_lock:
            if roster:
                self._roster_dirty = False
            if registry:
                self._registry_dirty = False

    def _start_sync(self) -> None:
        if self._sync_thread is not None and self._sync_thread.is_alive():
            return
        self._sync_stop.clear()
        self._sync_thread = threading.Thread(target=self._sync_loop,
                                             name='group-mesh-sync', daemon=True)
        self._sync_thread.start()
        log.debug('[group-mesh] 后台同步线程已启动')

    def _stop_sync(self) -> None:
        self._sync_stop.set()
        self._sync_wake.set()
        thread = self._sync_thread
        if thread is not None:
            thread.join(timeout=5)
        self._sync_thread = None

    def _sync_loop(self) -> None:
        while not self._sync_stop.is_set():
            woke = self._sync_wake.wait(self._sync_interval_seconds())
            if self._sync_stop.is_set():
                break
            self._sync_wake.clear()
            if woke:
                # 把短时间内的多次变更合并成一次同步：`add_member` 之类的连续操作
                # 会多次 `_request_sync`，不该触发多次 push。
                self._sync_stop.wait(0.3)
                if self._sync_stop.is_set():
                    break
            try:
                self._sync_once()
            except Exception as e:  # 后台线程不能因为一次失败退出
                self._last_sync_error = f'{type(e).__name__}: {e}'
                log.warning('[group-mesh] 后台同步失败: %s', e)

    def _sync_once(self) -> None:
        if not self._sync_lock.acquire(blocking=False):
            return  # 已经有一轮在跑（另一轮或显式刷新），不叠加
        try:
            identity = self._load_identity()
            roster = self._load_roster()
            if identity is None or roster is None:
                return
            self._refresh_own_registration()
            self._ensure_registry()
            candidates = self._sync_candidates(identity, roster)
            # 后台不抢"第一个成功"，尽量把多个对端的注册表/名单都合并进来。
            self._fetch_registries_parallel(candidates, identity, roster,
                                            overall_timeout=SYNC_OVERALL_SECONDS,
                                            first_only=False)
            if self._sync_stop.is_set():
                return  # 卸载中：不要再发起推送
            self._push_pending_changes(identity, roster, candidates)
            self._last_sync_at = int(time.time())
            self._last_sync_error = ''
        finally:
            self._sync_lock.release()

    def _sync_candidates(self, identity: Identity,
                         roster: Roster) -> List[Tuple[Tuple[str, int], str]]:
        """后台同步与显式刷新共用的候选端点集合。"""
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
        self_device_id = identity.device.public_key.hex()
        for device_id, info in self._peer_names(roster).items():
            if device_id == self_device_id:
                continue
            for endpoint in self._peer_endpoints(info):
                add(endpoint, str(info.get('name') or device_id))
        return candidates

    def _push_pending_changes(self, identity: Identity, roster: Roster,
                              candidates: List[Tuple[Tuple[str, int], str]]) -> None:
        with self._sync_state_lock:
            roster_dirty = self._roster_dirty
            registry_dirty = self._registry_dirty
        if not (roster_dirty or registry_dirty):
            return
        # 本机自己的端点不是"对端"，不推给自己。
        self_endpoints = {endpoint for endpoint, _ in self._bootstrap_endpoints(identity)}
        targets = [(endpoint, label) for endpoint, label in candidates
                   if endpoint not in self_endpoints]
        if roster_dirty:
            self._push_roster(identity, roster, targets)
        if registry_dirty:
            self._push_registry(identity, roster, targets)

    def _push_roster(self, identity: Identity, roster: Roster,
                     targets: List[Tuple[Tuple[str, int], str]]) -> None:
        if not targets:
            return  # 还没有对端地址可推；保持 dirty，等发现到对端再试
        local_version = roster.version
        with self._lock:
            notes = dict(self._peer_roster_notes)
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
            self._clear_sync_dirty(roster=True)
            return
        pushed = 0
        history = self._load_roster_history()
        for endpoint, _label in pending:
            if self._sync_stop.is_set():
                return
            try:
                connection = self._open_dedicated(endpoint, identity, roster)
                try:
                    result = mesh_client.push_roster(connection, roster, history)
                finally:
                    connection.close()
                version = result.get('version', local_version) if isinstance(result, dict) else local_version
                with self._lock:
                    self._peer_roster_notes[endpoint] = {
                        'group': roster.group,
                        'version': int(version or local_version),
                        'hash': roster.content_hash.hex()[:16],
                    }
                pushed += 1
            except (RemoteError, TransportError, OSError) as e:
                self._unreachable[endpoint] = f'{endpoint[0]}:{endpoint[1]} 推送名单失败: {e}'
            except Exception as e:  # 兜底：单个端点失败不该中断整轮
                self._unreachable[endpoint] = f'{endpoint[0]}:{endpoint[1]} 推送名单异常: {e}'
        if pushed:
            log.info('[group-mesh] 已把名单 v%s 推给 %d 个对端', local_version, pushed)
            self._clear_sync_dirty(roster=True)

    def _push_registry(self, identity: Identity, roster: Roster,
                       targets: List[Tuple[Tuple[str, int], str]]) -> None:
        if self._published_seq is None:
            return  # 本机还没发布注册记录：等节点发布后再推
        if not targets:
            return  # 还没有对端地址可推；保持 dirty，等发现到对端再试
        self._ensure_registry()
        if self._registry is None:
            return
        # 取一份当前快照再推：后台拉取线程可能同时在 merge，直接推同一个对象会在
        # 序列化到一半时被改写。
        with self._lock:
            snapshot = registry_mod.Registry.from_snapshot_dicts(self._registry.snapshot_dicts())
        pushed = 0
        for endpoint, _label in targets:
            if self._sync_stop.is_set():
                return
            try:
                connection = self._open_dedicated(endpoint, identity, roster)
                try:
                    mesh_client.push_registry(connection, snapshot)
                finally:
                    connection.close()
                pushed += 1
            except (RemoteError, TransportError, OSError) as e:
                self._unreachable[endpoint] = f'{endpoint[0]}:{endpoint[1]} 推送注册表失败: {e}'
            except Exception as e:
                self._unreachable[endpoint] = f'{endpoint[0]}:{endpoint[1]} 推送注册表异常: {e}'
        if pushed:
            log.info('[group-mesh] 已把注册表推给 %d 个对端（本机 seq=%s）',
                     pushed, self._published_seq)
            self._clear_sync_dirty(registry=True)

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
                if first_only and any(future.result() for future in done):
                    break   # 已经拿到一份快照：够了，别让用户继续等
                if not first_only and not pending:
                    break   # 后台：等全部跑完（或到 deadline）
        finally:
            # 不 shutdown(wait=True)：未完成的探测线程不该阻塞调用方
            pool.shutdown(wait=False)

    def _fetch_registry_from(self, endpoint: Tuple[str, int], identity: Identity,
                             roster: Optional[Roster]) -> int:
        """连过去拉一次注册快照并合并，返回采纳条数。

        **顺带做一次名单分发**（§5.7）：同一条连接上再问一次 `op=roster`，
        因此"成员升级名单"不需要额外的探测预算（这条路径本来就要连对端）。
        名单验证与采纳见 `_adopt_remote_roster` —— 通道不必可信，签名才是判据。

        **用专用连接**：这一条会被后台同步线程调用，不能借 UI 的复用连接池 ——
        一条 Noise 连接上的请求是严格串行的，两个线程同时 send 会把帧拼坏
        （实现路径文档 §5.11 记的就是这个坑）。
        """
        if self._sync_stop.is_set():
            return 0
        connection = self._open_dedicated(endpoint, identity, roster)
        try:
            remote = mesh_client.fetch_registry(connection)
            self._adopt_remote_roster(connection, endpoint)
        finally:
            connection.close()
        if self._sync_stop.is_set():
            return 0  # 卸载中：不要再 merge/写盘，避免和新实例抢数据根
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

    def _adopt_remote_roster(self, connection: Any, endpoint: Tuple[str, int]) -> None:
        """从对端拉一次名单，按规则 1–6 验证后采纳（§5.7）。

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
        current = self._load_roster()
        try:
            remote = mesh_client.fetch_roster(connection, current)
        except (RemoteError, TransportError) as e:
            # 老版本对端没有 `roster` op，会回 bad_request：只记调试日志，不噪音
            log.debug(f'[group-mesh] 向 {endpoint} 拉名单失败: {e}')
            return
        if remote is None:
            return
        with self._lock:
            self._peer_roster_notes[endpoint] = {
                'group': remote.group, 'version': remote.version,
                'hash': remote.content_hash.hex()[:16],
            }
        if current is not None and remote.version <= current.version:
            return
        try:
            remote.accepts(current)
        except RecordError as e:
            log.warning(f'[group-mesh] 对端 {endpoint} 给的名单未被接受: {e}')
            return
        current_version = current.version if current is not None else None
        try:
            self._save_roster(remote)
        except OSError as e:
            log.warning(f'[group-mesh] 采纳的新名单写盘失败: {e}')
            return
        log.info(f'[group-mesh] 已从 {endpoint} 采纳新名单：'
                 f'v{current_version} -> v{remote.version}')
        self._roster_notice = {
            'from': f'{endpoint[0]}:{endpoint[1]}',
            'previous_version': current_version,
            'version': remote.version,
            'group': remote.group,
        }

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

    def _open_dedicated(self, endpoint: Tuple[str, int], identity: Identity,
                        roster: Optional[Roster]) -> Any:
        """开一条**专用**连接（用完即关），不进入 UI 的复用池。

        后台同步线程与 UI 请求会并发；复用池里的 Connection 是"一条 Noise 连接上
        请求严格串行"的，两线程同时 send 会把帧拼坏（实现路径文档 §5.11）。因此
        后台同步、以及显式探测，都走这里；UI 的浏览/取文件继续用 `_connect` 复用池。
        """
        return mesh_client.open_connection(endpoint[0], endpoint[1], identity, roster,
                                           timeout=PROBE_TIMEOUT_SECONDS)

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
