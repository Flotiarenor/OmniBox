"""下载逻辑：按清单并行下载待下载作品（画师/喜欢共用）。

- 已下载（ids）或永久跳过（failed_ids，404/已删除）的不再处理；
- 任何一页非 404 失败都不会把作品写入去重集合（下次同步重试）；
  只要失败页全部是 404，就把作品写入 failed_ids 永久跳过（避免重复重试已删除页）；
- 文件已存在（download 返回 False）视为已下载，幂等跳过。
- ugoira 动图（type == "ugoira"）走 process_ugoira：另取帧时序元数据、
  下载 zip 帧序列并转成动画 WebP 保存（见 ugoira.py）；不再把 zip 当 jpg 下载。
"""

import logging
import os
import shutil
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from pixiv_mini import PixivError

from . import artist as artist_mod
from . import tasks as tasks_mod
from . import ugoira as ugoira_mod
from .limiter import RateLimitError

log = logging.getLogger(__name__)

def _safe_int(value: Any) -> Optional[int]:
    """安全转 int：None 或非法值返回 None，合法值返回 int。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        log.info(f"[pixiv_sync-download]作品id 缺失/非法: {value}")
        return None

def all_image_urls(p, illust: Dict[str, Any]) -> List[str]:
    """多图作品返回全部页 URL，单图返回封面 URL。

    （固定行为）优选下载画师原图（original）；取不到时回退 1200px 大图（large）。
    清单 item 带了预提取 urls 时直接使用。
    """
    direct = illust.get("urls")
    if direct:
        return [u for u in direct if u]
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


def _work_id(illust: Dict[str, Any]):
    """从 item 中安全取作品 id；缺失/非法时返回 None。"""
    try:
        return _safe_int(illust.get("id"))
    except (TypeError, ValueError):
        return None


def _safe_ext(url: str) -> str:
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"} else ".jpg"


def process_ugoira(p, illust: Dict[str, Any], task: Dict[str, Any], ids: Set[int], failed: Set[int], sub) -> None:
    """下载一个 ugoira 动图作品并转成动画 WebP（全部成功才入去重集合）。

    与 process_illust 的语义保持一致：
    - 已有目标 WebP（非 0 字节）视为已下载（幂等，不发起任何网络请求）；
    - 帧时序接口/zip/转换失败（非 404）→ 只计数失败、不入 ids，下次同步重试；
    - 404（作品/zip 已删除）→ 写入 failed_ids 永久跳过；
    - 触发 Pixiv 429 → 抛 RateLimitError 停止整个任务（尊重全局限流）。
    """
    iid = _work_id(illust)
    if iid is None:
        return
    title = illust.get("title", "") or ""
    with p._task_lock:
        task["current"] = f"{title} ({iid}) [动图]"

    if iid in ids or iid in failed:
        with p._task_lock:
            task["skipped"] += 1
            task["done"] += 1
            tasks_mod.persist_task(p._tasks_file(), task)
        return

    def _done(failed_count: int, add_ids: bool, add_failed: bool) -> None:
        with p._task_lock:
            task["failed"] += failed_count
            task["done"] += 1
            if add_ids:
                ids.add(iid)
                task["downloaded"] += 1
            if add_failed:
                failed.add(iid)
            tasks_mod.persist_task(p._tasks_file(), task)

    target = sub / f"{iid}.webp"
    tmpdir: Optional[Path] = None
    try:
        # 已有完整目标文件 → 幂等跳过（等价于 download 返回 False 的语义）
        if target.exists() and target.stat().st_size > 0:
            _done(0, add_ids=True, add_failed=False)
            return

        client = p._client()
        # 1) 帧时序（app-api，走全局限速；失败=非 404 临时错误，下次重试）
        p._rate_limiter.wait()
        try:
            meta = client.ugoira_metadata(iid)
        except PixivError as e:
            if "429" in str(e):
                raise RateLimitError(
                    "触发 Pixiv 限流（429），任务已停止，请等待冷却后重试"
                ) from None
            raise
        zip_url, frames = ugoira_mod.parse_metadata(meta)
        if not zip_url or not frames:
            raise PixivError(f"ugoira {iid} 元数据为空（帧时序缺失）")

        # 2) zip 下载到系统临时目录（CDN 走流式，不受 app-api 限速；
        #    zip 非图片，verify_image=False，由 ugoira.convert 做 zip 完整性兜底）
        tmpdir = Path(tempfile.mkdtemp(prefix=f"pixiv-sync-u{int(time.time())}-"))
        zip_path = tmpdir / f"{iid}.zip"
        client.download(zip_url, path=str(tmpdir), name=f"{iid}.zip", replace=True,
                        verify_image=False)

        # 3) 帧序列 → 动画 WebP（转换失败同样下次重试）
        ugoira_mod.convert(zip_path, frames, target)
        _done(0, add_ids=True, add_failed=False)
    except RateLimitError:
        raise
    except PixivError as e:
        msg = str(e)
        if "HTTP 404" in msg:
            # 作品/zip 已删除：永久跳过，避免每次同步重试同一已删除动图
            log.warning(f"[pixiv-sync] ugoira {iid} 已删除/404，永久跳过")
            _done(1, add_ids=False, add_failed=True)
        else:
            log.error(f"[pixiv-sync] ugoira {iid} 失败（下次同步重试）: {msg}")
            _done(1, add_ids=False, add_failed=False)
    except Exception as e:
        log.error(f"[pixiv-sync] ugoira {iid} 异常: {type(e).__name__}: {e}")
        _done(1, add_ids=False, add_failed=False)
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def process_illust(p, illust: Dict[str, Any], task: Dict[str, Any], ids: Set[int], failed: Set[int], sub) -> None:
    """下载一个作品（全部页），并更新去重集合 / 失败跳过集合 / 任务计数。

    只有全部页面都成功（新下载或文件已存在）时才写入 ids；存在非 404 失败时
    下次同步重试；失败页全部是 404 时写入 failed_ids 永久跳过。
    """
    iid = _work_id(illust)
    if iid is None:
        return
    if str(illust.get("type") or "").lower() == "ugoira":
        # ugoira 是 zip 帧序列，不是普通图片：转动画 WebP，独立流程。
        process_ugoira(p, illust, task, ids, failed, sub)
        return
    with p._task_lock:
        task["current"] = f"{illust.get('title', '')} ({iid})"

    if iid in ids or iid in failed:
        with p._task_lock:
            task["skipped"] += 1
            task["done"] += 1
            tasks_mod.persist_task(p._tasks_file(), task)
        return

    try:
        urls = all_image_urls(p, illust)
        if not urls:
            with p._task_lock:
                task["failed"] += 1
                task["done"] += 1
                tasks_mod.persist_task(p._tasks_file(), task)
            return

        # 固定行为：多图作品始终放入 {作品id}/ 子文件夹（单图平铺）；如需全局改关，改这里为 False。
        subfolder = len(urls) > 1
        target = sub / str(iid) if subfolder else sub
        ok = 0
        fail = 0
        missing_404 = 0
        for idx, url in enumerate(urls):
            ext = _safe_ext(url)
            name = f"{iid}{ext}" if len(urls) == 1 else f"{iid}_p{idx}{ext}"
            try:
                # True=新下载；False=文件已存在（视为已下载）；PixivError=失败
                p._client().download(url, path=str(target), name=name)
                ok += 1
            except PixivError as e:
                fail += 1
                if "HTTP 404" in str(e):
                    missing_404 += 1

        with p._task_lock:
            task["failed"] += fail
            task["done"] += 1
            if fail == 0:
                # 全部页可用，作品级去重才成立。
                ids.add(iid)
                task["downloaded"] += 1
            elif missing_404 and missing_404 == fail:
                # 所有失败页都是 404：缺失页不会再恢复。把作品永久跳过，
                # 避免每次同步都重试同一个已删除页；已成功的页文件保留。
                # 只写 failed、不写 ids，这样「重试失败作品」清空后仍可重试。
                failed.add(iid)
            tasks_mod.persist_task(p._tasks_file(), task)
    except Exception as e:
        # 单作品出现未预期异常时也要推进任务计数，避免进度永久卡住。
        log.error(f"[pixiv-sync] 作品 {iid} 下载异常: {e}")
        with p._task_lock:
            task["failed"] += 1
            task["done"] += 1
            tasks_mod.persist_task(p._tasks_file(), task)


def download_pending(p, task: Dict[str, Any], ids: Set[int], failed: Set[int], kind: str) -> None:
    """从清单下载待下载部分（画师/喜欢共用逻辑）。

    注意：works.done 只是扫描时保存的快照，真正的“是否已下载”以
    downloaded_ids.json（ids）和 failed_ids.json（failed）为准；否则用户
    手动删图后即使点过「刷新记录/校验内容」，done=1 仍会阻止重下。
    """
    items, _ = p._db().load_pending(kind)
    if not items:
        raise PixivError(
            f"没有待下载的作品（清单为空：{'关注' if kind == 'following' else '收藏'}没有可同步的内容）"
        )
    # 待下载 = 不在已下载集合、不在失败跳过集合；done 快照不参与过滤。
    todo = []
    for i in items:
        iid = _work_id(i)
        if iid is None:
            continue
        if iid not in ids and iid not in failed:
            todo.append(i)

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

    if p._cancel_flag:
        task["current"] = "已取消"
        tasks_mod.persist_task(p._tasks_file(), task)
        return

    # 按画师解析目标目录（主线程，含改名迁移/缓存）。
    # 整个任务只读写一次 artists.json，而不是每个作品都 open/load/dump 一遍。
    artist_cache = artist_mod.load_cache(p._artists_file())
    pending = []
    try:
        for ill in todo:
            user = ill.get("user") or {}
            uid = user.get("id")
            if uid:
                try:
                    uid = int(uid)
                except (TypeError, ValueError):
                    uid = None
            if uid:
                sub = artist_mod.artist_dir(
                    p._root(), p._artists_file(), uid, str(user.get("name") or uid),
                    cache=artist_cache, persist=False,
                )
            else:
                sub = p._root() / "pixiv" / "未分类"
            pending.append((sub, ill))
    finally:
        artist_mod.save_cache(p._artists_file(), artist_cache)

    workers = p._workers()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        it = iter(pending)
        running = {}

        def submit_next():
            try:
                sub, illust = next(it)
            except StopIteration:
                return
            running[pool.submit(process_illust, p, illust, task, ids, failed, sub)] = illust

        for _ in range(workers):
            if p._cancel_flag:
                break
            submit_next()

        while running:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                running.pop(future)
                # process_illust 内部已兜底计数；这里再兜底一次以防御提交阶段异常。
                try:
                    future.result()
                except Exception as e:
                    log.error(f"[pixiv-sync] 下载任务异常: {e}")
                    with p._task_lock:
                        task["failed"] += 1
                        task["done"] += 1
                        tasks_mod.persist_task(p._tasks_file(), task)
            if p._cancel_flag:
                break
            for _ in done:
                if p._cancel_flag:
                    break
                submit_next()

    if p._cancel_flag:
        task["current"] = "已请求取消，等待在途下载结束"
        tasks_mod.persist_task(p._tasks_file(), task)

    # 标记清单 done（保留在清单，供统计）
    just_done = {_work_id(i) for i in todo if _work_id(i) in ids}
    if just_done:
        p._db().mark_done(kind, just_done)