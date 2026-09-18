"""远端浏览与取回（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from shell.backend.plugin_utils import load_sibling
from shell.groupmesh import client as mesh_client
from shell.groupmesh.transport import RemoteError, TransportError

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
_opts = _common._opts

class RemoteMixin:
    """列对端共享项 / 目录，以及把单个文件取回本机下载目录。"""

    def list_remote(self, opts: Any = None, device_id: str = '', share_id: str = '',
                    path: str = '.') -> Dict[str, Any]:
        """列对端的共享项，或列某个共享项里的目录。

        `device_id` 可以留空：引导阶段只填了地址时，直接连那个地址，对端会在应答里
        报出自己的身份（`peer_device_id`），界面据此补上设备 ID。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
        share_id = str(options.get('share_id') or share_id or '')
        path = str(options.get('path') or path or '.')

        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}

        candidates = self._manual_endpoints(device_id, roster)
        if not candidates:
            return {'success': False,
                    'error': '还没有可用的对端地址。请先在"对端"里登记一台设备的地址，'
                             '或刷新一次让本机通过注册记录发现它'}

        last_error = ''
        for endpoint, label in candidates:
            try:
                connection = self._connect(endpoint, identity, roster)
            except (RemoteError, TransportError, OSError) as e:
                last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                continue
            peer_id = connection.peer.device_key.hex()
            try:
                if not share_id:
                    shares = mesh_client.list_shares(connection)
                    return {'success': True, 'device_id': peer_id, 'peer_device_id': peer_id,
                            'name': label or connection.peer.name,
                            'endpoint': list(endpoint), 'shares': shares}
                result = mesh_client.list_directory(connection, share_id, path)
            except RemoteError as e:
                # 确定性错误（无权限/没有这个共享项/路径不存在）：**不换设备重试**，
                # 否则真正的拒绝原因会被"另一台也连不上"覆盖掉。
                return {'success': False, 'error': str(e), 'device_id': device_id,
                        'peer_device_id': peer_id, 'share_id': share_id, 'path': path}
            except (TransportError, OSError) as e:
                last_error = f'与 {label or peer_id} 的连接中断: {e}'
                continue
            finally:
                self._release(connection)

            return {'success': True, 'device_id': peer_id, 'peer_device_id': peer_id,
                    'name': label or connection.peer.name,
                    'endpoint': list(endpoint), 'share_id': share_id,
                    'path': result.get('path') or path,
                    'dir': bool(result.get('dir')),
                    'entries': result.get('entries') or []}

        return {'success': False, 'error': last_error or '所有候选地址都连不上',
                'device_id': device_id, 'offline': True}

    def download_remote(self, opts: Any = None, device_id: str = '', share_id: str = '',
                        path: str = '', overwrite: bool = False) -> Dict[str, Any]:
        """把对端共享项里的一个文件取回本机（落进下载目录）。

        落点在 `downloads_dir`（设置项 `download_dir` 可改）。文件名保留对端的
        相对结构：`<共享标识>/<相对路径>` —— 只用文件名会把对端两个同名文件
        撞成一个，而相对路径同时也是 `list_remote` 返回的 `path`，界面据此直接
        拼出可访问的本机路径。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '')
        share_id = str(options.get('share_id') or share_id or '')
        path = str(options.get('path') or path or '')
        overwrite = bool(options.get('overwrite', overwrite))

        if not share_id or not path:
            return {'success': False, 'error': '必须给出 share_id 与 path'}

        # 目标路径必须落在下载目录之内：对端给的相对路径可能带 `..`，
        # 直接拼接会让它写到下载目录之外（内核会拒绝越界的共享内路径，
        # 但本机这一侧的目标路径同样必须校验）。
        # 这一步刻意放在解析设备之前：它是纯粹的本地校验，不该因为"设备没填/不可达"
        # 而改变结论，也让用例可以在离线状态下验证它。
        relative = Path(str(path).replace('\\', '/').lstrip('/'))
        if relative.is_absolute() or any(part == '..' for part in relative.parts):
            return {'success': False, 'error': f'路径越界: {path!r}'}
        destination = self.downloads_dir / share_id / relative
        try:
            destination.resolve().relative_to(self.downloads_dir.resolve())
        except ValueError:
            return {'success': False, 'error': f'路径越界: {path!r}'}
        if destination.exists() and not overwrite:
            return {'success': True, 'skipped': True, 'local_path': str(destination),
                    'reason': '本机已有同名文件（overwrite=true 可覆盖）'}

        return self._fetch_to(destination, device_id, share_id, str(path))

    def _fetch_to(self, destination: Path, device_id: str, share_id: str,
                  path: str) -> Dict[str, Any]:
        """把对端共享项里的一个文件取到**指定路径**（不做越界校验，调用方负责）。

        为什么从 `download_remote` 里抽出来：按需取字节（`ensure_file`）要落到物化
        暂存目录，而用户显式下载要落到下载目录 —— 除了目标路径，连接、候选端点重试、
        "确定性错误不换设备重试"这些分支完全一样。各写一份的话两边迟早漂移
        （一边记得不重试、另一边忘了，真正的拒绝原因被"另一台也连不上"盖掉）。
        """
        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}

        candidates = self._manual_endpoints(device_id, roster)
        if not candidates:
            return {'success': False,
                    'error': '还没有可用的对端地址。请先在"对端"里登记一台设备的地址，'
                             '或刷新一次让本机通过注册记录发现它'}

        last_error = ''
        for endpoint, label in candidates:
            try:
                connection = self._connect(endpoint, identity, roster)
            except (RemoteError, TransportError, OSError) as e:
                last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                continue
            peer_id = connection.peer.device_key.hex()
            try:
                written = mesh_client.fetch_to_file(connection, share_id, path, destination)
            except RemoteError as e:
                # 确定性错误（无权限/文件不存在）：不换设备重试
                return {'success': False, 'error': str(e), 'device_id': peer_id,
                        'peer_device_id': peer_id, 'share_id': share_id, 'path': path}
            except (TransportError, OSError) as e:
                last_error = f'传输失败: {e}'
                continue
            finally:
                self._release(connection)

            try:
                size = destination.stat().st_size
            except OSError:
                # 落位成功但读不到（被杀毒/索引服务短暂占用等）：字节数还准，别把一次
                # 成功的取回报成失败。
                size = written
            return {'success': True, 'device_id': peer_id, 'peer_device_id': peer_id,
                    'name': label or connection.peer.name,
                    'share_id': share_id, 'path': path, 'local_path': str(destination),
                    'bytes': written, 'size': size}

        return {'success': False, 'error': last_error or '所有候选地址都连不上',
                'device_id': device_id, 'offline': True}
