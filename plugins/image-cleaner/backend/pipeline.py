"""扫描编排：一次扫描 = 收集文件 → 分组 → 写结果缓存。

无状态：扫描、分组、结果缓存与指纹库各自持有自己的状态；这里只决定调用顺序。
同步入口（`duplicate_scan` / `similar_scan`）与后台任务共用这一份实现 ——
后台任务只额外负责进度与取消，分组逻辑不允许有第二份。
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


class _CancelProbe:
    """把 `cancelled()` 回调包成后台任务的形状：分组实现只读 `task.cancelled`、写 `task.update`。

    校验收尾（`regroup_all`）没有任务对象可传（壳的 `on_pass_end` 只给报告与是否校验），
    但"点取消后及时返回"必须复用分组里已有的取消点。这个探针让两条路径共用同一份
    `check_cancel` 实现：收尾重新分组不必再写一套取消判据，也不会漏掉最内层循环。
    """

    def __init__(self, cancelled: Callable[[], bool]) -> None:
        self._cancelled = cancelled

    @property
    def cancelled(self) -> bool:
        return bool(self._cancelled())

    def update(self, **kwargs: Any) -> None:
        """收尾路径没有进度任务：进度字段直接丢弃。"""
        del kwargs


class ScanPipeline:
    """把扫描、分组与结果落盘串成一次完整扫描。"""

    def __init__(self, name: str, scanner: Any, duplicates: Any, similar: Any,
                 results: Any, hashes: Any, modes: Tuple[str, ...]) -> None:
        self._name = name
        self._scanner = scanner
        self._duplicates = duplicates
        self._similar = similar
        self._results = results
        self._hashes = hashes
        self._modes = modes

    def collect(self) -> List[Dict[str, Any]]:
        """当前磁盘上的全部图片（带虚拟路径前缀）。"""
        return self._scanner.all_files()

    def group(self, mode: str, files: List[Dict[str, Any]],
              task: Optional[Any] = None,
              threshold: Optional[int] = None) -> Tuple[List[dict], int]:
        """按模式分组，返回 `(groups, scanned)`；`scanned` 是该模式的口径。

        完全重复的口径是文件数，相似的口径是**有效图片数**（读不了的图不参与判定）。
        """
        if mode == 'similar':
            hashes, valid = self._similar.dhash_all(files, task)
            return self._similar.groups(hashes, valid, threshold, task), len(valid)
        return self._duplicates.groups(files, task), len(files)

    def duplicate_scan(self) -> Dict:
        """全库完全重复分组（同步入口）。"""
        files = self.collect()
        if not files:
            return {'groups': [], 'scanned': 0}
        groups, scanned = self.group('dupe', files)
        self._results.save_result('dupe', groups, scanned)
        return {'groups': groups, 'scanned': scanned}

    def similar_scan(self, threshold: Optional[int] = None) -> Dict:
        """全库视觉相似分组（同步入口）。"""
        files = self.collect()
        if len(files) < 2:
            return {'groups': [], 'scanned': len(files)}
        hashes, valid = self._similar.dhash_all(files)
        groups = self._similar.groups(hashes, valid, threshold)
        self._hashes.save()
        self._results.save_result('similar', groups, len(valid))
        return {'groups': groups, 'scanned': len(valid)}

    def regroup_all(self, cancelled: Optional[Callable[[], bool]] = None) -> Dict:
        """用当前指纹缓存重新分组，并写入两个模式的结果缓存（校验趟的收尾）。

        `scanned` 与 `group()` 同一口径：完全重复数文件、相似数有效图片 —— 两处写进
        缓存的数字必须是同一含义，界面的「已扫描 N 张」才不会被读不了的图污染。

        `cancelled` 是收尾阶段的取消检查点：整库重新分组要读全树、补算全部 dHash，
        用户点「取消」之后不该再等它跑完。检查点同时进到两个分组实现的最内层循环
        （探针只读 `cancelled`，与后台任务的 `check_cancel` 共用一份实现）；取消时
        **不写任何模式的结果缓存** —— 半份分组会被界面当成完整结论。
        """
        probe = None if cancelled is None else _CancelProbe(cancelled)

        def stop() -> bool:
            return probe is not None and probe.cancelled

        if stop():
            return {'scanned': 0, 'cancelled': True}
        files = self.collect()
        out: Dict[str, Any] = {'scanned': len(files)}
        threshold = self._similar.resolve_threshold(None)
        for mode in self._modes:
            if stop():
                out['cancelled'] = True
                break
            try:
                if mode == 'dupe':
                    groups = self._duplicates.groups(files, probe)
                    scanned = len(files)
                else:
                    hashes, valid = self._similar.dhash_all(files, probe)
                    groups = self._similar.groups(hashes, valid, threshold, probe)
                    scanned = len(valid)
            except Exception as e:
                # 取消点抛的异常与真的分组失败分开：前者直接收手，不写结果也不记错误。
                if stop():
                    out['cancelled'] = True
                    break
                log.error(f'[{self._name}] 重新分组失败（{mode}）: {e}')
                out[mode] = {'error': str(e)}
                continue
            self._results.save_result(mode, groups, scanned)
            out[mode] = len(groups)
        return out
