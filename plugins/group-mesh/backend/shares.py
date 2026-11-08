"""共享项：挂载 / 移除、可用性与用量、接入壳的统一刷新基建。

状态自持：共享根用量缓存（`usage_cache`）。声明的落盘在 `Storage`，发布新注册记录
委托给 `Node`，刷新引擎由入口以闭包注入（`engine_for(plugin)` 需要插件实例本身）。

用量缓存的写入方是本组件的刷新钩子，读取方是共享项卡片（经 `share_roots_report`），
因此它只在这里。用量是"某个绝对目录在某个时刻的状态"，所以缓存条目带着路径与时刻：
共享根换过位置就整份作废，条目事件只作废事件落在其下的那些共享根。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shell.backend.ports import SettingsPort
from shell.groupmesh.records import RecordError
from shell.groupmesh.shares import Acl, new_share

log = logging.getLogger(__name__)


def _norm_path(path: str) -> str:
    """路径比较用的规范形式：绝对化 + 大小写折叠（Windows 上两者等价）。"""
    return os.path.normcase(os.path.abspath(path)) if path else ''


def _root_prefix(root_path: str) -> str:
    """根路径的"目录前缀"形式：保证以分隔符结尾，盘符根目录本身也适用。"""
    return root_path if root_path.endswith(os.sep) else root_path + os.sep


def _share_of_path(path: str, paths: Dict[str, str]) -> Optional[str]:
    """绝对路径落在哪个共享根下：嵌套共享时归最内层（最长匹配）。"""
    target = _norm_path(path)
    if not target:
        return None
    best: Optional[str] = None
    best_len = -1
    for share_id, root in paths.items():
        root_path = _norm_path(root)
        if not root_path:
            continue
        if target != root_path and not target.startswith(_root_prefix(root_path)):
            continue
        if len(root_path) > best_len:
            best, best_len = share_id, len(root_path)
    return best


def _missing_usage() -> Dict[str, Any]:
    """用量还没算过（或共享根不可用）时的占位值。"""
    return {'used_bytes': None, 'entries': None, 'truncated': False,
            'usage_at': None, 'usage_stale': True}


class Shares:
    def __init__(self, storage: Any, node: Any, settings: SettingsPort, lock: Any,
                 rules: Any, global_data_root: Path,
                 engine: Callable[[], Any]) -> None:
        self._storage = storage
        self._node = node
        self._settings = settings
        self._lock = lock
        self._rules = rules
        self._global_data_root = global_data_root
        self._engine = engine
        # 共享根用量缓存：{share_id: {path, used_bytes, truncated, entries, at}}。
        # 由统一刷新基建的校验驱动（见 spec()），共享项卡片读它，
        # `get_status` 不遍历目录。
        self.usage_cache: Dict[str, Dict[str, Any]] = {}

    # ── 可用性与状态 ──────────────────────────────────────────────────────

    def root_state(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """一个共享根的可用性。只用 `is_dir()`，不遍历目录。

        容量统计（`tree_usage`）刻意不在这里做：`D:\\图库` 那种目录走一遍可能要
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

    def _share_entries(self) -> List[Dict[str, Any]]:
        """共享根与声明合成一条：位置取自 `share_roots.json`，缺省回落到声明里的路径。"""
        roots = self._storage.load_share_roots()
        result: List[Dict[str, Any]] = []
        for share_id, share in self._storage.load_shares().items():
            entry = roots.get(share_id) or {'path': share.path,
                                            'max_bytes': share.max_bytes}
            result.append({'share_id': share_id,
                           'acl': share.declaration.acl.to_dict(),
                           **self.root_state(entry)})
        return result

    def get_share_roots(self) -> List[Dict[str, Any]]:
        """本机全部共享根及其状态（界面的"共享项"页用）。

        用量只读缓存、不遍历目录：`get_status()` 是首屏调用，而一个共享根走一遍
        可能要几十秒。没算过的共享根给出占位值（`usage_stale` 为真）。
        """
        return [{**item, **self._usage_fields(item['share_id'], item, scan=False)}
                for item in self._share_entries()]

    def share_roots_report(self) -> List[Dict[str, Any]]:
        """每个共享根的可用性 + 用量 + 条目数（缺的现算，算过的复用缓存）。"""
        return [{**item, **self._usage_fields(item['share_id'], item, scan=True)}
                for item in self._share_entries()]

    def _usage_fields(self, share_id: str, state: Dict[str, Any], *,
                      scan: bool) -> Dict[str, Any]:
        """用量字段：`scan` 为真时缺的现算，为假时只读缓存。"""
        if not state.get('available'):
            return _missing_usage()
        cached = self.usage_cache.get(share_id)
        if not self._usage_matches(cached, state):
            if not scan:
                return _missing_usage()
            cached = self.scan_share_usage(share_id, state)
        if not isinstance(cached, dict) or 'used_bytes' not in cached:
            reason = str((cached if isinstance(cached, dict) else {}).get('reason') or '')
            return {**_missing_usage(), 'reason': reason}
        return {'used_bytes': int(cached['used_bytes']),
                'entries': int(cached.get('entries') or 0),
                'truncated': bool(cached.get('truncated')),
                'usage_at': cached.get('at'),
                'usage_stale': False}

    @staticmethod
    def _usage_matches(cached: Any, state: Dict[str, Any]) -> bool:
        """缓存里的用量是否属于当前位置（共享根换过路径时整份作废）。"""
        if not isinstance(cached, dict):
            return False
        return _norm_path(str(cached.get('path') or '')) == _norm_path(str(state.get('path') or ''))

    def scan_share_usage(self, share_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """算一个共享根的用量与条目数，写进缓存（同一次刷新同一目录只走一遍）。"""
        target = Path(str(state['path'])).expanduser()
        try:
            used, truncated = self._storage.tree_usage(target, limit=state['max_bytes'])
        except OSError as e:
            return {'available': False, 'reason': f'扫描失败: {e}'}
        usage = {
            'path': state['path'],
            'used_bytes': used,
            'truncated': truncated,
            'entries': self._storage.count_entries(target),
            'at': int(time.time()),
        }
        self.usage_cache[share_id] = usage
        return usage

    # ── 挂载与移除 ────────────────────────────────────────────────────────

    def add_share(self, opts: Any = None, share_id: str = '', path: str = '',
                  read: str = 'group', write: str = 'owner') -> Dict[str, Any]:
        """挂载一个本机共享项（默认**不限制容量**）。

        两条硬约束在插件层就挡掉，不推给用户自觉：
          * 共享根不得落在程序数据目录 / 身份目录之内（避免把私钥或程序本身共享出去）；
          * 共享根必须是已存在的目录。

        容量上限（`max_bytes`）**刻意不做成界面设置项、默认也不设限**：
        节点侧的判定是"每条连接量一次基线再推算"，实测 8 条并发连接能在
        "上限 1000 字节"的共享项上写进 3200 字节且全部成功 —— 它给不出可信保证，
        却会让人以为有防线。需要时仍可显式传入（`0` 或不传 = 不限制）。
        """
        options = self._rules.opts(opts)
        share_id = str(options.get('share_id') or share_id or '')
        path = str(options.get('path') or path or '')
        read = str(options.get('read') or read or 'group')
        write = str(options.get('write') or write or 'owner')

        identity = self._storage.load_identity()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份'}

        target = Path(path).expanduser().resolve()
        if not target.is_dir():
            return {'success': False, 'error': f'共享根必须是已存在的目录: {target}'}

        forbidden = [self._global_data_root, self._storage.identity_dir]
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
            shares = self._storage.load_shares()
            shares[share_id] = share
            self._storage.save_shares(shares)
        # 同一个标识可能指向新位置：旧用量属于上一个目录。
        self._drop_usage(share_id)
        # 共享清单变了：立刻重发注册记录并 push 给对端；节点没跑时留给后台同步兜底。
        self._node.republish()
        # 根集合变了：下一次同步不该被最小间隔挡掉，新根由那一趟建指纹。
        self._reset_throttle()
        return {'success': True, 'share_id': share_id, 'path': str(target),
                'acl': acl.to_dict(), 'max_bytes': limit}

    def remove_share(self, opts: Any = None, share_id: str = '') -> Dict[str, Any]:
        """取消一个本机共享项（只删声明，不碰共享根里的文件）。"""
        options = self._rules.opts(opts)
        share_id = str(options.get('share_id') or share_id or '')
        with self._lock:
            shares = self._storage.load_shares()
            if share_id not in shares:
                return {'success': False, 'error': f'没有共享项 {share_id}'}
            del shares[share_id]
            self._storage.save_shares(shares)
        self._drop_usage(share_id)
        # 共享清单变了：立刻重发注册记录并 push 给对端。
        self._node.republish()
        # 根集合变了：下一次同步立即真扫。该根已不在 `roots` 里，它的指纹由那一趟的
        # "目录消失"分支按整趟结果清掉（部分遍历不做这件事，那时等下一趟或手动校验）。
        self._reset_throttle()
        return {'success': True, 'share_id': share_id}

    def refresh_share_roots(self) -> Dict[str, Any]:
        """重扫本机共享根：更新可用性，并给出条目数与用量（兼容入口）。

        实现已委托给壳的统一刷新基建（`system_freshness_verify` / 插件侧
        `engine.sync`）：本机共享根是"本地文件树 → 用量与条目数"的派生数据，
        判据（目录级短路、幽灵清理）统一由基建托管。新代码请用
        `system_freshness_sync` / `system_freshness_verify`。

        与 `get_share_roots` 的差别是这里**现算**用量（`share_roots_report`），
        后者只读缓存，因此首屏不会遍历共享根。
        """
        engine = self._engine()
        if engine is not None:
            report = engine.sync('', force=True)
            return {'success': True, 'roots': self.share_roots_report(),
                    'scanned_at': int(time.time()),
                    'action': report.get('action'), 'changed': report.get('added', 0)}
        return {'success': True, 'roots': self.share_roots_report(),
                'scanned_at': int(time.time())}

    # ── 统一刷新基建（同步 / 校验）────────────────────────────────────────
    #
    # 本机共享根是"本地文件树 → 用量与条目数"的派生数据：指纹与幽灵清理由壳的引擎
    # 托管，本组件只回答"哪些共享根的用量作废了"以及"这一趟算出了什么"。
    # 用量结果进 `usage_cache`，由 `get_share_roots` / `share_roots_report` 读出。

    def spec(self) -> Dict[str, Any]:
        return {
            'roots': lambda: [Path(str(s.path)) for s in self._storage.load_shares().values()
                              if str(s.path or '').strip()],
            'include': (),                 # 用量统计关心全部文件，不筛后缀
            'derive': self.derive,
            'prune': self.prune,
            'on_verified': self.audit,
            'on_pass_end': self.pass_end,
            'rebuild': self.rebuild,
            'content_version': 1,
            'unit': '项',
            'min_sync_interval': 10.0,     # 共享根可能很大：被动同步间隔放宽
            'max_dirs_per_sync': 200,
        }

    def sync_errors(self) -> List[str]:
        """最近一趟同步或校验报告里的读取错误（引擎侧已按上限截断）。"""
        engine = self._engine()
        if engine is None:
            return []
        try:
            state = engine.state()
        except Exception as e:  # 状态是附加信息：读不到不该让首屏失败
            log.info(f'[group-mesh] 读取刷新状态失败: {e}')
            return []
        report = state.get('last_report') or {}
        return [str(item) for item in (report.get('errors') or [])]

    def derive(self, items) -> Dict[str, Any]:
        """条目变化：受影响共享根的用量缓存作废（下次显示时重算）。

        判据是事件里条目的**绝对路径**落在哪个共享根下：条目键是虚拟路径，与共享根
        的物理位置没有可比性；嵌套共享时归最内层。
        """
        paths = self._share_paths()
        dropped: List[str] = []
        for item in items:
            share_id = _share_of_path(str(item.get('path') or ''), paths)
            if share_id is None or share_id not in self.usage_cache:
                continue
            self.usage_cache.pop(share_id, None)
            dropped.append(share_id)
        return {'count': len(items), 'dropped': len(dropped)}

    def prune(self, keys) -> int:
        if keys:
            self.usage_cache.clear()
        return len(keys)

    def audit(self, keys) -> Dict[str, Any]:
        """全量校验后的对账：用量缓存整体重算（共享根集合可能已经变了）。"""
        self.usage_cache.clear()
        return {'valid_entries': len(keys)}

    def pass_end(self, report, verified: bool) -> Dict[str, Any]:
        """整趟结束：校验这一趟就把用量算出来（同步不遍历目录，只报已缓存的数量）。"""
        if verified:
            roots = self.share_roots_report()
            return {'shares': len(roots),
                    'used_bytes': sum(int(r.get('used_bytes') or 0) for r in roots)}
        return {'shares': len(self.usage_cache)}

    def rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃用量缓存（不动共享清单与配额设置）。"""
        self.usage_cache.clear()
        return {'kind': kind, 'cleared': True}

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _share_paths(self) -> Dict[str, str]:
        """共享标识 → 本机绝对路径（`spec.roots` 的同一份来源）。"""
        return {share_id: str(share.path or '')
                for share_id, share in self._storage.load_shares().items()
                if str(share.path or '').strip()}

    def _drop_usage(self, share_id: str) -> None:
        self.usage_cache.pop(share_id, None)

    def _reset_throttle(self) -> None:
        """共享根集合变了：下一次同步不该被最小间隔挡掉。"""
        engine = self._engine()
        if engine is not None:
            engine.reset_throttle()
