"""接入壳的统一刷新基建（同步 / 校验）。

壳负责去抖、单飞、增量短路、幽灵清理、进度与状态展示，这里只回答三个问题——
管哪些文件、怎么算指纹、哪些条目需要重活。本插件的派生数据有两层：**逐图指纹**（dHash）
与**分组结果**（按模式缓存）。接入基建后：

- 被动同步只对"新增/指纹变化"的文件算 dHash（目录级短路，稳态零成本）；
- 手动「校验」= 全量比对 + 幽灵清理（删掉磁盘上已消失文件的指纹与孤儿条目），
  整趟结束时用刚维护好的指纹重新分组，直接把两个模式的结果写进缓存；
- `get_cached_scan` 回报 `stale`，界面据此提示而不是拿旧分组当真。

状态自持：无（指纹库、结果缓存、扫描与分组都是注入的组件）。`engine_provider` 是入口
给的闭包：壳把刷新引擎挂在插件实例上，组件只按需问"引擎在哪"。
"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

#: 收尾重新分组时向引擎问"取消了吗"的最小间隔（秒）。
#: 分组循环每张图问一次，直接查引擎状态会把一次 SQLite 查询塞进最内层循环；
#: 0.25 秒的判定粒度对"点取消后及时返回"足够。
CANCEL_POLL_SECONDS = 0.25


class FreshnessProvider:
    """向壳声明新鲜度规格，并实现它的钩子。"""

    def __init__(self, host: Any, hashes: Any, results: Any,
                 pipeline: Any, extensions: Any,
                 engine_provider: Callable[[], Optional[Any]]) -> None:
        self._host = host
        self._hashes = hashes
        self._results = results
        self._pipeline = pipeline
        self._extensions = extensions
        self._engine = engine_provider
        # 取消询问的缓存（时间戳, 结果）：见 `_cancel_requested`
        self._cancel_seen_at = 0.0
        self._cancel_seen = False

    def spec(self) -> Dict[str, Any]:
        return {
            'roots': lambda: [root for root, _prefix in self._host.roots()],
            # 前缀必须跟着宿主现算：命名空间 token 由 image-viewer 生成，
            # 写死会与扫描出的相对路径对不上（指纹库的键就全错位）
            'prefixes': lambda: [prefix for _root, prefix in self._host.roots()],
            'include': tuple(sorted(self._extensions)),
            'derive': self.derive,
            'prune': self.prune,
            'on_verified': self.audit,
            'on_pass_end': self.pass_end,
            'rebuild': self.rebuild,
            # 指纹算法版本直接取指纹库的条目版本：改算法只动 `const.DHASH_VERSION`，
            # 指纹条目与引擎的"整体作废"跟着同一个数字走
            'content_version': self._hashes.version,
            'unit': '张',
            'min_sync_interval': 5.0,
        }

    def reset_throttle(self) -> None:
        """设置变更后重置引擎的去抖窗口（入口在 on_settings_changed 里调用）。"""
        engine = self._engine()
        if engine is not None:
            engine.reset_throttle()

    # ===== 统一刷新的钩子 =====

    def derive(self, items) -> Dict:
        """条目新增/指纹变化：只给这些文件算 dHash（这是全流程最贵的一步）。"""
        errors: List[str] = []
        for item in items:
            abs_path = str(item.get('path') or '')
            if not abs_path:
                continue
            try:
                self._hashes.get(abs_path, float(item.get('mtime') or 0))
            except Exception as e:          # 单张失败不影响整趟
                errors.append(f'{item.get("key")}: {e}')
        self._results.mark_stale()
        return {'count': len(items), 'errors': errors[:20]}

    def prune(self, keys) -> int:
        """条目消失：删掉它的指纹，并作废分组结果。"""
        removed = 0
        for rel in keys:
            abs_path = self._host.abs_for_rel(str(rel))
            if not abs_path:
                continue
            if self._hashes.drop(self._hashes.key_for_abs(abs_path)):
                removed += 1
        if keys:
            self._results.mark_stale()
        return removed

    def audit(self, keys) -> Dict:
        """全量校验后的对账：指纹缓存里只保留磁盘上仍然存在的图片。

        `dhash.json` 只增不减时，在资源管理器里删掉的图片会永久留在指纹库里。
        """
        valid = set()
        for rel in keys:
            abs_path = self._host.abs_for_rel(str(rel))
            if abs_path:
                valid.add(self._hashes.key_for_abs(abs_path))
        dropped = self._hashes.prune(valid)
        self._results.mark_stale()
        return {'dropped_hashes': dropped, 'valid_entries': len(valid)}

    def pass_end(self, report, verified: bool) -> Dict:
        """整趟结束：落盘指纹；只有完整无错的校验才重新分组。

        取消、预算用尽与读取错误都会让报告的 `partial` / `errors` 非空。此时指纹集合
        不完整，按它重新分组会把没走到的图片当成"没有重复"；而且整库重新分组要读全树、
        补算全部 dHash，用户点的却是「取消」。这种情况只把结果缓存标成过期，等下一次
        完整校验再分组。引擎在取消/预算用尽后仍会调本钩子，所以判据必须在这里。
        """
        self._hashes.save()
        partial = bool(report.get('partial'))
        errors = bool(report.get('errors'))
        if verified and not partial and not errors:
            return self._pipeline.regroup_all(self._cancel_requested)
        self._results.persist_stale()
        return {'hashes': self._hashes.count(), 'verified': False, 'regrouped': False,
                'reason': 'partial' if partial else ('errors' if errors else 'unverified')}

    def _cancel_requested(self) -> bool:
        """收尾阶段「用户已经点了取消吗」。

        `on_pass_end` 拿不到任务对象（壳只传报告与是否校验），所以按引擎的状态投影
        问一次。分组的最内层循环每张图都会问，结果缓存 `CANCEL_POLL_SECONDS` 秒：
        直接查引擎状态意味着一次 SQLite 查询/张，会把收尾拖得比不检查还慢。
        """
        now = time.monotonic()
        if now - self._cancel_seen_at < CANCEL_POLL_SECONDS:
            return self._cancel_seen
        self._cancel_seen_at = now
        self._cancel_seen = False
        engine = self._engine()
        if engine is not None:
            try:
                task = (engine.state() or {}).get('task') or {}
                self._cancel_seen = bool(task.get('cancelled'))
            except Exception as e:      # 状态读不出来按"没取消"处理，不能把收尾打挂
                log.warning('[image-cleaner] 读取取消状态失败: %s', e)
        return self._cancel_seen

    def rebuild(self, kind: str = 'derived') -> Dict:
        """逃生门：丢弃逐图指纹与分组结果（不动相册本身）。"""
        self._hashes.clear()
        self._results.save({})
        self._results.clear_stale()
        return {'kind': kind, 'cleared': True}
