"""出站连接：界面侧复用的连接池 + 后台专用的短连接。

状态自持：连接池字典。复用与专用的分界**只在这里决定**（见下面两个方法的说明），
调用方不需要知道连接是怎么建起来的。

连接池与注册表共锁（装配期注入的同一把 `lock`）：迁移前 `_connect` 与
`_fetch_registry_from` 的合并、共享项读改写用的是同一把锁，拆开会改变并发行为。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Tuple

from shell.groupmesh import client as mesh_client
from shell.groupmesh.identity import Identity
from shell.groupmesh.roster import Roster

log = logging.getLogger(__name__)

#: 探测端点的连接超时（秒）。刷新设备时要试多个端点，而设备常同时发布 IPv6 与
#: IPv4 —— IPv6 在本机没有路由时每次都要等满超时。内核默认 20 秒，实测 5 个端点
#: 串行等下来是 80 秒，界面看起来就是"卡住不动"。探测用短超时，真正的文件传输
#: 仍走内核默认值。
PROBE_TIMEOUT_SECONDS = 3.0

Endpoint = Tuple[str, int]


class ConnectionPool:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        # 已建立的出站连接：(设备公钥 bytes, host, port) -> Connection。
        # 复用的理由：一次请求就是一次 Noise 握手（多个往返 + 公钥运算），
        # 逐次建连会让"浏览一个目录"变成几个握手的开销。
        self._connections: Dict[Tuple[bytes, str, int], Any] = {}

    def get(self, endpoint: Endpoint, identity: Identity,
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

    def release(self, connection: Any) -> None:
        """归还连接。当前实现是复用的，因此这里不做任何事。

        保留这个调用点是为了让"以后改成短连接（用完即关）"只改这一个方法，
        而不用去翻每个调用方 —— 连接的生死只应在一处决定。
        """
        return

    def open_dedicated(self, endpoint: Endpoint, identity: Identity,
                       roster: Optional[Roster]) -> Any:
        """开一条**专用**连接（用完即关），不进入界面侧的复用池。

        后台同步线程与界面请求会并发；复用池里的 Connection 是"一条 Noise 连接上
        请求严格串行"的，两线程同时 send 会把帧拼坏。因此后台同步、以及显式探测，
        都走这里；界面的浏览/取文件继续用 `get()` 复用池。
        """
        return mesh_client.open_connection(endpoint[0], endpoint[1], identity, roster,
                                          timeout=PROBE_TIMEOUT_SECONDS)

    def close_all(self) -> None:
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
