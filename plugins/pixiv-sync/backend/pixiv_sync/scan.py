"""刷新/扫描逻辑：拉取关注列表、逐画师全量列表、收藏列表，生成待下载清单。

清单已沉淀到 SQLite（见 db.py），本模块负责「先刷新再同步」的前半段：
- 画师级断点 scan.done_uids（关注）；
- 收藏断点 scan.next_qs（完整翻页参数，Pixiv 实际使用 max_bookmark_id）。
"""

import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional, Set

from pixiv_mini import PixivError

from . import tasks as tasks_mod
from .download import _work_id
from .limiter import RateLimitError

log = logging.getLogger(__name__)

def _safe_int(value: Any) -> Optional[int]:
    """安全转 int：None 或非法值返回 None，合法值返回 int。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        log.info(f"[pixiv_sync-scan]画师id/作品id/tag 缺失/非法: {value}")
        return None
    
def fetch_following(p, task: Dict[str, Any] | None = None) -> List[tuple]:
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
        iid = _safe_int(illust.get("id"))
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


def _uid_of(item: Dict[str, Any]):
    try:
        return _safe_int((item.get("user") or {}).get("id"))
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



# 关注新作聚合流(illust_follow)单轮最多翻页数；超过仍未遇到「整页作品全部已入库」，
# 说明关注作品积压超出时间流窗口，转入逐画师兜底扫描（见 _fetch_follow_stream）。
MAX_FOLLOW_PAGES = 40


def fetch_artist(p, uid: int, known_ids=None) -> List[Dict[str, Any]]:
    """扫描一个画师的作品（模块级）。

    已知作品 id 集合传入后，从最新往回翻，遇到已知 id 就停（增量尾巴）；
    否则全量翻到最后一页。
    """
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
            batch = page.get("illusts", []) or []
            if not batch:
                break
            illusts.extend(batch)
            if known_ids is not None:
                hit = any(
                    _work_id(ill) in known_ids
                    for ill in batch
                    if _work_id(ill) is not None
                )
                if hit:
                    break  # 后面都是上一轮已入库的旧作品
            nxt = page.get("next_url")
            if not nxt:
                break
            q = client.parse_qs(nxt)
        except PixivError as e:
            if "429" in str(e):
                raise RateLimitError("触发 Pixiv 限流（429），任务已停止，请等待冷却后重试") from None
            raise
    return illusts


def _group_items(items: List[Dict[str, Any]]):
    """旧清单按画师分组（无画师的孤儿单独保留）。"""
    items_by_uid: Dict[int, List[Dict[str, Any]]] = {}
    orphans: List[Dict[str, Any]] = []
    for it in items:
        uid = _uid_of(it)
        if uid is None:
            orphans.append(it)
        else:
            items_by_uid.setdefault(uid, []).append(it)
    return items_by_uid, orphans


def _scan_artist_window(p, task, ids, uids, items_by_uid, done_uids,
                        truncate=False) -> tuple:
    """滑动窗口并行逐画师扫描，并把结果逐画师落库（断点保护）。

    - 画师在旧清单（items_by_uid）里有记录 → 增量尾巴（从最新翻到已入库作品停）；
      否则 → 全量翻底。
    - 每完成一个画师立即 replace_artist 写 works 与 scan.done_uids，
      429/中断后下次可续扫。
    - truncate=True 时本批次受 max_artists 限制（只用于「全量拉历史」的画师批次，
      该类每画师请求页数多、是 429 主因）；**增量尾巴批次传 truncate=False**，
      单次任务内一次扫完、不受 max_artists 限制（每画师平均 1 次请求即停）。
      被截断时会把尚未扫到的画师移出 done_uids，确保下轮仍会轮到它们。

    返回 (processed, truncated)。
    """
    uids = [u for u in uids]
    if not uids:
        return 0, False
    window = max(1, p._scan_workers())
    max_artists = p._max_artists() if truncate else 0
    processed = 0
    truncated = False
    with ThreadPoolExecutor(max_workers=min(window, len(uids))) as pool:
        it = iter(uids)
        running: Dict[Any, tuple] = {}

        def submit_next():
            if truncated:
                return
            try:
                uid = next(it)
            except StopIteration:
                return
            full_scan = uid not in items_by_uid
            known_ids = None
            if not full_scan:
                known_ids = {_work_id(i) for i in items_by_uid.get(uid, [])}
            running[pool.submit(fetch_artist, p, uid, known_ids)] = (uid, full_scan)

        for _ in range(min(window, len(uids))):
            submit_next()

        while running:
            if p._cancel_flag:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            if p._cancel_flag:
                break
            for fut in done:
                uid, full_scan = running.pop(fut)
                try:
                    illusts = fut.result()
                except RateLimitError:
                    p._cancel_flag = True
                    raise
                except Exception as e:
                    log.error(f"[pixiv-sync] 拉取画师 {uid} 列表失败: {e}")
                    continue

                if full_scan:
                    built = []
                    for ill in reversed(illusts):  # 反转：旧作品在前
                        item = build_item(p, ill, ids)
                        if item is not None:
                            built.append(item)
                    items_by_uid[uid] = built
                    saved_items = built
                else:
                    old_items = items_by_uid.get(uid, [])
                    old_ids = {_work_id(i) for i in old_items}
                    new_items = []
                    for ill in reversed(illusts):  # 反转：旧作品在前
                        item = build_item(p, ill, ids)
                        if item is None or _work_id(item) in old_ids:
                            continue
                        new_items.append(item)
                    saved_items = old_items + new_items
                    items_by_uid[uid] = saved_items
                if done_uids is not None:
                    done_uids.add(uid)
                processed += 1
                done_label = len(done_uids) if done_uids is not None else processed
                task["current"] = f"已扫描画师 {done_label} 位"
                task["total"] = len(uids)
                tasks_mod.persist_task(p._tasks_file(), task)
                p._db().replace_artist(
                    "following", uid, saved_items,
                    {"done_uids": sorted(done_uids) if done_uids is not None else []},
                )
                if max_artists and processed >= max_artists:
                    truncated = True

            if p._cancel_flag:
                break
            if not truncated:
                for _ in done:
                    submit_next()

    if truncated and done_uids is not None:
        # 截断：把尚未扫到的画师移出 done_uids，保证下轮补扫不会漏
        for uid in uids[processed:]:
            done_uids.discard(uid)
    return processed, truncated


def _fetch_follow_stream(p, task, ids, items_by_uid) -> bool:
    """illust_follow 聚合增量：把全部关注画师的新作从最新往回翻。

    停止条件：某一页（约 30 条）作品**全部已入库**。依据：对任意画师，
    「未入库新作」必然比该画师已入库的作品更新，因此一旦翻到连续整页
    已入库的时间带，其后（更旧）不可能再出现未入库新作——一次请求即可
    同时覆盖所有画师，不必逐画师请求。

    收集到的新作按画师并入 items_by_uid 并落库（途中断点保护，最终由
    main 的 save_pending 全量覆盖）。
    返回 need_polish：翻满 MAX_FOLLOW_PAGES 页仍未见到整页已入库
    （积压超出 follow 时间流窗口），调用方应转逐画师兜底补齐，保证不漏。
    """
    client = p._client()
    known_ids: Set[int] = set()
    for group in items_by_uid.values():
        for i in group:
            iid = _work_id(i)
            if iid is not None:
                known_ids.add(iid)
    collected: Dict[int, List[Dict[str, Any]]] = {}
    qs = None
    pages = 0
    collected_total = 0
    saw_full_known_page = False
    while not p._cancel_flag:
        try:
            p._rate_limiter.wait()
            if qs:
                params = dict(qs)
                params.pop("restrict", None)
                page = client.illust_follow(**params)
            else:
                page = client.illust_follow()
        except PixivError as e:
            if "429" in str(e):
                raise RateLimitError("触发 Pixiv 限流（429），任务已停止，请等待冷却后重试") from None
            raise
        batch = page.get("illusts", []) or []
        page_had_new = False
        for ill in batch:
            iid = _work_id(ill)
            if iid is None or iid in known_ids:
                continue
            item = build_item(p, ill, ids)
            if item is None:
                continue
            uid = _uid_of(item)
            if uid is None:
                continue  # follow 流作品必然带画师；孤儿忽略
            collected.setdefault(uid, []).append(item)
            known_ids.add(iid)
            page_had_new = True
            collected_total += 1
        pages += 1
        if batch and not page_had_new:
            saw_full_known_page = True
            break  # 整页已入库 → 更旧区域无未入库新作
        nxt = page.get("next_url")
        if not nxt or pages >= MAX_FOLLOW_PAGES:
            break
        qs = client.parse_qs(nxt)
    need_polish = collected_total > 0 and not saw_full_known_page
    # 合并进内存清单并途中落库
    if collected:
        for uid, new_items in collected.items():
            old = items_by_uid.get(uid, [])
            items_by_uid[uid] = old + new_items
            p._db().replace_artist(
                "following", uid, items_by_uid[uid],
                {"done_uids": [], "complete": True},
            )
        task["current"] = f"关注新作增量 {collected_total} 条"
        tasks_mod.persist_task(p._tasks_file(), task)
    return need_polish


def collect_following_pending(p, task: Dict[str, Any], ids: Set[int]) -> tuple:
    """扫描关注画师作品，返回 (items, scan)。画师级断点：scan.done_uids / complete。

    状态机（避免每轮对所有画师逐个请求，显著降低 429 风险）：
    - 铺底/断点（complete=False 或存在未完成画师）：只补扫 done_uids 之外
      的画师——DB 已有记录的画师 → 增量尾巴（每画师平均 1 次请求，不限额、
      单次任务一次扫完）；全新画师 → 全量翻底（每画师翻页多，受 max_artists
      分批防 429）。未完成的画师下轮继续（不会重复扫已完成画师）。
    - 增量（complete=True 且无待补画师）：
      1. 老画师新作改用 **illust_follow 聚合流**一次覆盖：从最新往回翻，
         遇到「整页作品全部已入库」即停（通常 1~3 次请求），不再逐画师请求；
      2. 若翻满 MAX_FOLLOW_PAGES 页仍未见整页已入库（长时间未同步的积压
         超出时间流窗口）→ 转逐画师尾巴兜底一次补齐（不限额），保证不漏。
    """
    items, scan = p._db().load_pending("following")
    complete = bool(scan and scan.get("complete"))
    done_uids: Set[int] = set(scan.get("done_uids", [])) if isinstance(scan, dict) else set()
    items_by_uid, orphans = _group_items(items)

    following = fetch_following(p, task)
    selected = p._load_selected_artists()
    if selected:
        following = [(u, n) for u, n in following
                     if str(u) in selected or n in selected]
    if not following:
        raise PixivError("关注列表为空或画师名单未匹配到任何画师")

    # ---------- 阶段 1：铺底 / 断点续扫 / 新关注画师全量优先 ----------
    todo = [uid for uid, _ in following if uid not in done_uids]
    if todo or not complete:
        # 分层：需要「全量拉历史」的新画师 vs 只需「增量尾巴」的旧画师。
        # max_artists 只约束全量批次（每画师翻页多、429 主因）；尾巴批次
        # 不限额，单次任务内一次扫完，不再按 30/轮反复点。
        todo_full = [uid for uid in todo if uid not in items_by_uid]
        todo_tail = [uid for uid in todo if uid in items_by_uid]
        if todo_full:
            _scan_artist_window(
                p, task, ids, todo_full, items_by_uid, done_uids, truncate=True,
            )
        if todo_tail and not p._cancel_flag:
            # 尾巴批次不受 max_artists 限制；全量截断与否都照扫（进度不受累）
            _scan_artist_window(p, task, ids, todo_tail, items_by_uid, done_uids)
        # 全部关注画师都处理完才算 complete；否则下轮继续补扫
        complete = all(uid in done_uids for uid, _ in following)
        flat = _flatten_items(items_by_uid, orphans)
        return flat, {"done_uids": sorted(done_uids), "complete": complete}

    # ---------- 阶段 2：聚合增量（所有关注画师均已有完整记录） ----------
    need_polish = _fetch_follow_stream(p, task, ids, items_by_uid)
    if need_polish:
        # 积压超出 follow 时间流窗口：逐画师兜底。这些画师在 DB 均有记录，
        # 属增量尾巴（每画师 1~N 次请求即停），单次任务一次扫完、不受
        # max_artists 限制；先把全部移出 done_uids 再逐个扫回——429/中断时
        # 已确认的画师留在 done，未确认的下轮从阶段 1 的尾巴批次续扫（断点）。
        all_uids = [uid for uid, _ in following]
        for uid in all_uids:
            done_uids.discard(uid)
        _scan_artist_window(p, task, ids, all_uids, items_by_uid, done_uids)
        complete = all(uid in done_uids for uid, _ in following)
    flat = _flatten_items(items_by_uid, orphans)
    return flat, {"done_uids": sorted(done_uids), "complete": complete}



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