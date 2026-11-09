"""清单条目构造：把 Pixiv 列表响应里的 illust 精简成待下载 item。

关注与收藏两个方向共用同一套字段与同一套安全取值规则，所以放在这里：
两边各写一份的结果是"某个方向把缺失 id 写成 0"这类偏差，而它只会表现为下载阶段
少了几件作品，不报错。
"""

import logging
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger(__name__)


def safe_int(value: Any) -> Optional[int]:
    """安全转 int：None 或非法值返回 None，合法值返回 int。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        log.info(f"[pixiv_sync-scan]画师id/作品id/tag 缺失/非法: {value}")
        return None


def extract_all_urls(illust: Dict[str, Any]) -> List[str]:
    """提取全部页原图 URL（original 优先，回退 large），从列表响应中直接取。"""
    meta_pages = illust.get("meta_pages") or []
    if meta_pages:
        urls = []
        for page in meta_pages:
            iu = page.get("image_urls") or {}
            u = iu.get("original") or iu.get("large")
            if u:
                urls.append(u)
        if urls:
            return urls
    orig = (illust.get("meta_single_page") or {}).get("original_image_url")
    if orig:
        return [orig]
    u = (illust.get("image_urls") or {}).get("large")
    return [u] if u else []


def build_item(illust: Dict[str, Any], ids: Set[int]):
    """把列表响应中的 illust 精简为清单 item（只存下载/展示所需，含 urls/tags 名）。

    作品 id 缺失/非法时返回 None，调用方跳过，避免把 0/垃圾 id 写进清单。
    """
    try:
        iid = safe_int(illust.get("id"))
    except (TypeError, ValueError):
        return None
    if iid is None or iid <= 0:
        return None
    user = illust.get("user") or {}
    return {
        "id": iid,
        "type": illust.get("type"),
        "title": illust.get("title"),
        "page_count": illust.get("page_count"),
        "create_date": illust.get("create_date"),
        "user": {"id": user.get("id"), "name": user.get("name")},
        "urls": extract_all_urls(illust),
        "tags": [t.get("name") for t in (illust.get("tags") or []) if t.get("name")],
        "done": iid in ids,
    }


def uid_of(item: Dict[str, Any]):
    try:
        return safe_int((item.get("user") or {}).get("id"))
    except (TypeError, ValueError):
        return None


def flatten_items(
    items_by_uid: Dict[int, List[Dict[str, Any]]],
    orphans: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    """按作品创建时间旧→新合并所有画师的 item（同时间按 id 稳定排序）。"""
    items = [it for group in items_by_uid.values() for it in group]
    if orphans:
        items.extend(orphans)
    items.sort(key=lambda it: ((it.get("create_date") or ""), int(it.get("id", 0))))
    return items


def group_items(items: List[Dict[str, Any]]):
    """旧清单按画师分组（无画师的孤儿单独保留）。"""
    items_by_uid: Dict[int, List[Dict[str, Any]]] = {}
    orphans: List[Dict[str, Any]] = []
    for it in items:
        uid = uid_of(it)
        if uid is None:
            orphans.append(it)
        else:
            items_by_uid.setdefault(uid, []).append(it)
    return items_by_uid, orphans
