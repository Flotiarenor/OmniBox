"""共享节点（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Dict, Optional

from shell.backend.plugin_utils import load_sibling
from shell.groupmesh import registry as registry_mod
from shell.groupmesh.node import serve

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
DEFAULT_PORT = _common.DEFAULT_PORT
AUTO_START_RETRY_SECONDS = _common.AUTO_START_RETRY_SECONDS

class NodeMixin:
    """节点的启停与状态、注册记录发布 / 重发、自动启动，以及设置变更后的作废。"""

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
                      roster_loader=self._load_roster, roster_saver=self._save_roster,
                      roster_history_loader=self._load_roster_history,
                      ready=on_ready, stop_requested=self._node_stop.is_set)
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
            # 节点刚起来：唤醒后台同步，立刻做一轮发现并把本机注册记录推出去。
            self._request_sync()
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

        `_registration_lock` 串行化"读 seq → 写注册表 → 写 state.json"：后台同步、
        节点启动、共享项变更都会触发发布，不串行化会把 seq 写乱。注册表本身再加
        `_lock`，与 `_fetch_registry_from()` 的 merge 互斥。

        返回新的 `seq`；发布失败（例如注册表写不进去）不阻断节点启动 ——
        节点能收能发才是主要功能，发现能力是附加的。
        """
        identity = self._load_identity()
        if identity is None:
            return None
        self._ensure_registry()
        if self._registry is None:
            return None
        try:
            with self._registration_lock:
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
                shares = sorted(self._load_shares())
                registration = registry_mod.new_registration(
                    device_key=identity.device.public_key,
                    device_private_key=identity.device.private_key,
                    seq=seq, endpoints=endpoints, shares=shares)
                with self._lock:
                    self._registry.add(registration)
                    registry_mod.save_registry(self.identity_dir, self._registry)
                state['registration_seq'] = seq
                state['endpoints'] = [[a, p] for a, p in endpoints]
                # 记下共享清单，`_refresh_own_registration` 才能发现"共享项变了要重发"。
                state['shares'] = shares
                state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n',
                                      encoding='utf-8')
            log.info(f'[group-mesh] 已发布注册记录 seq={seq}，端点 {endpoints}，共享项 {shares}')
            # 注册记录变了：唤醒后台同步，把它 push 给对端。
            self._request_sync(registry_dirty=True)
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

        现在由后台同步每一轮调用：既比端点，也比共享清单 —— 共享项变化后
        `add_share` / `remove_share` 会立刻走一次（见 `_republish_registration`），
        这里再兜底一次，保证"改完共享项"最终一定重新发布。
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
        shares = sorted(self._load_shares())
        try:
            state = json.loads((self.identity_dir / 'state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
        if state.get('endpoints') == endpoints and state.get('shares') == shares:
            return None
        published = self._publish_registration(bind, port)
        if published is not None:
            self._published_seq = published
            log.info(f'[group-mesh] 本机端点/共享清单变化，已重新发布注册记录 '
                     f'seq={published}：{endpoints} / {shares}')
        return published

    def _republish_registration(self) -> Optional[int]:
        """共享项变化后立刻重发注册记录；节点没跑时留给后台同步兜底。"""
        if self._node_thread is None or not self._node_thread.is_alive():
            self._request_sync(registry_dirty=True)
            return None
        bind = str(self.setting('bind', '::') or '::')
        listening = self._listening
        if not listening:
            return None
        published = self._publish_registration(bind, int(listening[1]))
        if published is not None:
            self._published_seq = published
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
        if 'sync_interval_seconds' in set(changed_keys or ()):
            # 间隔变了：唤醒后台循环，让它按新间隔重新等待。
            self._sync_wake.set()
