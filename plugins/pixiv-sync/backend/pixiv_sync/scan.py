"""刷新/扫描逻辑：拉取关注列表、逐画师全量列表、收藏列表，生成待下载清单。

清单已沉淀到 SQLite（见 db.py），本模块负责「先刷新再同步」的前半段：
- 画师级断点 scan.done_uids（关注）；
- 页级断点 scan.offset（收藏）。
"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Set, Tuple

from pixiv_mini import PixivError

from . import tasks as tasks_mod
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


def build_item(p, illust: Dict[str, Any], ids: Set[int]) -> Dict[str, Any]:
    """把列表响应中的 illust 精简为清单 item（只存下载/展示所需，含 urls/tags 名）。"""
    user = illust.get("user") or {}
    return {
        "id": int(illust.get("id", 0)),
        "type": illust.get("type"),
        "title": illust.get("title"),
        "page_count": illust.get("page_count"),
        "create_date": illust.get("create_date"),
        "user": {"id": user.get("id"), "name": user.get("name")},
        "urls": extract_all_urls(illust),
        "tags": [t.get("name") for t in (illust.get("tags") or []) if t.get("name")],
        "done": int(illust.get("id", -1)) in ids,
    }


def collect_following_pending(p, task: Dict[str, Any], ids: Set[int]) -> tuple:
    """扫描关注画师作品，返回 (items, scan)。画师级断点：scan.done_uids。

    每次刷新处理一批画师（受 max_refresh 待下载条数限制），未处理的画师下次继续；
    全部画师处理完 scan.complete=True。清单按旧→新排序（老图优先下载）。
    """
    items, scan = p._db().load_pending("following")
    done_uids = set(scan.get("done_uids", [])) if isinstance(scan, dict) else set()
    if scan and scan.get("complete"):
        done_uids = set()  # 上一轮完整结束，新一轮从头
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
                    q.pop("user_id", None)
                    q.pop("type", None)
                    page = client.user_illusts(uid, **q)
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

    def pending_count(it: List[Dict[str, Any]]) -> int:
        return sum(1 for i in it if not i.get("done"))

    todo = [uid for uid, _ in following if uid not in done_uids]
    start_pending = pending_count(items)
    window = p._scan_workers()  # 滑动窗口大小
    with ThreadPoolExecutor(max_workers=window) as pool:
        it = iter(todo)
        running: Dict[Any, int] = {}
        # 填满窗口
        for _ in range(window):
            try:
                uid = next(it)
            except StopIteration:
                break
            running[pool.submit(fetch_artist, uid)] = uid
        while running:
            if p._cancel_flag:
                break
            # 本批新增待下载达到上限 → 剩余画师下次继续（断点）
            if refresh_limit and (pending_count(items) - start_pending) >= refresh_limit:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                uid = running.pop(fut)
                try:
                    illusts = fut.result()
                except RateLimitError:
                    p._cancel_flag = True
                    raise
                except Exception as e:
                    print(f"[pixiv-sync] 拉取列表失败: {e}")
                    continue
                # 替换该画师的 items（更新），其他画师保留（断点累积）
                kept = [i for i in items if (i.get("user") or {}).get("id") != uid]
                for ill in reversed(illusts):  # 反转：旧作品在前
                    iid = ill.get("id")
                    if iid is not None:
                        kept.append(build_item(p, ill, ids))
                items = kept
                done_uids.add(uid)
                task["current"] = (
                    f"已扫描画师 {len(done_uids)}/{len(following)}，清单 {len(items)} 条"
                )
                tasks_mod.persist_task(p._tasks_file(), task)
                p._db().save_pending("following", items, {"done_uids": sorted(done_uids)})
            # 滑动：补充新画师进窗口
            for _ in done:
                try:
                    uid = next(it)
                except StopIteration:
                    continue
                running[pool.submit(fetch_artist, uid)] = uid

    complete = len(done_uids) >= len(following)
    return items, {"done_uids": sorted(done_uids), "complete": complete}


def collect_bookmarks_pending(p, task: Dict[str, Any], ids: Set[int]) -> tuple:
    """扫描收藏列表，返回 (items, scan)。页级断点：scan.offset 记录已翻页位置。

    每次刷新翻到 max_refresh 条待下载后暂停，下次从断点继续；翻完 scan.complete=True。
    """
    items, scan = p._db().load_pending("bookmarks")
    offset = scan.get("offset") if isinstance(scan, dict) else None
    if scan and scan.get("complete"):
        items = []  # 上一轮完整结束，新一轮从头
        offset = None
    refresh_limit = p._max_refresh()
    seen = {i.get("id") for i in items}
    start_pending = sum(1 for i in items if not i.get("done"))
    qs = {"offset": offset} if offset is not None else None

    client = p._client()
    while not p._cancel_flag:
        try:
            p._rate_limiter.wait()
            if qs:
                qs.pop("user_id", None)
                page = client.user_bookmarks_illust(client.user_id, **qs)
            else:
                page = client.user_bookmarks_illust(client.user_id)
            batch = page.get("illusts", []) or []
            for ill in reversed(batch):  # 反转：旧收藏在前
                iid = ill.get("id")
                if iid is None or iid in seen:
                    continue
                items.append(build_item(p, ill, ids))
                seen.add(iid)
                # 本批新增待下载达上限 → 保存进度，下次从断点继续
                if refresh_limit and (sum(1 for i in items if not i.get("done")) - start_pending) >= refresh_limit:
                    cur_offset = qs.get("offset") if qs else 0
                    p._db().save_pending("bookmarks", items, {"offset": cur_offset, "complete": False})
                    task["current"] = f"清单 {len(items)} 条（部分扫描，可再点刷新继续）"
                    tasks_mod.persist_task(p._tasks_file(), task)
                    return items, {"offset": cur_offset, "complete": False}
            nxt = page.get("next_url")
            if not nxt:
                break
            qs = client.parse_qs(nxt)
            p._db().save_pending("bookmarks", items, {"offset": qs.get("offset"), "complete": False})
        except PixivError as e:
            if "429" in str(e):
                raise RateLimitError("触发 Pixiv 限流（429），任务已停止，请等待冷却后重试") from None
            raise
    task["current"] = f"清单 {len(items)} 条（扫描完成）"
    tasks_mod.persist_task(p._tasks_file(), task)
    return items, {"complete": True}