"""共享项（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from shell.backend.plugin_utils import load_sibling
from shell.groupmesh.records import RecordError
from shell.groupmesh.shares import Acl, new_share

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
_opts = _common._opts

class ShareMixin:
    """共享根的挂载 / 移除 / 状态与用量（声明的落盘见 storage 分片）。"""

    def _root_state(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """一个共享根的可用性。只用 `is_dir()`，不遍历目录。

        容量统计（`_tree_usage`）刻意不在这里做：`G:\\图库` 那种目录走一遍可能要
        几十秒，而 `get_status()` 是首屏调用。界面要精确用量时调 `refresh_share_roots`。
        """
        raw = str(entry.get('path') or '')
        state: Dict[str, Any] = {
            'path': raw,
            'available': False,
            'max_bytes': entry.get('max_bytes'),
            'reason': None,
        }
        if not raw:
            state['reason'] = '未设置路径'
            return state
        try:
            target = Path(raw).expanduser()
            state['available'] = target.is_dir()
            if not state['available']:
                state['reason'] = '目录不存在或所在磁盘未接入'
        except OSError as e:
            state['reason'] = f'无法访问: {e}'
        return state

    def get_share_roots(self) -> List[Dict[str, Any]]:
        """本机全部共享根及其状态（界面的"共享项"页用）。"""
        roots = self._load_share_roots()
        shares = self._load_shares()
        result: List[Dict[str, Any]] = []
        for share_id, share in shares.items():
            entry = roots.get(share_id) or {'path': share.path,
                                            'max_bytes': share.max_bytes}
            result.append({'share_id': share_id,
                           'acl': share.declaration.acl.to_dict(),
                           **self._root_state(entry)})
        return result
    # ── 共享项 ────────────────────────────────────────────────────────────

    def add_share(self, opts: Any = None, share_id: str = '', path: str = '',
                  read: str = 'group', write: str = 'owner') -> Dict[str, Any]:
        """挂载一个本机共享项（默认**不限制容量**）。

        §6.5 的两条硬约束在插件层就挡掉，不推给用户自觉：
          * 共享根不得落在程序数据目录 / 身份目录之内（避免把私钥或程序本身共享出去）；
          * 共享根必须是已存在的目录。

        容量上限（`max_bytes`）**刻意不做成界面设置项、默认也不设限**：
        `node.py` 的判定是"每条连接量一次基线再推算"，实测 8 条并发连接能在
        "上限 1000 字节"的共享项上写进 3200 字节且全部成功 —— 它给不出可信保证，
        却会让人以为有防线。需要时仍可显式传入（`0` 或不传 = 不限制）。
        """
        options = _opts(opts)
        share_id = str(options.get('share_id') or share_id or '')
        path = str(options.get('path') or path or '')
        read = str(options.get('read') or read or 'group')
        write = str(options.get('write') or write or 'owner')

        identity = self._load_identity()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份'}

        target = Path(path).expanduser().resolve()
        if not target.is_dir():
            return {'success': False, 'error': f'共享根必须是已存在的目录: {target}'}

        forbidden = [Path(self.config['directories']['data_root']).resolve(), self.identity_dir]
        for guarded in forbidden:
            try:
                target.relative_to(guarded)
            except ValueError:
                continue
            return {'success': False,
                    'error': f'共享根不得位于 {guarded} 之内（那里是程序数据与身份私钥）'}

        # 容量上限：不传 / 传 0 / 传 null 一律表示不限制。保留显式传入的通道，
        # 但**界面不再发送硬编码的 1 GiB**（那既不是用户选的，也限不住并发上传）。
        limit: Optional[int] = None
        if 'max_bytes' in options and options['max_bytes'] not in (None, 0, '0', ''):
            try:
                limit = int(options['max_bytes'])
            except (TypeError, ValueError):
                return {'success': False,
                        'error': f'max_bytes 必须是整数、0 或 null（0/null = 不限制），'
                                 f'收到 {options["max_bytes"]!r}'}

        try:
            acl = Acl(read=read, write=write)
            share = new_share(share_id=share_id, path=str(target),
                              owner_key=identity.principal.public_key,
                              node_key=identity.device.public_key,
                              node_private_key=identity.device.private_key,
                              acl=acl, seq=1, max_bytes=limit)
        except (RecordError, ValueError) as e:
            return {'success': False, 'error': str(e)}

        with self._lock:
            shares = self._load_shares()
            shares[share_id] = share
            self._save_shares(shares)
        # 共享清单变了：立刻重发注册记录并 push 给对端；节点没跑时留给后台同步兜底。
        self._republish_registration()
        return {'success': True, 'share_id': share_id, 'path': str(target),
                'acl': acl.to_dict(), 'max_bytes': limit}

    def remove_share(self, opts: Any = None, share_id: str = '') -> Dict[str, Any]:
        """取消一个本机共享项（只删声明，不碰共享根里的文件）。"""
        options = _opts(opts)
        share_id = str(options.get('share_id') or share_id or '')
        with self._lock:
            shares = self._load_shares()
            if share_id not in shares:
                return {'success': False, 'error': f'没有共享项 {share_id}'}
            del shares[share_id]
            self._save_shares(shares)
        # 共享清单变了：立刻重发注册记录并 push 给对端。
        self._republish_registration()
        return {'success': True, 'share_id': share_id}

    def refresh_share_roots(self) -> Dict[str, Any]:
        """重扫本机共享根：更新可用性，并给出条目数与用量。

        只有用户点"刷新"时才走这里（目录树扫描可能几十秒），`get_status()` 永远
        是轻量的。用量在超过容量上限时提前停止累加，并在 `truncated` 里说明这个
        数字是下界还是精确值 —— 否则界面会把"扫到一半就够配额了"显示成精确用量。
        """
        result: List[Dict[str, Any]] = []
        roots = self._load_share_roots()
        for share_id, share in self._load_shares().items():
            entry = roots.get(share_id) or {'path': share.path, 'max_bytes': share.max_bytes}
            state = self._root_state(entry)
            item = {'share_id': share_id, **state}
            if state['available']:
                limit = state['max_bytes']
                try:
                    used, truncated = self._tree_usage(Path(state['path']).expanduser(),
                                                       limit=limit)
                except OSError as e:
                    item['available'] = False
                    item['reason'] = f'扫描失败: {e}'
                    result.append(item)
                    continue
                item['used_bytes'] = used
                item['truncated'] = truncated
                item['entries'] = self._count_entries(Path(state['path']).expanduser())
            result.append(item)
        return {'success': True, 'roots': result,
                'scanned_at': int(time.time())}

    @staticmethod
    def _count_entries(root: Path, max_entries: int = 200_000) -> int:
        """目录树里的条目数（文件 + 目录），用于"这个共享项有多大"的粗略提示。"""
        count = 0
        for _base, dirs, files in os.walk(root):
            count += len(dirs) + len(files)
            if count > max_entries:
                return max_entries
        return count
