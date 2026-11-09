"""共享项：挂载 / 移除、可用性与用量、接入壳的统一刷新基建。

状态自持：共享项清单与发布。用量缓存在注入的 `share_paths.UsageCache` 里（`usage_cache`
只读投影）；声明的落盘在 `Storage`，发布新注册记录委托给 `Node`，刷新引擎由入口以闭包
注入（`engine_for(plugin)` 需要插件实例本身）。

用量缓存由本组件的刷新钩子读写，共享项卡片经 `share_roots_report` 读。用量是"某个绝对
目录在某个时刻的状态"，所以缓存条目带着路径与时刻：共享根换过位置就整份作废，目录集合
变化按事件目录键归属到受影响的那几个根。

`track_entries=False` 之下引擎不维护条目事件：被动同步仍然只有"根目录指纹"这一条廉价
判据（深层子目录里的变化它看不见），权威结论由整趟校验给出 —— `pass_end(verified=True)`
无条件重算全部共享根的用量。目录集合变化（新建空目录 / 整个目录移走）是唯一的被动信号，
`dirs_changed` 只作废变化落在其下的根；无法归属（前缀表不可得、目录列表被截断）时保留旧
数字并标成待更新（`usage_stale`），不再把没走到的根清成占位。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shell.backend.ports import SettingsPort
from shell.groupmesh.records import RecordError
from shell.groupmesh.shares import Acl, new_share

log = logging.getLogger(__name__)


class Shares:
    def __init__(self, storage: Any, node: Any, settings: SettingsPort, lock: Any,
                 rules: Any, global_data_root: Path,
                 engine: Callable[[], Any], paths: Any) -> None:
        self._storage = storage
        self._node = node
        self._settings = settings
        self._lock = lock
        self._rules = rules
        self._global_data_root = global_data_root
        self._engine = engine
        # 路径规范 / 嵌套归属 / 根目录指纹 / 用量缓存（入口注入 share_paths.SharePaths）
        self._paths = paths
        # 共享根用量缓存：同步按根指纹、校验强制重算、目录变化按根作废。
        # 状态在注入的 `UsageCache` 里；共享项卡片经 `get_share_roots` 读，
        # `get_status` 不遍历目录。
        self._usage = paths.usage_cache(storage)

    @property
    def usage_cache(self) -> Dict[str, Dict[str, Any]]:
        """用量缓存条目（只读视图；读写都经由刷新钩子，测试用它确认缓存 populated）。"""
        return self._usage.cache

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
        return [{**item, **self._usage.fields(item['share_id'], item, scan=False)}
                for item in self._share_entries()]

    def share_roots_report(self, force: bool = False) -> List[Dict[str, Any]]:
        """每个共享根的可用性 + 用量 + 条目数（缺的现算，算过的复用缓存）。

        `force=True` 时无条件重算全部可用根：整趟校验给出的就是这一刻的权威数字，
        不能再被"根目录指纹没变"挡住（子目录里加了文件时根目录 mtime 不变）。
        """
        return [{**item, **self._usage.fields(item['share_id'], item, scan=True,
                                              force=force)}
                for item in self._share_entries()]

    def scan_share_usage(self, share_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
        """算一个共享根的用量与条目数，写进缓存（同一次刷新同一目录只走一遍）。"""
        return self._usage.scan(share_id, state)

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

        与 `get_share_roots` 的差别是这里**现算**用量（`share_roots_report(force=True)`），
        后者只读缓存，因此首屏不会遍历共享根。`changed` 是这一趟目录集合的变化数
        （`track_entries=False` 下没有条目事件，目录集合变化是唯一的"变没变"信号）。
        """
        engine = self._engine()
        if engine is not None:
            report = engine.sync('', force=True)
            return {'success': True, 'roots': self.share_roots_report(force=True),
                    'scanned_at': int(time.time()),
                    'action': report.get('action'),
                    'changed': self._paths.dirs_change_count(report)}
        return {'success': True, 'roots': self.share_roots_report(force=True),
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
            # 本插件只关心目录级事实（用量、条目数），不维护逐条目派生数据。
            # 声明后引擎不写 `entries` 表，指纹库不再随共享根的文件数线性膨胀；
            # 引擎支持这个字段之前它是装饰性的（规格归一化忽略未知键）。
            'track_entries': False,
            'include': (),                 # 用量统计关心全部文件，不筛后缀
            'derive': self.derive,
            'prune': self.prune,
            'on_dirs_changed': self.dirs_changed,
            'on_verified': self.audit,
            'on_pass_end': self.pass_end,
            'rebuild': self.rebuild,
            'content_version': 1,
            'unit': '项',
            'min_sync_interval': 10.0,     # 共享根可能很大：被动同步间隔放宽
            'max_dirs_per_sync': 200,
        }

    def dirs_changed(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """目录集合变了：只作废受影响共享根的用量，其余根的数字保留。

        `track_entries=False` 之后引擎不再维护逐条目事件，目录集合变化是唯一的被动
        信号。目录键是虚拟路径（首根空前缀、其余 `__<目录名>`），归属按引擎当前的根
        前缀表还原成共享标识：只有变化落在其下的根才丢缓存，部分遍历没走到的根不受
        影响。前缀表拿不到、或有目录被列表上限截断（看不到其余目录属于哪个根）时保守
        处理 —— 保留旧数字，只标成待更新（`usage_stale`），不再把没走到的根清成占位。
        """
        new_dirs = list(report.get('new_dirs') or [])
        vanished = list(report.get('vanished_dirs') or [])
        if not new_dirs and not vanished:
            return {'cleared': 0, 'stale': 0, 'dirs_changed': 0}
        owners = self._dir_owners()
        affected, unowned = self._paths.affected_shares(new_dirs, vanished, owners)
        unowned = unowned or bool(report.get('new_dirs_truncated')
                                  or report.get('vanished_dirs_truncated'))
        cleared = self._usage.invalidate(affected)
        stale = 0
        if unowned:
            known = self._share_paths()
            stale = self._usage.mark_stale(known.keys(), known)
        return {'cleared': cleared, 'stale': stale,
                'dirs_changed': len(new_dirs) + len(vanished)}

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

        **本模式（`track_entries=False`）下引擎不会调用这个钩子**：它保持公共方法，
        供将来改回逐条目指纹时复用；用量失效现在走 `dirs_changed` + 整趟校验强制重算。
        """
        paths = self._share_paths()
        dropped: List[str] = []
        for item in items:
            share_id = self._paths.share_of(str(item.get('path') or ''), paths)
            if share_id is None or share_id not in self._usage.cache:
                continue
            self._drop_usage(share_id)
            dropped.append(share_id)
        return {'count': len(items), 'dropped': len(dropped)}

    def prune(self, keys) -> int:
        """条目消失：清空用量缓存。`track_entries=False` 下引擎不会调用（保留给逐条目模式）。"""
        if keys:
            self._usage.clear()
        return len(keys)

    def audit(self, keys) -> Dict[str, Any]:
        """全量校验后的对账：用量缓存整体重算。

        `track_entries=False` 下引擎不会调用（它没有条目键集合可给）；用量重算由
        `pass_end(verified=True)` 负责。
        """
        self._usage.clear()
        return {'valid_entries': len(keys)}

    def pass_end(self, report, verified: bool) -> Dict[str, Any]:
        """整趟结束：校验这一趟无条件重算全部共享根用量（同步只报已缓存的数量）。

        为什么校验必须忽略根目录指纹：用量统计的是整棵树的字节数，子目录里加/删文件
        不会改根目录 mtime，只有整趟校验是"现在就扫一遍"的权威结论。同步保持廉价：
        只按根指纹判断，不遍历。
        `report['partial']`（用户取消；校验没有预算上限）表示这一趟没有走完，不能当成
        权威结论，也不该在用户取消后接着把整棵树扫一遍 —— 保留旧数字并标成待更新。
        """
        if verified:
            if report.get('partial'):
                known = self._share_paths()
                stale = self._usage.mark_stale(known.keys(), known)
                return {'shares': len(self._usage.cache), 'stale': stale,
                        'verified': False}
            roots = self.share_roots_report(force=True)
            return {'shares': len(roots), 'verified': True,
                    'used_bytes': sum(int(r.get('used_bytes') or 0) for r in roots)}
        return {'shares': len(self._usage.cache)}

    def rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃用量缓存（不动共享清单与配额设置）。"""
        self._usage.clear()
        return {'kind': kind, 'cleared': True}

    # ── 内部 ──────────────────────────────────────────────────────────────

    def _share_paths(self) -> Dict[str, str]:
        """共享标识 → 本机绝对路径（`spec.roots` 的同一份来源）。"""
        return {share_id: str(share.path or '')
                for share_id, share in self._storage.load_shares().items()
                if str(share.path or '').strip()}

    def _dir_owners(self) -> Optional[Dict[str, str]]:
        """引擎当前的"根前缀 → 共享标识"表；拿不到一致映射时返回 None。

        目录键（`new_dirs` / `vanished_dirs`）是虚拟路径，归属只能按引擎算出的根前缀
        还原。根顺序与 `spec.roots` 一致，逐根用规范化后的物理路径对回共享标识；
        任何一根对不上（共享清单刚变、规格求值异常）就整表作废，由调用方保守处理。
        """
        engine = self._engine()
        if engine is None:
            return None
        try:
            source = engine.spec.source
        except Exception as e:  # 规格/根声明异常：状态与清理都不该把首屏打挂
            log.info(f'[group-mesh] 读取共享根前缀失败，目录变化按保守方式处理: {e}')
            return None
        return self._paths.root_owners([str(root) for root in source.roots],
                                       list(source.prefixes), self._share_paths())

    def _drop_usage(self, share_id: str) -> None:
        self._usage.drop(share_id)

    def _reset_throttle(self) -> None:
        """共享根集合变了：下一次同步不该被最小间隔挡掉。"""
        engine = self._engine()
        if engine is not None:
            engine.reset_throttle()
