"""视觉相似分组：dHash + 汉明距离并查集。

无状态：指纹库与设置端口由入口注入。阈值读的是本插件设置项 `threshold`（schema 的
min/max 已在写入时校验），0 是**合法阈值**（只认同哈希），不能与"未配置"混用。
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from shell.backend import tasks
from shell.backend.ports import SettingsPort


class SimilarFinder:
    """dHash 聚类：先算有效指纹，再按汉明距离做并查集。"""

    def __init__(self, settings: SettingsPort, hashes: Any,
                 check_cancel: Callable[[Any], None]) -> None:
        self._settings = settings
        self._hashes = hashes
        self._check_cancel = check_cancel

    def resolve_threshold(self, threshold: Optional[int] = None) -> int:
        """生效阈值：显式传入优先，否则读设置（默认 8）。"""
        # `setting()` 是动态类型；schema 里 threshold 的类型是 number、默认 8，写入时已按
        # min/max 校验，所以读回来一定是数字（可能被存成 float）。
        raw: int | float = threshold if threshold is not None else cast(
            int | float, self._settings.setting('threshold', 8))
        return max(0, min(16, int(raw)))

    def dhash_all(self, files: List[Dict[str, Any]],
                  task: Optional['tasks.BackgroundTask'] = None) -> Tuple[List[int], list]:
        """算出每张图的 dHash，返回 `(hashes, valid)`；读不了的图不进 `valid`。

        `task` 非空时上报第一段进度：`total` = 本次扫描的文件数、`processed` = 已算完的
        文件数。第二段（两两比较）由 `groups` 另开分母与阶段文案。
        """
        if task is not None:
            task.update(total=max(1, len(files)), processed=0, current='计算相似指纹')
        hashes: List[int] = []
        valid: List[Dict[str, Any]] = []
        for index, item in enumerate(files):
            self._check_cancel(task)
            digest = self._hashes.get(item['abs'], item['mtime'])
            if task is not None:
                task.update(processed=index + 1, current=Path(item['rel']).name)
            if digest is None:
                continue          # 读不了的不参与相似判定（0 是合法哈希，不能兼表失败）
            hashes.append(digest)
            valid.append(item)
        return hashes, valid

    def groups(self, hashes: List[int], valid: list,
               threshold: Optional[int],
               task: Optional['tasks.BackgroundTask'] = None) -> List[dict]:
        """dHash 并查集聚类（`task` 非空时上报进度并响应取消）。

        两两比较是 O(n²)，进度按**外层下标**推进。这是扫描的第二段，分母是有效图片数
        （与第一段"文件数"不同），前端按 `current` 里的阶段文字区分，不需要把两段
        凑成一个百分比。
        """
        threshold = self.resolve_threshold(threshold)
        n = len(valid)
        if task is not None:
            task.update(total=max(1, n), processed=0, current='比较相似度')
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(n):
            self._check_cancel(task)
            hi = hashes[i]
            for j in range(i + 1, n):
                # 尺寸差异过大不可能是相似图
                if max(valid[i]['size'], valid[j]['size']) > min(valid[i]['size'], valid[j]['size']) * 6:
                    continue
                if (hi ^ hashes[j]).bit_count() <= threshold:
                    union(i, j)
            if task is not None:
                task.update(processed=i + 1)

        clusters: Dict[int, List[str]] = {}
        for i in range(n):
            clusters.setdefault(find(i), []).append(valid[i]['rel'])
        groups = [{'files': sorted(paths)} for paths in clusters.values() if len(paths) >= 2]
        groups.sort(key=lambda g: -len(g['files']))
        return groups
