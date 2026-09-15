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
    """上传内容到对端共享项。服务端会按 ACL 的 write 项判定权限。"""
    return connection.request({'op': 'write', 'share': share_id, 'path': path,
                               'data': base64.b64encode(data).decode('ascii'),
                               'append': append})


def push_file(connection: Connection, share_id: str, local_path: Path,
              remote_path: str) -> int:
    local_path = Path(local_path)
    written = 0
    with open(local_path, 'rb') as handle:
        while True:
            block = handle.read(DEFAULT_CHUNK_BYTES)
            if not block:
                break
            push_bytes(connection, share_id, remote_path, block, append=written > 0)
            written += len(block)
    return written


def delete_remote(connection: Connection, share_id: str, path: str) -> Dict[str, Any]:
    return connection.request({'op': 'delete', 'share': share_id, 'path': path})


def fetch_registry(connection: Connection) -> Registry:
    """取对端注册表快照（§7.3）。"""
    response = connection.request({'op': 'registry', 'action': 'list'})
    return Registry.from_snapshot_dicts(response.get('records'))


def push_registry(connection: Connection, registry: Registry) -> Dict[str, Any]:
    return connection.request({'op': 'registry', 'action': 'push',
                               'records': registry.snapshot_dicts()})
