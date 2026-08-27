"""下载逻辑：按清单并行下载待下载作品（画师/喜欢共用）。

- 已下载（ids）或永久跳过（failed_ids，404/已删除）的不再处理；
- 单页失败计入 failed 且不入去重集合（下次重试）；404 的作品进 failed 永久跳过；
- 文件已存在（download 返回 False）视为已下载，幂等跳过。
"""

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Set
from urllib.parse import urlparse

from pixiv_mini import PixivError

from . import artist as artist_mod
from . import tasks as tasks_mod


def all_image_urls(p, illust: Dict[str, Any]) -> List[str]:
    """多图作品返回全部页 URL，单图返回封面 URL。

    （固定行为）优选下载画师原图（original）；取不到时回退 1200px 大图（large）。
    清单 item 带了预提取 urls 时直接使用。
    """
    direct = illust.get("urls")
    if direct:
        return direct
    # 固定行为：默认下载原图（original）；如需全局改关，改这里为 False。
    want_original = True

    def pick(page: Dict[str, Any]):
        urls = page.get("image_urls") or {}
        if want_original:
            orig = urls.get("original")
            if orig:
                return orig
        return urls.get("large")

    meta_pages = illust.get("meta_pages") or []
    if meta_pages:
        urls = [u for u in (pick(x) for x in meta_pages) if u]
        if urls:
            return urls
    if want_original:
        orig = (illust.get("meta_single_page") or {}).get("original_image_url")
        if orig:
            return [orig]
    url = (illust.get("image_urls") or {}).get("large")
    return [url] if url else []


def process_illust(p, illust: Dict[str, Any], task: Dict[str, Any], ids: Set[int], failed: Set[int], sub) -> None:
    """下载一个作品（全部页），并更新去重集合 / 失败跳过集合 / 任务计数。"""
    iid = illust.get("id")
    if iid is None:
        return
    task["current"] = f"{illust.get('title', '')} ({iid})"
    if int(iid) in ids or int(iid) in failed:
        with p._task_lock:
            task["skipped"] += 1
            task["done"] += 1
            tasks_mod.persist_task(p._tasks_file(), task)
        return

    urls = all_image_urls(p, illust)
    # 固定行为：多图作品始终放入 {作品id}/ 子文件夹（单图平铺）；如需全局改关，改这里为 False。
    subfolder = len(urls) > 1
    target = sub / str(iid) if subfolder else sub
    ok = 0
    fail = 0
    for idx, url in enumerate(urls):
        ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
        name = f"{iid}{ext}" if len(urls) == 1 else f"{iid}_p{idx}{ext}"
        try:
            # True=新下载；False=文件已存在（视为已下载）；PixivError=失败
            p._client().download(url, path=str(target), name=name)
            ok += 1
        except PixivError as e:
            if "HTTP 404" in str(e):
                failed.add(int(iid))  # 作品已从 pixiv 删除：永久跳过，不再每次重试
            fail += 1
    with p._task_lock:
        task["failed"] += fail
        if ok > 0 or not urls:
            ids.add(int(iid))
            task["downloaded"] += 1
        task["done"] += 1
        tasks_mod.persist_task(p._tasks_file(), task)


def download_pending(p, task: Dict[str, Any], ids: Set[int], failed: Set[int], kind: str) -> None:
    """从清单下载待下载部分（画师/喜欢共用逻辑）。"""
    items, _ = p._db().load_pending(kind)
    if not items:
        raise PixivError(
            f"待下载清单为空，请先点「刷新{'关注名单' if kind == 'following' else '喜欢名单'}」"
        )
    # 待下载 = 未标记 done、不在已下载集合、不在失败跳过集合
    todo = [
        i for i in items
        if not i.get("done") and int(i.get("id", -1)) not in ids and int(i.get("id", -1)) not in failed
    ]
    limit = p._max_download()
    if limit:
        todo = todo[:limit]
    if not todo:
        task["total"] = 0
        task["current"] = "清单中已无待下载作品"
        tasks_mod.persist_task(p._tasks_file(), task)
        return
    task["total"] = len(todo)
    task["current"] = f"开始下载 {len(todo)} 个作品…"
    tasks_mod.persist_task(p._tasks_file(), task)

    # 按画师解析目标目录（主线程，含改名迁移/缓存）
    pending = []
    for ill in todo:
        user = ill.get("user") or {}
        uid = user.get("id")
        if uid:
            sub = artist_mod.artist_dir(
                p._root(), p._artists_file(), int(uid), str(user.get("name") or uid)
            )
        else:
            sub = p._root() / "pixiv" / "未分类"
        pending.append((sub, ill))

    workers = p._workers()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(process_illust, p, illust, task, ids, failed, sub)
            for sub, illust in pending
        ]
        for future in futures:
            try:
                future.result()
            except Exception as e:
                print(f"[pixiv-sync] 下载任务异常: {e}")

    # 标记清单 done（保留在清单，供统计）
    just_done = {int(i["id"]) for i in todo if int(i.get("id", -1)) in ids}
    if just_done:
        p._db().mark_done(kind, just_done)