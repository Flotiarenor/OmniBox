"""刷新/扫描逻辑：拉取关注列表、逐画师全量列表、收藏列表，生成待下载清单。

清单已沉淀到 SQLite（见 db.py），本模块负责「先刷新再同步」的前半段：
- 画师级断点 scan.done_uids（关注）；
- 收藏断点 scan.next_qs（完整翻页参数，Pixiv 实际使用 max_bookmark_id）。
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Set

from pixiv_mini import PixivError

from . import tasks as tasks_mod
from .download import _work_id
from .limiter import RateLimitError


def fetch_following(p, task: Dict[str, Any] = None) -> List[tuple]:
    """翻页拉取全部关注画师列表 [(user_id, name)]。"""
    client = p._client()
    following: List[tuple] = []
    qs = None
    while not p._cancel_flag:
        try:
            p._rate_limiter.wait()  # 全局限速
            if qs:
                qs.pop("user_id", None)
                page = client.user_following(client.user_id, **qs)
            else:
                page = client.user_following(client.user_id)
            for item in page.get("user_previews", []) or []:
                user = item.get("user") or {}
                if user.get("id"):
                    following.append((int(user["id"]), str(user.get("name") or user["id"])))
            next_url = page.get("next_url")
            if not next_url:
                break
            qs = client.parse_qs(next_url)
        except PixivError as e:
            if "429" in str(e):
                raise RateLimitError("触发 Pixiv 限流（429），任务已停止，请等待冷却后重试") from None
            raise
    if task is not None:
        task["current"] = f"拉取 {len(following)} 位画师的作品列表…"
        tasks_mod.persist_task(p._tasks_file(), task)
    return following


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


def build_item(p, illust: Dict[str, Any], ids: Set[int]):
    """把列表响应中的 illust 精简为清单 item（只存下载/展示所需，含 urls/tags 名）。

    作品 id 缺失/非法时返回 None，调用方跳过，避免把 0/垃圾 id 写进清单。
    """
    try:
        iid = int(illust.get("id"))
    except (TypeError, ValueError):
        return None
    if iid <= 0:
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


def _uid_of(item: Dict[str, Any]):
    try:
        return int((item.get("user") or {}).get("id"))
    except (TypeError, ValueError):
        return None


def _flatten_items(
    items_by_uid: Dict[int, List[Dict[str, Any]]],
    orphans: List[Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    """按作品创建时间旧→新合并所有画师的 item（同时间按 id 稳定排序）。"""
    items = [it for group in items_by_uid.values() for it in group]
    if orphans:
        items.extend(orphans)
    items.sort(key=lambda it: ((it.get("create_date") or ""), int(it.get("id", 0))))
    return items


def collect_following_pending(p, task: Dict[str, Any], ids: Set[int]) -> tuple:
    """扫描关注画师作品，返回 (items, scan)。画师级断点：scan.done_uids。

    每次刷新处理一批画师（受 max_refresh 待下载条数限制），未处理的画师下次继续；
    全部画师处理完 scan.complete=True。清单按旧→新排序（老图优先下载）。
    """
    items, scan = p._db().load_pending("following")
    done_uids = set(scan.get("done_uids", [])) if isinstance(scan, dict) else set()
    if scan and scan.get("complete"):
        done_uids = set()  # 上一轮完整结束，新一轮从头

    # 旧清单按画师分组，替换某个画师时不需要每次 O(N) 扫描全表。
    items_by_uid: Dict[int, List[Dict[str, Any]]] = {}
    orphans: List[Dict[str, Any]] = []
    pending_by_uid: Dict[int, int] = {}
    for it in items:
        uid = _uid_of(it)
        if uid is None:
            orphans.append(it)
            continue
        items_by_uid.setdefault(uid, []).append(it)
        if not it.get("done"):
            pending_by_uid[uid] = pending_by_uid.get(uid, 0) + 1
    start_pending = sum(pending_by_uid.values())

    following = fetch_following(p, task)
    selected = p._load_selected_artists()
    if selected:
        following = [(u, n) for u, n in following
                     if str(u) in selected or n in selected]
    if not following:
        raise PixivError("关注列表为空或画师名单未匹配到任何画师")

    refresh_limit = p._max_refresh()

    def fetch_artist(uid: int) -> List[Dict[str, Any]]:
        """完整扫描一个画师的全部作品（从最新翻到最旧）。"""
        client = p._client()
        illusts: List[Dict[str, Any]] = []
        q = None
        while not p._cancel_flag:
            try:
                p._rate_limiter.wait()  # 全局限速（多线程共享）
                if q:
                    params = dict(q)
                    params.pop("user_id", None)
                    params.pop("type", None)
                    page = client.user_illusts(uid, **params)
                else:
                    page = client.user_illusts(uid)
                illusts.extend(page.get("illusts", []) or [])
                nxt = page.get("next_url")
                if not nxt:
                    break
                q = client.parse_qs(nxt)
            except PixivError as e:
                if "429" in str(e):
                    raise RateLimitError("触发 Pixiv 限流（429），任务已停止，请等待冷却后重试") from None
                raise
        return illusts

    todo = [uid for uid, _ in following if uid not in done_uids]
    window = p._scan_workers()  # 滑动窗口大小
    pending = start_pending
    stop_submitting = False

    with ThreadPoolExecutor(max_workers=window) as pool:
        it = iter(todo)
        running: Dict[Any, int] = {}

        def submit_next():
            try:
                uid = next(it)
            except StopIteration:
                return
            running[pool.submit(fetch_artist, uid)] = uid

        # 填满窗口
        for _ in range(window):
            submit_next()

        while running:
            if p._cancel_flag:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            if p._cancel_flag:
                break
            for fut in done:
                uid = running.pop(fut)
                try:
                    illusts = fut.result()
                except RateLimitError:
                    p._cancel_flag = True
                    raise
                except Exception as e:
                    print(f"[pixiv-sync] 拉取画师 {uid} 列表失败: {e}")
                    continue

                built = []
                for ill in reversed(illusts):  # 反转：旧作品在前
                    item = build_item(p, ill, ids)
                    if item is not None:
                        built.append(item)
                old_pending = pending_by_uid.get(uid, 0)
                new_pending = sum(1 for x in built if not x.get("done"))
                items_by_uid[uid] = built
                pending_by_uid[uid] = new_pending
                pending += new_pending - old_pending
                done_uids.add(uid)

                flat = _flatten_items(items_by_uid, orphans)
                task["current"] = (
                    f"已扫描画师 {len(done_uids)}/{len(following)}，清单 {len(flat)} 条"
                )
                task["total"] = len(flat)
                tasks_mod.persist_task(p._tasks_file(), task)
                p._db().replace_artist(
                    "following", uid, built, {"done_uids": sorted(done_uids)}
                )

                if refresh_limit and (pending - start_pending) >= refresh_limit:
                    # 不再提交新画师；但把已提交且在途的结果收完，避免浪费
                    # 已经发出的 API 请求，同时保证断点准确。
                    stop_submitting = True

            if p._cancel_flag:
                break
            if not stop_submitting:
                for _ in done:
                    submit_next()

    flat = _flatten_items(items_by_uid, orphans)
    complete = len(done_uids) >= len(following)
    return flat, {"done_uids": sorted(done_uids), "complete": complete}


def collect_bookmarks_pending(p, task: Dict[str, Any], ids: Set[int]) -> tuple:
    """扫描收藏列表，返回 (items, scan)。

    Pixiv 收藏接口的翻页参数是 max_bookmark_id，因此断点保存完整的 next_qs，
    而不是硬编码 offset；兼容旧版本保存的 offset 断点。
    """
    items, scan = p._db().load_pending("bookmarks")
    next_qs: Dict[str, Any] | None = None
    if isinstance(scan, dict):
        if scan.get("complete"):
            items = []  # 上一轮完整结束，新一轮从头
        elif "next_qs" in scan:
            next_qs = dict(scan.get("next_qs") or {})
        elif scan.get("offset") is not None:
            # v0.3 之前误把翻页断点存成 offset，兼容迁移。
            next_qs = {"offset": scan.get("offset")}

    refresh_limit = p._max_refresh()
    seen = {_work_id(i) for i in items}  # 复用 download._work_id 的安全取 id
    new_pending = 0
    qs = dict(next_qs) if next_qs else None

    client = p._client()
    while not p._cancel_flag:
        try:
            p._rate_limiter.wait()
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
                item = build_item(p, ill, ids)
                if item is None:
                    continue
                items.append(item)
                seen.add(iid)
                if not item.get("done"):
                    new_pending += 1
                # 本批新增待下载达上限 → 保存“当前页参数”作为断点。
                # 下次会重拉当前页，但 seen 会跳过已入库的条目。
                if refresh_limit and new_pending >= refresh_limit:
                    resume_qs = dict(qs or {})
                    p._db().save_pending(
                        "bookmarks", items,
                        {"next_qs": resume_qs, "complete": False},
                    )
                    task["current"] = f"清单 {len(items)} 条（部分扫描，可再点刷新继续）"
                    task["total"] = len(items)
                    tasks_mod.persist_task(p._tasks_file(), task)
                    return items, {"next_qs": resume_qs, "complete": False}
            nxt = page.get("next_url")
            if not nxt:
                # 最后一页先落盘，避免 main 写库前进程退出丢掉整页。
                p._db().save_pending("bookmarks", items, {"complete": True})
                break
            qs = client.parse_qs(nxt) or {}
            p._db().save_pending(
                "bookmarks", items, {"next_qs": dict(qs), "complete": False}
            )
        except PixivError as e:
            if "429" in str(e):
                raise RateLimitError("触发 Pixiv 限流（429），任务已停止，请等待冷却后重试") from None
            raise

    task["current"] = f"清单 {len(items)} 条（扫描完成）"
    task["total"] = len(items)
    tasks_mod.persist_task(p._tasks_file(), task)
    return items, {"complete": True}