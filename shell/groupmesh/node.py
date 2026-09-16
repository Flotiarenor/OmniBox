"""节点服务端：处理一条连接上的请求。

## 安全边界（设计文档 §6.5）

"可写共享是数据投放面"：被授予写权限的主体可以在属主目录内创建任意文件。
因此本模块把下列约束做成**硬约束**，不依赖调用方自觉：

* 路径校验：相对路径解析后必须仍在该共享项根目录内（拒绝 `..` 与符号链接逃逸）；
* 共享目录与程序目录、配置目录分离：共享项路径由属主显式指定，不由插件推断；
* 不提供远程重命名与移动；
* 目录枚举数量有上限，避免一次请求把整个目录树返回。

## 授权判定的位置（§6.3）

判定在这里、也就是**属主设备**上执行，判定依据是 `connection.peer.principal_key`
—— 该值来自 Noise 握手验证过的静态公钥，再经团体名单解析而来，
**不接受请求参数里的自称身份**。
"""

from __future__ import annotations

import base64
import os
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__ as KERNEL_VERSION
from .identity import Identity
from .registry import Registration, Registry, new_registration
from .roster import Roster
from .shares import AclDenied, Authorizer, LocalShare, Permission
from .transport import Connection, PeerIdentity, TransportError, exchange_hello, run_handshake

# 目录枚举与文件大小的硬上限
MAX_LIST_ENTRIES = 2000
MAX_WRITE_BYTES = 8 * 1024 * 1024


def resolve_in_share(root: str, relative: str) -> str:
    """把共享项内的相对路径解析成绝对路径，并确保不越界。

    三道检查缺一不可：

    1. `..` 段直接拒绝（比"解析后比较"更早，错误信息也更明确）；
    2. 绝对路径（含 Windows 盘符与 UNC）拒绝；
    3. 解析符号链接后再确认最终路径仍在根内 —— 只做字符串前缀比较挡不住
       "根目录下有个指向 /etc 的符号链接"这条路。
    """
    if not relative:
        relative = '.'
    normalized = relative.replace('\\', '/')
    parts = [p for p in normalized.split('/') if p not in ('', '.')]
    if any(p == '..' for p in parts):
        raise PathRejected(f'路径含 .. 段，拒绝: {relative!r}')
    if os.path.isabs(normalized) or (len(normalized) > 1 and normalized[1] == ':'):
        raise PathRejected(f'拒绝绝对路径: {relative!r}')

    root_abs = os.path.realpath(os.path.abspath(root))
    candidate = os.path.realpath(os.path.join(root_abs, *parts)) if parts else root_abs
    try:
        inside = os.path.commonpath([root_abs, candidate]) == root_abs
    except ValueError as e:
        # 不同盘符 / 不同挂载点，commonpath 直接抛错 —— 一定不在根内
        raise PathRejected(f'路径不在共享根内: {relative!r}') from e
    if not inside:
        raise PathRejected(f'路径逃逸出共享根: {relative!r} -> {candidate}')
    return candidate


# 单条已接受连接的读写超时（秒）。握手、请求应答都受它约束 —— 没有它，一个
# "连上就不说话"的对端能把处理线程永远挂住（处理线程已独立于 accept 循环，
# 但挂着的线程仍占着一条连接与一个线程名额）。
CONNECTION_TIMEOUT_SECONDS = 30.0


class PathRejected(Exception):
    """路径校验失败。协议层转成 403 语义的应答（越界与无权限同一语义）。"""


@dataclass
class Node:
    """一个共享节点的服务端实现。"""

    identity: Identity
    roster: Optional[Roster]
    shares: Dict[str, LocalShare] = field(default_factory=dict)
    registry: Optional[Registry] = None
    # 记录审计：谁在何时写入/删除了什么（§6.4）。MVP 先落内存，见实现路径文档 P2。
    audit: List[Dict[str, Any]] = field(default_factory=list)
    # 每次建连时重新读取名单的回调（见 current_roster）。
    roster_loader: Optional[Callable[[], Optional[Roster]]] = None
    # 外部请求停止：accept 循环每轮检查一次（见 serve() 里为什么用轮询而不是
    # "另一个线程 close 套接字"）。None 表示不检查，一直服务到套接字被关闭。
    stop_requested: Optional[Callable[[], bool]] = None

    @property
    def device_key(self) -> bytes:
        return self.identity.device.public_key

    @property
    def owner_key(self) -> bytes:
        return self.identity.principal.public_key

    def current_roster(self) -> Optional[Roster]:
        """取当前名单：有 loader 就重读，否则用构造时传入的那份。

        **必须每次建连都重读**：团体名单是会变的（群主加人、改角色），而节点是
        长驻进程。若只在启动时载入一次，那么在节点运行期间新加入的成员会被判为
        "不在名单里"而拒绝 —— 实测踩到过：先起节点、再在另一处 `roster add`，
        新成员连接直接被拒。**重启节点才能生效**对使用者是不可接受的。
        """
        if self.roster_loader is not None:
            try:
                loaded = self.roster_loader()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f'重新读取名单失败，沿用内存副本: {e}')
            else:
                if loaded is not None:
                    self.roster = loaded
        return self.roster

    def add_share(self, share: LocalShare) -> None:
        if share.declaration.owner_key != self.owner_key:
            raise AclDenied('共享项的属主不是本机主体，拒绝挂载')
        if share.declaration.node_key != self.device_key:
            raise AclDenied('共享项的 node 不是本机设备，拒绝挂载')
        self.shares[share.share_id] = share

    # ── 授权 ──────────────────────────────────────────────────────────────

    def _authorizer(self, peer: PeerIdentity) -> Authorizer:
        roster = self.current_roster()
        group_member = bool(roster and roster.contains_principal(peer.principal_key))
        return Authorizer(requester_key=peer.principal_key, group_member=group_member,
                          requester_device_key=peer.device_key)

    def _share_or_fail(self, share_id: Any) -> LocalShare:
        if not isinstance(share_id, str) or share_id not in self.shares:
            raise RemoteFailure('not_found', f'本机没有共享项 {share_id!r}')
        return self.shares[share_id]

    # ── 请求分派 ──────────────────────────────────────────────────────────

    def handle(self, request: Dict[str, Any], peer: PeerIdentity) -> Dict[str, Any]:
        """处理一条请求，返回应答。任何异常都转成 error 应答，不断开连接。"""
        op = request.get('op')
        if not isinstance(op, str):
            return {'status': 'error', 'code': 'bad_request', 'error': '缺少 op 字段'}
        handler = {
            'hello': self._op_hello,
            'list': self._op_list,
            'stat': self._op_stat,
            'read': self._op_read,
            'write': self._op_write,
            'delete': self._op_delete,
            'registry': self._op_registry,
        }.get(op)
        if handler is None:
            return {'status': 'error', 'code': 'bad_request', 'error': f'未知操作 {op!r}'}
        try:
            return handler(request, peer)
        except AclDenied as e:
            # 403 语义：越界与无权限同一语义（§6.5 / plugin-guide 的既有约定）
            return {'status': 'error', 'code': 'forbidden', 'error': str(e)}
        except PathRejected as e:
            return {'status': 'error', 'code': 'forbidden', 'error': str(e)}
        except RemoteFailure as e:
            return {'status': 'error', 'code': e.code, 'error': e.message}
        except Exception as e:
            return {'status': 'error', 'code': 'internal', 'error': f'{type(e).__name__}: {e}'}

    def _op_hello(self, request: Dict[str, Any], peer: PeerIdentity) -> Dict[str, Any]:
        return {'status': 'ok', 'kernel': KERNEL_VERSION, 'device': self.device_key.hex(),
                'principal': self.owner_key.hex(), 'name': self.identity.device.name,
                'shares': sorted(self.shares), 'features': list(request.get('features') or [])}

    def _op_list(self, request: Dict[str, Any], peer: PeerIdentity) -> Dict[str, Any]:
        """列出对端**有权读取**的共享项（无权限的共享项不出现在结果里）。"""
        authorizer = self._authorizer(peer)
        visible = [s.declaration.to_dict() for s in self.shares.values()
                   if authorizer.allows(s.declaration, Permission.READ)]
        return {'status': 'ok', 'shares': visible, 'count': len(visible)}

    def _op_stat(self, request: Dict[str, Any], peer: PeerIdentity) -> Dict[str, Any]:
        share = self._share_or_fail(request.get('share'))
        self._authorizer(peer).check(share.declaration, Permission.READ)
        target = resolve_in_share(share.path, str(request.get('path', '.')))
        if not os.path.isfile(target):
            raise RemoteFailure('not_found', f'不是普通文件: {request.get("path")!r}')
        size = os.path.getsize(target)
        return {'status': 'ok', 'share': share.share_id, 'path': request.get('path'),
                'size': size, 'name': os.path.basename(target)}

    def _op_read(self, request: Dict[str, Any], peer: PeerIdentity) -> Dict[str, Any]:
        """读取一段文件内容。支持 offset/length，便于分段传输大文件。"""
        share = self._share_or_fail(request.get('share'))
        self._authorizer(peer).check(share.declaration, Permission.READ)

        relative = str(request.get('path', '.'))
        target = resolve_in_share(share.path, relative)
        if os.path.isdir(target):
            return self._list_directory(share, target, relative)
        if not os.path.isfile(target):
            raise RemoteFailure('not_found', f'文件不存在: {relative!r}')

        offset = request.get('offset', 0)
        length = request.get('length', 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise RemoteFailure('bad_request', 'offset 必须是非负整数')
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise RemoteFailure('bad_request', 'length 必须是非负整数')
        size = os.path.getsize(target)
        if offset > size:
            raise RemoteFailure('bad_request', f'offset 越界（{offset} > {size}）')
        length = min(length or size - offset, size - offset)

        with open(target, 'rb') as handle:
            handle.seek(offset)
            data = handle.read(length)
        self._record(peer, 'read', share.share_id, relative, len(data))
        return {'status': 'ok', 'share': share.share_id, 'path': relative, 'offset': offset,
                'size': size, 'length': len(data), 'eof': offset + len(data) >= size,
                'data': base64.b64encode(data).decode('ascii')}

    def _op_write(self, request: Dict[str, Any], peer: PeerIdentity) -> Dict[str, Any]:
        share = self._share_or_fail(request.get('share'))
        self._authorizer(peer).check(share.declaration, Permission.WRITE)

        relative = str(request.get('path', ''))
        if not relative or relative.endswith('/'):
            raise RemoteFailure('bad_request', '写入必须给出文件名')
        target = resolve_in_share(share.path, relative)
        data_b64 = request.get('data')
        if not isinstance(data_b64, str):
            raise RemoteFailure('bad_request', 'data 必须是 base64 字符串')
        try:
            data = base64.b64decode(data_b64, validate=True)
        except Exception as e:
            raise RemoteFailure('bad_request', f'data 不是合法 base64: {e}') from e
        if len(data) > MAX_WRITE_BYTES:
            raise RemoteFailure('too_large', f'单次写入不得超过 {MAX_WRITE_BYTES} 字节')

        # 容量上限（§6.5）：授予写权限等于允许对方占用磁盘
        if share.max_bytes is not None:
            existing = os.path.getsize(target) if os.path.exists(target) else 0
            projected = self._share_usage(share) - existing + len(data)
            if projected > share.max_bytes:
                raise RemoteFailure(
                    'quota_exceeded',
                    f'超出共享项容量上限（{projected} > {share.max_bytes} 字节）')

        os.makedirs(os.path.dirname(target) or share.path, exist_ok=True)
        append = bool(request.get('append'))
        with open(target, 'ab' if append else 'wb') as handle:
            handle.write(data)
        self._record(peer, 'write', share.share_id, relative, len(data))
        return {'status': 'ok', 'share': share.share_id, 'path': relative,
                'written': len(data), 'total': os.path.getsize(target)}

    def _op_delete(self, request: Dict[str, Any], peer: PeerIdentity) -> Dict[str, Any]:
        """删除。

        §6.2：**删除权完全由 ACL 决定，不由归属推断** —— 不查"是谁上传的"，
        只查 ACL 的 delete 项。§6.4 要求不做物理删除（先进应用级回收站），
        本 MVP 尚未实现回收站，见实现路径文档 P2。
        """
        share = self._share_or_fail(request.get('share'))
        self._authorizer(peer).check(share.declaration, Permission.DELETE)
        relative = str(request.get('path', ''))
        target = resolve_in_share(share.path, relative)
        if not os.path.isfile(target):
            raise RemoteFailure('not_found', f'文件不存在: {relative!r}')
        size = os.path.getsize(target)
        os.remove(target)
        self._record(peer, 'delete', share.share_id, relative, size)
        return {'status': 'ok', 'share': share.share_id, 'path': relative, 'deleted': size,
                'note': 'MVP 为物理删除，应用级回收站未实现'}

    def _op_registry(self, request: Dict[str, Any], peer: PeerIdentity) -> Dict[str, Any]:
        """交换注册表快照（§7.3）。

        `action=list` 返回本地快照；`action=push` 接收对端快照并合并。
        记录一律经 `Registry.add()` 校验设备自签与 seq 单调性，因此对端无法伪造。
        """
        action = request.get('action', 'list')
        if self.registry is None:
            raise RemoteFailure('unavailable', '本节点未启用注册表')
        if action == 'list':
            return {'status': 'ok', 'records': self.registry.snapshot_dicts()}
        if action == 'push':
            incoming = Registry.from_snapshot_dicts(request.get('records'))
            accepted = self.registry.merge(incoming)
            return {'status': 'ok', 'accepted': accepted, 'total': len(self.registry)}
        raise RemoteFailure('bad_request', f'未知的 registry action: {action!r}')

    # ── 辅助 ──────────────────────────────────────────────────────────────

    def _list_directory(self, share: LocalShare, target: str, relative: str) -> Dict[str, Any]:
        entries = []
        with os.scandir(target) as iterator:
            for entry in iterator:
                if len(entries) >= MAX_LIST_ENTRIES:
                    raise RemoteFailure('too_large',
                                        f'目录项超过 {MAX_LIST_ENTRIES}，请缩小范围')
                try:
                    is_dir = entry.is_dir()
                    size = entry.stat().st_size if not is_dir else 0
                except OSError:
                    continue
                entries.append({'name': entry.name, 'dir': is_dir, 'size': size})
        entries.sort(key=lambda e: (not e['dir'], e['name']))
        return {'status': 'ok', 'share': share.share_id, 'path': relative or '.',
                'dir': True, 'entries': entries}

    def _share_usage(self, share: LocalShare) -> int:
        total = 0
        for base, _dirs, files in os.walk(share.path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    continue
        return total

    def _record(self, peer: PeerIdentity, action: str, share_id: str, path: str,
                size: int) -> None:
        import time
        self.audit.append({'ts': int(time.time()), 'actor': peer.principal_id,
                           'device': peer.device_id, 'action': action,
                           'share': share_id, 'path': path, 'bytes': size})

    # ── 注册自己的记录 ────────────────────────────────────────────────────

    def make_registration(self, endpoints: List[Tuple[str, int]], seq: int) -> Registration:
        return new_registration(device_key=self.device_key,
                                device_private_key=self.identity.device.private_key,
                                seq=seq, endpoints=endpoints, shares=sorted(self.shares))


class RemoteFailure(Exception):
    """需要以特定错误码回给对端的失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── 监听循环 ──────────────────────────────────────────────────────────────

def serve(host: str, port: int, identity: Identity, roster: Optional[Roster],
          shares: Optional[Dict[str, LocalShare]] = None,
          registry: Optional[Registry] = None,
          roster_loader: Optional[Callable[[], Optional[Roster]]] = None,
          ready: Optional[Callable[[socket.socket], None]] = None,
          on_error: Optional[Callable[[socket.socket, BaseException], None]] = None,
          stop_requested: Optional[Callable[[], bool]] = None) -> None:
    """在一个 TCP 端口上服务任何已加入团体的设备。

    单一连接失败只影响那条连接：握手或请求出错即断开，监听循环继续
    （无中心系统里，任何成员都可以随时上线/离线）。

    `roster_loader` 让长驻节点能拿到**最新**名单（见 `Node.current_roster`）。

    `on_error` 在监听套接字创建后、`bind`/`listen` 失败时被调用（随后该套接字
    会被关闭并把异常抛出）。存在的理由与 `ready` 对称：调用方要能知道"是哪个
    套接字绑失败了"，测试也要能在**关闭之后**判定它确实被关了 —— 只看异常是判不到的
    （CPython 的引用计数/循环 GC 会在异常传播时顺手回收那个 socket，于是"没显式
    close"这条缺陷在测试里观察不到，实测确认过）。
    """
    node = Node(identity=identity, roster=roster, shares=dict(shares or {}),
                registry=registry, roster_loader=roster_loader,
                stop_requested=stop_requested)
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        # bind/listen 必须在 try 之内：**实测踩到过描述符泄漏** —— 服务重启窗口期
        # 旧进程还占着端口时 bind 抛 EADDRINUSE，而这个 socket 建在 try 之外，
        # 于是它既没被 close() 也没人再引用，却仍以 LISTEN 状态占着端口（`ss` 显示
        # 该 socket 归 omnibox-web.service 的 cgroup，但当前进程里已经找不到它）。
        # 后果是端口**永远显示被占用**：之后每次重试都 EADDRINUSE，节点再也起不来，
        # 只能靠重启整个服务释放。
        listener.bind((host, port))
        listener.listen(16)
    except BaseException as exc:
        if on_error is not None:
            try:
                on_error(listener, exc)
            except Exception:
                pass
        listener.close()
        raise
    # accept 的轮询间隔（秒）。为什么不用"另一个线程 close() 套接字来打断 accept"：
    # **Windows 上从另一个线程 closesocket() 并不保证解开阻塞中的 accept()**，于是
    # 停止线程可能永远卡在那里、监听套接字也关不掉（实测症状：界面点"停止节点"
    # 没有效果，端口一直被占、再启动就是 EADDRINUSE）。改成给监听套接字设一个短
    # 超时，accept 定期返回，循环里检查停止事件 —— 这在两个平台上都靠得住。
    ACCEPT_POLL_SECONDS = 0.5

    listener.settimeout(ACCEPT_POLL_SECONDS)
    if ready is not None:
        ready(listener)
    handlers: List[threading.Thread] = []
    try:
        while not (node.stop_requested is not None and node.stop_requested()):
            try:
                sock, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break          # 套接字已被关闭（stop() 走的是这条路）
            # **每条连接单独开线程**，不在这里同步处理。为什么必须这样：`_serve_one`
            # 会跑完整的 Noise 握手并阻塞在 socket 上（超时 60 秒），而"从另一个线程
            # 关闭套接字"在两个平台上都不保证解开阻塞中的 recv —— 于是只要有一个
            # 半死不活的连接（对端建连到一半就没了），accept 循环就回不到检查停止
            # 标志的地方：表现为停止超时、端口放不出来、再启动 EADDRINUSE。
            # 实测日志：连续多条"停止节点超时：监听线程没有在 8 秒内退出"。
            _spawn_handler(sock, address, node, handlers)
    finally:
        listener.close()
        # 给处理中的连接一点收尾时间（它们各自有自己的 socket 超时兜底）
        for handler in handlers:
            handler.join(timeout=1.0)


def _spawn_handler(sock: socket.socket, address: Any, node: Node,
                   handlers: List[threading.Thread]) -> None:
    """在独立线程里处理一条连接（见 serve() 里为什么要单开线程）。

    线程是 daemon：进程退出时不阻塞；同时登记到 `handlers` 里，停止时给它们一点
    收尾时间。处理完从列表里摘掉，避免长驻节点把已完成线程越攒越多。
    """
    def run() -> None:
        try:
            _serve_one(sock, address, node)
        finally:
            try:
                handlers.remove(threading.current_thread())
            except ValueError:
                pass

    thread = threading.Thread(target=run, name='group-mesh-conn', daemon=True)
    handlers.append(thread)
    thread.start()


def _serve_one(sock: socket.socket, address: Any, node: Node) -> None:
    """处理一条连接：协商 → 握手 → 请求应答循环，直到对端断开或出错。"""
    sock.settimeout(CONNECTION_TIMEOUT_SECONDS)
    try:
        negotiation = exchange_hello(sock)
        # 每条连接都用最新名单授权：长驻节点期间名单可能已变（群主加人）
        session, peer = run_handshake(sock, node.identity, node.current_roster(), negotiation,
                                      initiator=False)
        connection = Connection(sock, session, peer, negotiation)
        while True:
            try:
                request = connection.recv()
            except TransportError:
                break
            connection.send(node.handle(request, peer))
    except (TransportError, OSError, AclDenied):
        # 认证失败、对端半途断开、路径越界：都只影响这条连接
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f'处理连接 {address} 时异常: {e}')
    finally:
        try:
            sock.close()
        except OSError:
            pass
