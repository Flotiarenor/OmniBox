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

from shell.backend.freshness import engine_for
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

        容量统计（`_tree_usage`）刻意不在这里做：`D:\\图库` 那种目录走一遍可能要
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
        """重扫本机共享根：更新可用性，并给出条目数与用量（兼容入口）。

        实现已委托给壳的统一刷新基建（`system_freshness_verify` / 插件侧
        `engine.sync`）：本机共享根是"本地文件树 → 用量与条目数"的派生数据，
        判据（目录级短路、幽灵清理）统一由基建托管。新代码请用
        `system_freshness_sync` / `system_freshness_verify`。

        旧名字保留是因为它是**唯一**能拿到精确用量的入口（`get_status` 刻意不遍历
        目录，见 `_root_state`），而用量要显示在共享项卡片上。
        """
        engine = engine_for(self)
        if engine is not None:
            report = engine.sync('', force=True)
            return {'success': True, 'roots': self._share_roots_report(),
                    'scanned_at': int(time.time()),
                    'action': report.get('action'), 'changed': report.get('added', 0)}
        return {'success': True, 'roots': self._share_roots_report(),
                'scanned_at': int(time.time())}

    def _share_roots_report(self) -> List[Dict[str, Any]]:
        """每个共享根的可用性 + 用量 + 条目数（用量走缓存，见下）。"""
        result: List[Dict[str, Any]] = []
        roots = self._load_share_roots()
        for share_id, share in self._load_shares().items():
            entry = roots.get(share_id) or {'path': share.path, 'max_bytes': share.max_bytes}
            state = self._root_state(entry)
            item = {'share_id': share_id, **state}
            if state['available']:
                cached = self._usage_cache.get(share_id)
                if cached is None or cached.get('path') != state['path']:
                    cached = self._scan_share_usage(share_id, state)
                item.update(cached)
            result.append(item)
        return result

    def _scan_share_usage(self, share_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """算一个共享根的用量与条目数，写进缓存（同一次刷新同一目录只走一遍）。"""
        target = Path(state['path']).expanduser()
        try:
            used, truncated = self._tree_usage(target, limit=state['max_bytes'])
        except OSError as e:
            return {'available': False, 'reason': f'扫描失败: {e}'}
        usage = {
            'path': state['path'],
            'used_bytes': used,
            'truncated': truncated,
            'entries': self._count_entries(target),
        }
        self._usage_cache[share_id] = usage
        return usage

    # ===== 统一刷新基建（同步 / 校验）=====
    #
    # 本机共享根是"本地文件树 → 用量与条目数"的派生数据。旧实现把入口
    # `refresh_share_roots` 注册了却没有任何前端调用（实测全仓零引用），
    # 于是那三个字段（used_bytes / entries / scanned_at）从来没有被算出来过。
    # 接入基建后它由统一的「校验」驱动，结果进 `_usage_cache` 供共享项卡片读取。

    def freshness_spec(self) -> Dict[str, Any]:
        return {
            'roots': lambda: [Path(str(s.path)) for s in self._load_shares().values()
                              if str(s.path or '').strip()],
            'include': (),                 # 用量统计关心全部文件，不筛后缀
            'derive': self._freshness_derive,
            'prune': self._freshness_prune,
            'on_verified': self._freshness_audit,
            'on_pass_end': self._freshness_pass_end,
            'rebuild': self._freshness_rebuild,
            'content_version': 1,
            'unit': '项',
            'min_sync_interval': 10.0,     # 共享根可能很大：被动同步间隔放宽
            'max_dirs_per_sync': 200,
        }

    def _freshness_derive(self, items) -> Dict[str, Any]:
        """条目变化：受影响共享根的用量缓存作废（下次显示时重算）。"""
        for item in items:
            key = str(item.get('key') or '')
            for share_id, usage in list(self._usage_cache.items()):
                prefix = str(usage.get('path') or '')
                if prefix and key.startswith(prefix):
                    self._usage_cache.pop(share_id, None)
        return {'count': len(items)}

    def _freshness_prune(self, keys) -> int:
        if keys:
            self._usage_cache.clear()
        return len(keys)

    def _freshness_audit(self, keys) -> Dict[str, Any]:
        """全量校验后的对账：用量缓存整体重算（共享根集合可能已经变了）。"""
        self._usage_cache.clear()
        return {'valid_entries': len(keys)}

    def _freshness_pass_end(self, report, verified: bool) -> Dict[str, Any]:
        """整趟结束：校验这一趟就把用量算出来（旧入口没人调，等于从没算过）。"""
        if verified:
            roots = self._share_roots_report()
            return {'shares': len(roots),
                    'used_bytes': sum(int(r.get('used_bytes') or 0) for r in roots)}
        return {'shares': len(self._usage_cache)}

    def _freshness_rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃用量缓存（不动共享清单与配额设置）。"""
        self._usage_cache.clear()
        return {'kind': kind, 'cleared': True}

    @staticmethod
    def _count_entries(root: Path, max_entries: int = 200_000) -> int:
        """目录树里的条目数（文件 + 目录），用于"这个共享项有多大"的粗略提示。

        到上限就返回上限值：这时它是下界，但**不设**独立标志 —— 用量那边已经有
        `truncated`（同一次遍历的两个数字一起被截断，界面按同一个"≥"显示即可）。
        """
        count = 0
        for _base, dirs, files in os.walk(root):
            count += len(dirs) + len(files)
            if count > max_entries:
                return max_entries
        return count
