"""收藏清单的翻页扫描。

为什么与关注方向分文件：两条链路的断点语义完全不同 —— 关注是"画师级 done_uids +
聚合流 + 逐画师兜底"，收藏是"整串 next_qs 翻页 + 遇到上一轮尾巴即停"。放在同一个
文件里时，改一个方向要反复穿过另一方向的 300 行。
"""

import logging
from typing import Any, Dict, Set

from pixiv_mini import PixivError

from . import tasks as tasks_mod
from .download import _work_id
from .limiter import RateLimitError
from .scan_items import build_item

log = logging.getLogger(__name__)


def collect_bookmarks_pending(p, task: Dict[str, Any], ids: Set[int]) -> tuple:
    """扫描收藏列表，返回 (items, scan)。

    Pixiv 收藏接口的翻页参数是 max_bookmark_id，因此断点保存完整的 next_qs。
    上一轮完整扫描完成后，下一轮增量模式：从最新收藏往回翻，遇到上一轮
    已经见过的作品就停，只拉最新部分。
    """
    items, scan = p._db().load_pending("bookmarks")
    # 完整扫描后的新起一轮，或完整扫描后因单次上限拆出来的续跑，都属于
    # 增量模式（遇到上一轮完整尾巴就停）。首次全量扫描的续跑不能提前停。
    incremental = bool(scan and scan.get("complete"))
    next_qs: Dict[str, Any] | None = None
    if isinstance(scan, dict):
        if scan.get("complete"):
            # 保持旧清单，增量模式从第一页开始，遇到 known_ids 就停。
            next_qs = None
        elif "next_qs" in scan:
            next_qs = dict(scan.get("next_qs") or {})
            if scan.get("incremental"):
                incremental = True
        elif scan.get("offset") is not None:
            # v0.3 之前误把翻页断点存成 offset，兼容迁移。
            next_qs = {"offset": scan.get("offset")}

    seen = {_work_id(i) for i in items}  # 复用 download._work_id 的安全取 id
    known_ids = set(seen)  # 上一轮已入库的作品 id；增量模式下遇到它们就停
    qs = dict(next_qs) if next_qs else None

    client = p._client()
    while not p._cancel_flag:
        try:
            p._rate_limiter.wait(getattr(p, "_cancel_event", None))
            if qs:
                params = dict(qs)
                params.pop("user_id", None)
                page = client.user_bookmarks_illust(client.user_id, **params)
            else:
                page = client.user_bookmarks_illust(client.user_id)
            batch = page.get("illusts", []) or []
            for ill in reversed(batch):  # 反转：旧收藏在前
                iid = _work_id(ill)
                if iid is None or iid in seen:
                    continue
                item = build_item(ill, ids)
                if item is None:
                    continue
                items.append(item)
                seen.add(iid)

            if incremental and known_ids:
                hit = any(
                    _work_id(ill) in known_ids
                    for ill in batch
                    if _work_id(ill) is not None
                )
                if hit:
                    # 已到上一轮完整扫描的尾巴，后面的旧收藏不会再变化。
                    p._db().save_pending("bookmarks", items, {"complete": True})
                    break

            nxt = page.get("next_url")
            if not nxt:
                # 最后一页先落盘，避免 main 写库前进程退出丢掉整页。
                p._db().save_pending("bookmarks", items, {"complete": True})
                break
            qs = client.parse_qs(nxt) or {}
            p._db().save_pending(
                "bookmarks", items,
                {"next_qs": dict(qs), "complete": False, "incremental": incremental},
            )
        except PixivError as e:
            if "429" in str(e):
                raise RateLimitError("触发 Pixiv 限流（429），任务已停止，请等待冷却后重试") from None
            raise

    task["current"] = f"清单 {len(items)} 条（扫描完成）"
    task["total"] = len(items)
    tasks_mod.persist_task(p._tasks_file(), task)
    return items, {"complete": True}
