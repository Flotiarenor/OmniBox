"""镜像到用户目录（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from shell.backend.plugin_utils import load_sibling
from shell.groupmesh import client as mesh_client
from shell.groupmesh.transport import RemoteError, TransportError

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
_opts = _common._opts
MIRROR_MTIME_WINDOW_NS = _common.MIRROR_MTIME_WINDOW_NS

class MirrorMixin:
    """把一个共享项完整取到用户指定的本地目录（「网络位置」用，字节全取不留占位）。"""

    def mirror_share(self, opts: Any = None, device_id: str = '', share_id: str = '',
                     destination: str = '', max_entries: int = 20000,
                     max_bytes: int = 0) -> Dict[str, Any]:
        """把一个共享项**完整取到用户指定的本地目录**（"网络位置"用）。

        与 `materialize_remote()` 的三点区别，每一点都是"网络位置"能不能用的前提：

        1. **目标目录由用户指定**（在他自己的图库/媒体盘里），不是插件的 `.cache`；
        2. **字节全部取回，不留占位**：指定目录是普通目录，消费方（image-viewer 等）按
           本地文件工作、**没有** `ensure_file` 钩子可依赖 —— 留 0 字节占位就是宽高 0×0
           与整片 404 缩略图（实测见 `.dsh/group-mesh-materialize.md` §2.2）；
        3. **mtime 写成对端的值**：消费方的缓存失效读的是本地元数据，否则"对端换过"
           永远看不出来。

        幂等：大小与 mtime 都一致的文件直接跳过，所以重复执行只补差异。
        """
        options = _opts(opts)
        device_id = str(options.get('device_id') or device_id or '').strip().lower()
        share_id = str(options.get('share_id') or share_id or '')
        destination = str(options.get('destination') or destination or '')
        try:
            max_entries = int(options.get('max_entries', max_entries))
            max_bytes = int(options.get('max_bytes', max_bytes))
        except (TypeError, ValueError):
            return {'success': False, 'error': 'max_entries / max_bytes 必须是整数'}
        if not share_id or not destination:
            return {'success': False, 'error': '必须给出 share_id 与 destination'}

        raw_path = Path(destination).expanduser()
        # 必须是**绝对**路径：`C:Users...`（Windows 驱动器相对路径）与相对路径都会在
        # resolve() 之后变成某个"看起来正常"的位置，然后就在那里建目录 —— 实测踩到过
        # 一次分隔符被吃光的路径（`C:UsersADMINI~1AppDataLocalTemp...`），resolve 后
        # 落在盘符根上。校验必须在 resolve **之前**做，否则拿到的是已经被解析过的路径。
        if not raw_path.is_absolute():
            return {'success': False,
                    'error': f'目标目录必须是绝对路径（形如 D:\\图库\\相册 或 /home/me/pics）: '
                             f'{destination!r}'}
        try:
            root = raw_path.resolve()
        except OSError as e:
            return {'success': False, 'error': f'目标目录无法解析: {e}'}
        # 边界只挡**本插件自己的**数据目录与身份目录，不挡全局数据根：默认配置下
        # 全局数据根（`./data`）正是 image-viewer 的图片库，用户把"网络位置"放进
        # 自己的图库是完全正当的用法（那样它就是一个普通文件夹，原图也能直接打开）。
        # `add_share` 那条"不得位于数据根之内"是另一个问题（对外**暴露**文件），
        # 不要照抄到这里 —— 照抄会让默认配置下最自然的目标目录被拒。
        forbidden = [self.get_data_root(), self.identity_dir]
        for guarded in forbidden:
            try:
                root.relative_to(guarded)
            except ValueError:
                continue
            return {'success': False,
                    'error': f'目标目录不得位于 {guarded} 之内（程序数据与身份私钥）'}
        if self.is_protected_path(root):
            return {'success': False, 'error': f'目标目录是受保护路径: {root}'}

        # 目录可以不存在：用户很自然会说"放到图库下这个新文件夹里"。**但只有上级目录
        # 存在时才建** —— 否则一个手滑/粘贴丢了分隔符的路径会在盘符根上造出一个莫名其妙的
        # 目录（实测踩到：粘贴丢反斜杠后报"目标目录不存在"，用户完全看不出哪里错了）。
        if not root.is_dir():
            if not root.parent.is_dir():
                return {'success': False,
                        'error': f'目标目录不存在，且它的上级目录也不存在: {root}\n'
                                 f'（请用「浏览…」选一个已存在的目录，或先创建上级目录 '
                                 f'{root.parent}）'}
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return {'success': False, 'error': f'无法创建目标目录 {root}: {e}'}

        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}
        candidates = self._manual_endpoints(device_id, roster)
        if not candidates:
            return {'success': False, 'error': '还没有可用的对端地址（见"添加对端"）'}

        last_error = ''
        for endpoint, label in candidates:
            try:
                connection = self._connect(endpoint, identity, roster)
            except (RemoteError, TransportError, OSError) as e:
                last_error = f'{label or endpoint[0]}:{endpoint[1]} 连不上: {e}'
                continue
            try:
                walked = self._walk_remote(connection, share_id, '.', 64, max_entries)
                result = self._mirror_files(connection, share_id, root, walked, max_bytes)
            except RemoteError as e:
                return {'success': False, 'error': str(e), 'share_id': share_id}
            except (TransportError, OSError) as e:
                last_error = f'传输中断: {e}'
                continue
            finally:
                self._release(connection)
            device = connection.peer.device_key.hex()
            return {'success': True, 'device_id': device, 'name': label or connection.peer.name,
                    'share_id': share_id, 'root': str(root), 'entries': len(walked),
                    'truncated': len(walked) >= max_entries, **result}

        return {'success': False, 'error': last_error or '所有候选地址都连不上',
                'device_id': device_id, 'offline': True}

    def _mirror_files(self, connection: Any, share_id: str, root: Path,
                      walked: Dict[str, Dict[str, Any]], max_bytes: int) -> Dict[str, Any]:
        """把 `walked` 里的文件逐个取到 `root`（增量：一致就跳过），返回统计。

        连接由调用方持有并复用：一次取几十上百个文件时，逐文件建连会把"一次拷贝"
        变成几十次 Noise 握手。单个文件失败不中断整批 —— 汇报出来比整个失败有用。
        """
        created_dirs = 0
        fetched = 0
        unchanged = 0
        written_bytes = 0
        truncated = False
        errors: List[str] = []
        for rel, info in sorted(walked.items()):
            # 目标路径由"用户指定目录 + 对端给的相对路径"拼成，对端那一半不可信：
            # 绝对路径或 `..` 段能让一次"取回"覆盖根外的任意文件（审计项 P1-5）。
            # 归一与拒绝规则在 _common.safe_rel / _common.within_root，与物化共用。
            target = _common.within_root(root, rel)
            if target is None:
                log.warning(f'[group-mesh] 忽略越界的远端条目: {rel!r}')
                errors.append(f'{rel}: 路径越界，已忽略')
                continue
            if info['dir']:
                try:
                    target.mkdir(parents=True, exist_ok=True)
                    created_dirs += 1
                except OSError as e:
                    errors.append(f'{rel}: {e}')
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                errors.append(f'{rel}: {e}')
                continue
            if self._mirror_file_current(target, info):
                unchanged += 1
                continue
            if max_bytes and written_bytes + int(info['size']) > max_bytes:
                truncated = True
                continue
            try:
                written_bytes += mesh_client.fetch_to_file(connection, share_id, rel, target)
            except (RemoteError, TransportError, OSError) as e:
                errors.append(f'{rel}: {e}')
                continue
            # 真字节到位后再写 mtime：写入本身会把 mtime 刷成"现在"
            self._apply_remote_mtime(target, info.get('mtime_ns'))
            fetched += 1
        return {'dirs': created_dirs, 'fetched': fetched, 'unchanged': unchanged,
                'bytes': written_bytes, 'truncated_bytes': truncated,
                'errors': errors[:20], 'error_count': len(errors)}

    def _mirror_file_current(self, target: Path, info: Dict[str, Any]) -> bool:
        """镜像目标是否已经与对端一致（大小相同，且 mtime 相同或对端没给 mtime）。

        mtime 用窗口比较：本地文件系统会量化 `os.utime` 写下的纳秒值（FAT 只有 2 秒），
        要求逐 ns 相等会把"本来就是同一份"的文件判成需要重下，每次执行都全量重传。
        """
        try:
            local = target.stat()
        except OSError:
            return False
        if local.st_size != int(info.get('size') or 0):
            return False
        remote_mtime = info.get('mtime_ns')
        if remote_mtime is None:
            return True   # 对端没给 mtime（老内核）：只能靠大小
        return abs(local.st_mtime_ns - int(remote_mtime)) < MIRROR_MTIME_WINDOW_NS
