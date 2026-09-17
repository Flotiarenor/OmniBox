"""客户端：把协议层的能力包成可直接调用的函数。

CLI 与测试都走这一层，避免"测试里跑一套、命令行里跑另一套"。

分块读取的循环放在这里：服务端单条应答有大小上限，客户端负责把大文件按
`DEFAULT_CHUNK_BYTES` 一段段取回来。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .identity import Identity
from .registry import Registry
from .roster import Roster
from .transport import DEFAULT_CHUNK_BYTES, Connection, RemoteError, connect

ProgressFn = Callable[[int, int], None]


def open_connection(host: str, port: int, identity: Identity, roster: Optional[Roster],
                    timeout: float = 20.0) -> Connection:
    return connect(host, port, identity, roster, timeout=timeout)


def list_shares(connection: Connection) -> List[Dict[str, Any]]:
    """列出对端允许本主体读取的共享项。"""
    return list(connection.request({'op': 'list'}).get('shares') or [])


def stat_share(connection: Connection, share_id: str, path: str) -> Dict[str, Any]:
    return connection.request({'op': 'stat', 'share': share_id, 'path': path})


def list_directory(connection: Connection, share_id: str, path: str = '.') -> Dict[str, Any]:
    return connection.request({'op': 'read', 'share': share_id, 'path': path})


def fetch_bytes(connection: Connection, share_id: str, path: str,
                chunk_bytes: int = DEFAULT_CHUNK_BYTES,
                progress: Optional[ProgressFn] = None) -> bytes:
    """取回一个文件的全部内容（内部按块循环）。"""
    chunks: List[bytes] = []
    offset = 0
    total = 0
    first = True
    while True:
        response = connection.request({'op': 'read', 'share': share_id, 'path': path,
                                       'offset': offset, 'length': chunk_bytes})
        if response.get('dir'):
            raise RemoteError(f'{path!r} 是目录，不是文件')
        data = base64.b64decode(response.get('data') or '')
        if first:
            total = int(response.get('size') or 0)
            first = False
        if not data:
            break
        chunks.append(data)
        offset += len(data)
        if progress is not None:
            progress(offset, total)
        if response.get('eof'):
            break
        if len(data) < chunk_bytes:
            break
    return b''.join(chunks)


def fetch_to_file(connection: Connection, share_id: str, path: str, destination: Path,
                  chunk_bytes: int = DEFAULT_CHUNK_BYTES,
                  progress: Optional[ProgressFn] = None) -> int:
    """把远端共享项里的文件下载到本地路径，返回写入的字节数。"""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    offset = 0
    tmp = destination.with_suffix(destination.suffix + '.part')
    with open(tmp, 'wb') as handle:
        while True:
            response = connection.request({'op': 'read', 'share': share_id, 'path': path,
                                           'offset': offset, 'length': chunk_bytes})
            data = base64.b64decode(response.get('data') or '')
            if not data:
                break
            handle.write(data)
            written += len(data)
            offset += len(data)
            if progress is not None:
                progress(offset, int(response.get('size') or 0))
            if response.get('eof'):
                break
            if len(data) < chunk_bytes:
                break
    os.replace(tmp, destination)
    return written


def push_bytes(connection: Connection, share_id: str, path: str, data: bytes,
               append: bool = False) -> Dict[str, Any]:
    """上传一段内容到对端共享项。服务端会按 ACL 的 write 项判定权限。"""
    return connection.request({'op': 'write', 'share': share_id, 'path': path,
                               'data': base64.b64encode(data).decode('ascii'),
                               'append': append})


class UploadCancelled(Exception):
    """上传被调用方主动取消（`cancelled` 回调返回真）。

    单独一个异常类型：调用方要能把"用户点了取消"与"传输失败"分开 —— 前者不该
    报错、也不该重试，而对端暂存的 `.part` 是**要保留**的（它就是续传的起点）。
    """


def probe_upload(connection: Connection, share_id: str, path: str) -> Dict[str, Any]:
    """问对端：这个目标已经收下多少字节（续传起点）。

    需要 **write** 权限而不是 read —— 上传方可能只有写权限（§6.2 的两档是独立的）。
    """
    return connection.request({'op': 'write', 'share': share_id, 'path': path,
                               'part': True, 'probe': True})


def push_file(connection: Connection, share_id: str, local_path: Path,
              remote_path: str, overwrite: bool = False, offset: int = 0,
              chunk_bytes: int = DEFAULT_CHUNK_BYTES,
              progress: Optional[ProgressFn] = None,
              cancelled: Optional[Callable[[], bool]] = None) -> int:
    """把本地文件上传到对端共享项，返回**上传完成后对端持有的字节数**。

    走**暂存 + 提交**：分块写进对端的 `<目标>.part`，最后一块带 `eof=True`，由服务端
    原子改名成目标文件。这样中途断线（或进程被杀）只会在属主磁盘上留下 `.part`，
    不会把目标文件截断成半份 —— 直接覆写目标文件的话，断线等于毁掉对端已有的那份。

    续传：`offset` 是"对端已经收下的字节数"，调用方先用 `probe_upload()` 问出来，
    确认它与本地文件是同一份内容的前缀之后再传。每个分块都带 `offset`，对端会核对
    （不一致就回 `offset_mismatch`），因此两个上传方抢同一个目标不会拼出垃圾。

    取消：`cancelled()` 返回真时抛 `UploadCancelled`；此时抛出的位置是**分块边界**，
    已传的分块留在对端 `.part` 里，下次带同样的 `offset` 继续即可。

    目标已存在且 `overwrite=False` 时服务端会在第一个分块就回 `exists` 拒绝。
    """
    local_path = Path(local_path)
    total = local_path.stat().st_size
    sent = offset
    pushed = False
    if progress is not None:
        progress(sent, total)
    with open(local_path, 'rb') as handle:
        handle.seek(offset)
        while True:
            if cancelled is not None and cancelled():
                raise UploadCancelled(f'已传 {sent} / {total} 字节')
            block = handle.read(chunk_bytes)
            if not block:
                break
            # 预读一个字节判断这是不是最后一块：用文件大小算"最后一块"在上传过程中
            # 文件被追加时会永远等不到 eof，对端就只剩一个 .part。
            tail = handle.read(1)
            last = not tail
            if tail:
                handle.seek(-1, os.SEEK_CUR)
            response = connection.request({
                'op': 'write', 'share': share_id, 'path': remote_path,
                'data': base64.b64encode(block).decode('ascii'),
                'part': True, 'offset': sent, 'eof': last, 'overwrite': overwrite})
            pushed = True
            sent += len(block)
            # 对端回的游标必须与本地账一致：不一致说明有别人在写同一个暂存文件，
            # 继续传只会拼出更坏的结果。
            reported = response.get('offset')
            if isinstance(reported, int) and reported != sent:
                raise RemoteError(f'对端游标 {reported} 与本地 {sent} 不一致', 'offset_mismatch')
            if progress is not None:
                progress(sent, total)
    if not pushed:
        # 一个分块都没发：要么是空文件，要么**整个文件已经在对方暂存里**（上次传完但
        # 没提交）。两种情况都必须补一次提交，否则对端只剩一个 `.part`。
        connection.request({'op': 'write', 'share': share_id, 'path': remote_path,
                            'data': '', 'part': True, 'offset': sent, 'eof': True,
                            'overwrite': overwrite})
    return sent


def fetch_registry(connection: Connection) -> Registry:
    """取对端注册表快照（§7.3）。"""
    response = connection.request({'op': 'registry', 'action': 'list'})
    return Registry.from_snapshot_dicts(response.get('records'))


def push_registry(connection: Connection, registry: Registry) -> Dict[str, Any]:
    return connection.request({'op': 'registry', 'action': 'push',
                               'records': registry.snapshot_dicts()})


# ── 名单分发（§5.7）───────────────────────────────────────────────────────
#
# 两个方向都要带"我知道哪些历史名单"：`list` 用它换准入（对端据此判断我们确实
# 属于这个团体、只是版本旧），`push` 用它让对端把新名单推给我们。
# 为什么两个方向都带：新成员手里只有旧名单，若不带历史，对端无法区分
# "团体的老成员"与"随便一个拿到端点的陌生人"。

def fetch_roster(connection: Connection, local: Optional[Roster],
                 history: Optional[List[Roster]] = None) -> Optional[Roster]:
    """从对端拉一份名单；对端没有名单时返回 None。

    调用方拿到之后**必须**自己跑 `Roster.accepts(current)`：本函数只负责传输，
    不做验证 —— 验证需要"本机当前名单"这个上下文，而它属于调用方（§5.2 规则 1–6）。
    """
    response = connection.request(_roster_payload('list', local, history))
    payload = response.get('roster')
    if not isinstance(payload, dict):
        return None
    return Roster.from_dict(payload)


def push_roster(connection: Connection, roster: Roster,
                history: Optional[List[Roster]] = None) -> Dict[str, Any]:
    """把本机名单推给对端，由对端按规则 1–6 决定采纳与否（§5.7）。"""
    request = _roster_payload('push', roster, history)
    request['roster'] = roster.to_dict()
    return connection.request(request)


def _roster_payload(action: str, local: Optional[Roster],
                     history: Optional[List[Roster]]) -> Dict[str, Any]:
    """构造 roster 请求，带上"本机已知的历史名单"（新→旧，去重）。"""
    known = list(history or [])
    if local is not None:
        known.append(local)
    chain = Roster.history_chain(*known) if known else []
    request: Dict[str, Any] = {'op': 'roster', 'action': action,
                               'history': [item.to_dict() for item in chain]}
    if local is not None:
        request['roster'] = local.to_dict()
    return request
