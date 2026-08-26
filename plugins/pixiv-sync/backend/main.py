"""Pixiv 同步插件：image-viewer 的 Companion 插件。

设计参考 docs/image-cleaner-design.md（Companion 模式）与
docs/image-tagger-design.md §6（长任务状态机 / 断点约定）。
功能：
  - 同步画师：拉取关注用户的新作流 (v2/illust/follow)，多图全页下载
  - 同步喜欢：拉取当前用户公开收藏 (v1/user/bookmarks/illust)
  - 去重：记录已下载 illust_id 到 <root>/.cache/pixiv-sync/downloaded_ids.json
  - 断点：任务状态每张一写 <root>/.cache/pixiv-sync/tasks.json，重启后恢复 paused
下载目录默认写入 image-viewer 数据根目录，画作自动出现在相册中。
"""

import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from shell.backend.plugin_base import PluginBase

from pixiv_mini import PixivClient, PixivError

CACHE_SUBDIR = Path(".cache") / "pixiv-sync"


class _RateLimiter:
    """全局请求速率控制（令牌桶），避免触发 pixiv app-api 的 429 限流。

    pixiv 实测限流阈值约 30 req/10s，这里保守限制为 rate 次/秒。
    """

    def __init__(self, rate: float = 3.0):
        self._rate = rate
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.time()
            wait = max(0.0, self._last + 1.0 / self._rate - now)
            if wait:
                time.sleep(wait)
            self._last = time.time()


class PixivSyncPlugin(PluginBase):
    settings_schema = [
        {
            "key": "refresh_token",
            "label": "Pixiv Refresh Token",
            "type": "text",
            "central": False,  # 敏感项，不出现在集中设置面板
            "placeholder": "粘贴 refresh_token（非 access_token）",
            "help": "获取: gppt (github.com/eggplants/get-pixivpy-token) 或 Pixiv OAuth Flow",
        },
        {
            "key": "proxy",
            "label": "HTTP 代理",
            "type": "text",
            "placeholder": "http://127.0.0.1:7890",
            "help": "中国大陆访问 pixiv 需要代理；留空 = 直连",
        },
        {
            "key": "download_dir",
            "label": "下载目录",
            "type": "text",
            "placeholder": "默认: image-viewer 数据根目录",
            "help": "留空 = 写入 image-viewer 相册根目录，下载后自动出现在相册",
        },
        {
            "key": "download_original",
            "label": "下载原图（完整分辨率）",
            "type": "checkbox",
            "default": True,
            "help": "开启 = 下载画师原图（original，分辨率最高，文件较大）；关闭 = 下载 1200px 大图（master1200，更快更省空间）",
        },
        {
            "key": "multi_page_subfolder",
            "label": "多图作品放入子文件夹",
            "type": "checkbox",
            "default": True,
            "help": "开启 = 多图作品存放在 {作品id}/ 子文件夹中（如 123456/123456_p0.jpg），单图直接平铺；关闭 = 全部平铺在同一文件夹",
        },
        {
            "key": "workers",
            "label": "并发下载数",
            "type": "number",
            "default": 4,
            "min": 1,
            "max": 8,
            "help": "同时下载的画师/作品数；机械盘建议 1-2，SSD 可 4-8",
        },
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self._host = None
        self._pixiv_client: Optional[PixivClient] = None
        self._lock = threading.Lock()
        self._task_lock = threading.Lock()  # 保护 task 计数 / ids 集合 / 断点文件写
        self._rate_limiter = _RateLimiter(rate=3.0)  # app-api 请求限速 3/s
        self._task: Optional[Dict[str, Any]] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel_flag = False
        self._downloaded_ids: Optional[Set[int]] = None
        self._task = self._load_task()

    # ---------- 宿主访问 ----------

    def _get_host(self):
        if self._host is None:
            self._host = self.get_dependency("image-viewer")
            if self._host is None:
                raise RuntimeError("pixiv-sync 需要 image-viewer 插件已加载并声明依赖")
        return self._host

    def _root(self) -> Path:
        """下载根目录：用户自定义 download_dir，否则用宿主相册根目录。"""
        d = (self.setting("download_dir") or "").strip()
        if d:
            return Path(d).resolve()
        return self._get_host().get_data_root()

    def _cache_dir(self) -> Path:
        return self._root() / CACHE_SUBDIR

    # ---------- 客户端 ----------

    def _client(self) -> PixivClient:
        if self._pixiv_client is None:
            kwargs: Dict[str, Any] = {"timeout": 30}
            proxy = (self.setting("proxy") or "").strip()
            if proxy:
                kwargs["proxies"] = {"https": proxy, "http": proxy}
            self._pixiv_client = PixivClient(**kwargs)
        return self._pixiv_client

    # ---------- 去重记录 ----------

    def _ids_file(self) -> Path:
        return self._cache_dir() / "downloaded_ids.json"

    def _load_ids(self) -> Set[int]:
        if self._downloaded_ids is None:
            ids: Set[int] = set()
            try:
                data = json.loads(self._ids_file().read_text(encoding="utf-8"))
                raw = data.get("ids", []) if isinstance(data, dict) else []
                ids = set(int(x) for x in raw)
            except Exception:
                pass
            self._downloaded_ids = ids
        return self._downloaded_ids

    def _save_ids(self):
        try:
            self._cache_dir().mkdir(parents=True, exist_ok=True)
            self._ids_file().write_text(
                json.dumps({"ids": sorted(self._load_ids())}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[pixiv-sync] 保存去重记录失败: {e}")

    # ---------- 任务状态（断点） ----------

    def _tasks_file(self) -> Path:
        return self._cache_dir() / "tasks.json"

    def _load_task(self) -> Optional[Dict[str, Any]]:
        try:
            t = json.loads(self._tasks_file().read_text(encoding="utf-8"))
            if isinstance(t, dict) and t.get("state") in ("queued", "running"):
                t["state"] = "paused"  # 上次中断/重启，标记为可续跑
            return t
        except Exception:
            return None

    def _persist_task(self, task: Dict[str, Any]):
        try:
            self._cache_dir().mkdir(parents=True, exist_ok=True)
            self._tasks_file().write_text(
                json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _new_task(self, kind: str) -> Dict[str, Any]:
        task = {
            "kind": kind,
            "state": "queued",
            "done": 0,
            "total": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "current": "",
            "started_at": time.time(),
            "finished_at": None,
            "error": None,
        }
        self._task = task
        return task

    # ---------- 同步任务 ----------

    def sync_following(self) -> Dict:
        return self._start("following")

    def sync_bookmarks(self) -> Dict:
        return self._start("bookmarks")

    def _start(self, kind: str) -> Dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "error": "已有同步任务在运行"}
            self._cancel_flag = False
            self._thread = threading.Thread(
                target=self._run_sync, args=(kind,), daemon=True
            )
            self._thread.start()
        return {"ok": True, "data": {"kind": kind}}

    def cancel_task(self) -> Dict:
        with self._lock:
            if self._task and self._task.get("state") in ("queued", "running"):
                self._cancel_flag = True
        return {"ok": True}

    def _run_sync(self, kind: str):
        task = self._new_task(kind)
        self._persist_task(task)
        try:
            token = (self.setting("refresh_token") or "").strip()
            if not token:
                raise PixivError("未配置 refresh_token，请先在设置中填写")
            self._client().auth(refresh_token=token)
            task["state"] = "running"
            self._persist_task(task)

            if kind == "following":
                self._sync_following(task)
            else:
                self._sync_bookmarks(task)

            task["state"] = "cancelled" if self._cancel_flag else "done"
        except PixivError as e:
            task["state"] = "failed"
            task["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            task["state"] = "failed"
            task["error"] = f"{type(e).__name__}: {e}"
        finally:
            task["finished_at"] = time.time()
            task["current"] = ""
            self._save_ids()
            self._persist_task(task)

    def _sync_following(self, task: Dict[str, Any]):
        """同步画师（完整作品库）：关注列表 → 逐画师 user_illusts 翻页拉全部作品 → 并行下载。

        不再使用 illust_follow 新作流——那只会返回画师近期作品，历史作品会漏。
        """
        client = self._client()
        ids = self._load_ids()

        # 1. 翻页拉取全部关注画师（user_following）
        following: List[tuple] = []  # (user_id, name)
        qs = None
        while not self._cancel_flag:
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
        task["current"] = f"拉取 {len(following)} 位画师的作品列表…"
        self._persist_task(task)
        if self._cancel_flag:
            return

        # 2. 画师名单过滤：selected_artists.txt 存在且非空时只同步名单中的画师
        selected = self._load_selected_artists()
        if selected:
            before = len(following)
            following = [
                (uid, name) for uid, name in following
                if str(uid) in selected or name in selected
            ]
            task["current"] = f"名单过滤: {len(following)}/{before} 位画师"
            self._persist_task(task)
            if not following:
                raise PixivError(
                    "画师名单未匹配到任何关注画师，请检查 selected_artists.txt 中的名字或 id"
                )

        # 3. 主线程预解析画师目录（含改名迁移），避免并发写 artists 缓存
        subs = {uid: self._artist_dir(uid, name) for uid, name in following}

        # 4. 并行拉取所有画师的全部作品列表（低并发 + 节流，避免触发 pixiv 限流）
        def fetch_artist(uid: int) -> List[Dict[str, Any]]:
            illusts: List[Dict[str, Any]] = []
            q = None
            retries_429 = 0
            while not self._cancel_flag:
                try:
                    if q:
                        q.pop("user_id", None)
                        q.pop("type", None)
                        page = client.user_illusts(uid, **q)
                    else:
                        page = client.user_illusts(uid)
                    illusts.extend(page.get("illusts", []) or [])
                    next_url = page.get("next_url")
                    if not next_url:
                        break
                    q = client.parse_qs(next_url)
                    time.sleep(0.25)  # 请求间隔，避免 429
                except PixivError as e:
                    if "429" in str(e):  # Rate Limit：退避后重试
                        retries_429 += 1
                        if retries_429 > 3:
                            raise
                        time.sleep(5 * retries_429)  # 5s / 10s / 15s
                    else:
                        raise
            return illusts

        all_illusts: List[tuple] = []  # (sub, illust)
        with ThreadPoolExecutor(max_workers=4) as pool:
            # 把 uid 与 future 绑定，避免在结果循环中引用推导式变量
            tasks = [(uid, pool.submit(fetch_artist, uid)) for uid, _ in following]
            for uid, future in tasks:
                try:
                    illusts = future.result()
                except Exception as e:  # noqa: BLE001
                    print(f"[pixiv-sync] 拉取画师列表失败: {e}")
                    continue
                with self._task_lock:
                    for illust in illusts:
                        all_illusts.append((subs[uid], illust))
                    task["total"] = len(all_illusts)  # 实时进度
                    task["current"] = f"拉取作品列表 {len(all_illusts)}…"
                    self._persist_task(task)
        task["total"] = len(all_illusts)
        task["current"] = ""
        self._persist_task(task)
        if self._cancel_flag:
            return
        if not all_illusts and following:
            raise PixivError(
                f"拉取 {len(following)} 位画师的作品列表全部失败（可能触发 pixiv 限流 429），请稍后重试"
            )
        task["current"] = f"开始下载 {len(all_illusts)} 个作品…"
        self._persist_task(task)

        # 4. 全局并行下载（提交顺序按画师，线程池满载）
        workers = self._workers()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._process_illust, illust, task, ids, sub)
                for sub, illust in all_illusts
            ]
            for future in futures:
                try:
                    future.result()
                except Exception as e:  # noqa: BLE001
                    print(f"[pixiv-sync] 下载任务异常: {e}")

    def _workers(self) -> int:
        try:
            return max(1, min(8, int(self.setting("workers", 4))))
        except (TypeError, ValueError):
            return 4

    def _sync_bookmarks(self, task: Dict[str, Any]):
        """当前用户公开收藏：翻页拉全量后并行下载，统一放入 bookmarks 目录。"""
        client = self._client()
        ids = self._load_ids()
        sub = self._root() / "pixiv" / "bookmarks"

        all_illusts: List[Dict[str, Any]] = []
        qs = None
        retries_429 = 0
        while not self._cancel_flag:
            try:
                if qs:
                    qs.pop("user_id", None)
                    page = client.user_bookmarks_illust(client.user_id, **qs)
                else:
                    page = client.user_bookmarks_illust(client.user_id)
                all_illusts.extend(page.get("illusts", []) or [])
                next_url = page.get("next_url")
                if not next_url:
                    break
                qs = client.parse_qs(next_url)
                time.sleep(0.25)
            except PixivError as e:
                if "429" in str(e):
                    retries_429 += 1
                    if retries_429 > 3:
                        raise
                    time.sleep(5 * retries_429)
                else:
                    raise
        task["total"] = len(all_illusts)
        self._persist_task(task)
        if self._cancel_flag:
            return

        workers = self._workers()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._process_illust, illust, task, ids, sub)
                for illust in all_illusts
            ]
            for future in futures:
                try:
                    future.result()
                except Exception as e:  # noqa: BLE001
                    print(f"[pixiv-sync] 下载任务异常: {e}")

    def _process_illust(
        self, illust: Dict[str, Any], task: Dict[str, Any], ids: Set[int], sub: Path
    ):
        iid = illust.get("id")
        if iid is None:
            return
        task["current"] = f"{illust.get('title', '')} ({iid})"
        if int(iid) in ids:
            with self._task_lock:
                task["skipped"] += 1
                task["done"] += 1
                self._persist_task(task)
            return

        urls = self._all_image_urls(illust)
        # 多图作品放入 {作品id}/ 子文件夹，单图直接平铺
        subfolder = bool(self.setting("multi_page_subfolder", True)) and len(urls) > 1
        target = sub / str(iid) if subfolder else sub
        ok = 0
        fail = 0
        for idx, url in enumerate(urls):
            ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
            name = f"{iid}{ext}" if len(urls) == 1 else f"{iid}_p{idx}{ext}"
            try:
                if self._client().download(url, path=str(target), name=name):
                    ok += 1
            except PixivError:
                fail += 1
        with self._task_lock:
            task["failed"] += fail
            if ok > 0 or not urls:
                ids.add(int(iid))
                task["downloaded"] += 1
            task["done"] += 1
            self._persist_task(task)

    # ---------- 画师名单（selected_artists.txt：只同步指定画师） ----------

    _SELECTED_TEMPLATE = (
        "# Pixiv 同步画师名单\n"
        "# 每行一个画师：填画师名字或 Pixiv 用户 id（# 开头为注释，空行忽略）\n"
        "# 示例:\n"
        "#   柠檬静静静静\n"
        "#   66477791\n"
        "#\n"
        "# 本文件存在且非空时，同步画师只处理名单中的画师；\n"
        "# 删除本文件或清空内容 = 同步全部关注画师\n"
    )

    def _selected_file(self) -> Path:
        return self._cache_dir() / "selected_artists.txt"

    @staticmethod
    def _read_text_robust(file: Path) -> str:
        """读取文本文件，兼容 UTF-8 / UTF-8 BOM / GBK（记事本默认 ANSI 编码）。"""
        for enc in ("utf-8-sig", "gb18030"):
            try:
                return file.read_text(encoding=enc)
            except (UnicodeDecodeError, OSError):
                continue
        return ""

    def _load_selected_artists(self) -> Set[str]:
        """读取画师名单：每行一个画师（名字或 Pixiv 用户 id），# 注释、空行忽略。

        文件不存在或为空 = 同步全部关注画师。
        """
        selected: Set[str] = set()
        try:
            text = self._read_text_robust(self._selected_file())
        except FileNotFoundError:
            return selected
        except Exception as e:
            print(f"[pixiv-sync] 读取画师名单失败: {e}")
            return selected
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            selected.add(line)
        return selected

    def open_config(self) -> Dict:
        """打开画师名单配置文件所在文件夹。

        每次调用都会重写注释模板（保留用户填写的画师行，非注释行），
        方便用户始终能看到用法说明。
        """
        try:
            file = self._selected_file()
            file.parent.mkdir(parents=True, exist_ok=True)
            # 保留用户填写的画师行（非注释、非空），模板每次都写入
            artists: List[str] = []
            if file.exists():
                for line in self._read_text_robust(file).splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        artists.append(line)
            body = self._SELECTED_TEMPLATE
            if artists:
                body += "\n" + "\n".join(artists) + "\n"
            file.write_text(body, encoding="utf-8")
            os.startfile(str(file.parent))
            return {"ok": True, "file": str(file)}
        except Exception as e:
            return {"ok": False, "error": f"打开失败: {e}"}

    # ---------- 画师目录（名字命名 + id 缓存 + 改名迁移） ----------

    def _artists_file(self) -> Path:
        return self._cache_dir() / "artists.json"

    def _load_artist_cache(self) -> Dict[str, str]:
        try:
            data = json.loads(self._artists_file().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _save_artist_cache(self, cache: Dict[str, str]):
        try:
            self._cache_dir().mkdir(parents=True, exist_ok=True)
            self._artists_file().write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            print(f"[pixiv-sync] 保存画师缓存失败: {e}")

    def _artist_dir(self, uid: int, name: str) -> Path:
        """返回画师作品目录（以名字命名）。

        - 画师 id → 名字 记入本地缓存，画师改名后仍能识别为同一人；
        - 检测到改名时把旧名字目录迁移合并到新名字目录，避免"分家"。
        """
        base = self._root() / "pixiv" / "following"
        cache = self._load_artist_cache()
        key = str(uid)
        new_name = self._sanitize(name) or str(uid)
        old_name = cache.get(key)

        if old_name and old_name != new_name:
            old_dir = base / old_name
            new_dir = base / new_name
            if old_dir.exists() and old_dir != new_dir:
                try:
                    if not new_dir.exists():
                        old_dir.rename(new_dir)
                    else:  # 新名字目录已存在 → 合并（不覆盖同名文件）
                        for item in old_dir.iterdir():
                            dest = new_dir / item.name
                            if item.is_dir():
                                if dest.exists():
                                    for f in item.iterdir():
                                        if not (dest / f.name).exists():
                                            shutil.move(str(f), str(dest / f.name))
                                    item.rmdir()
                                else:
                                    item.rename(dest)
                            elif not dest.exists():
                                shutil.move(str(item), str(dest))
                        old_dir.rmdir()
                    print(f"[pixiv-sync] 画师 {uid} 改名: {old_name} → {new_name}，目录已迁移")
                except OSError as e:
                    print(f"[pixiv-sync] 画师改名目录迁移失败 {uid}: {e}")

        cache[key] = new_name
        self._save_artist_cache(cache)
        return base / new_name

    def _all_image_urls(self, illust: Dict[str, Any]) -> List[str]:
        """多图作品返回全部页 URL，单图返回封面 URL。

        开启 download_original 时优先取画师原图（original，完整分辨率），
        取不到时回退 1200px 大图（large）。
        """
        want_original = bool(self.setting("download_original", True))

        def pick(page: Dict[str, Any]) -> Optional[str]:
            urls = page.get("image_urls") or {}
            if want_original:
                orig = urls.get("original")
                if orig:
                    return orig
            return urls.get("large")

        meta_pages = illust.get("meta_pages") or []
        if meta_pages:
            urls = [u for u in (pick(p) for p in meta_pages) if u]
            if urls:
                return urls
        # 单图：meta_single_page.original_image_url 或 image_urls
        if want_original:
            orig = (illust.get("meta_single_page") or {}).get("original_image_url")
            if orig:
                return [orig]
        url = (illust.get("image_urls") or {}).get("large")
        return [url] if url else []

    @staticmethod
    def _sanitize(name: str) -> str:
        return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip() or "unknown"

    # ---------- 状态 / API ----------

    def get_status(self) -> Dict:
        with self._lock:
            task = dict(self._task) if self._task else None
        root = None
        try:
            root = str(self._root())
        except Exception:
            pass
        return {
            "task": task,
            "root_dir": root,
            "token_configured": bool((self.setting("refresh_token") or "").strip()),
            "downloaded_total": len(self._load_ids()),
            "running": bool(self._thread and self._thread.is_alive()),
            "selected_artists": len(self._load_selected_artists()),
            "selected_file": str(self._selected_file()),
        }

    def register_api(self) -> dict:
        return {
            "get_status": self.get_status,
            "sync_following": self.sync_following,
            "sync_bookmarks": self.sync_bookmarks,
            "cancel_task": self.cancel_task,
            "open_config": self.open_config,
            "get_settings": self.get_settings,
            "save_settings": self.save_settings,
        }

    def get_extensions(self) -> List[dict]:
        """注册到 image-viewer 左侧栏的扩展入口（与 image-cleaner 同构）。"""
        return [
            {
                "host": "image-viewer",
                "id": "pixiv-sync",
                "label": "Pixiv 同步",
                "icon": "🎨",
                "description": "同步下载关注画师新作与收藏画作",
                "embedUrl": "/plugins/pixiv-sync/frontend/index.html",
                "placement": "sidebar",
                "scope": "all",
            }
        ]

    def on_settings_changed(self, changed_keys):
        if changed_keys & {"refresh_token", "proxy", "download_dir"}:
            self._pixiv_client = None
            self._downloaded_ids = None
