"""Pixiv 同步插件：image-viewer 的 Companion 插件（入口）。

功能：
  - 同步画师：拉取关注画师全部作品，按画师目录下载
  - 同步喜欢：拉取当前用户公开收藏，按作品画师归入目录
  - 去重：已下载作品 id 记入 <root>/.cache/pixiv-sync/downloaded_ids.json（两同步共用）
  - 永久跳过：同步时 404/已删除的作品记入 failed_ids.json，不再每次重试；
    可通过「重试失败作品」按钮一键清除并自动重新同步一次
  - 断点：任务状态每张一写 <root>/.cache/pixiv-sync/tasks.json，重启后恢复 paused

实现已拆分到 backend/pixiv_sync/（limiter/tasks/store/db/artist/scan/download/oauth）。
本文件只保留插件类：设置、线程编排、API 挂载与宿主交互。
"""

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from shell.backend.plugin_base import PluginBase

from pixiv_mini import PixivClient, PixivError

from pixiv_sync import download, oauth, scan, store, tasks
from pixiv_sync.db import WorksDB
from pixiv_sync.limiter import RateLimitError, RateLimiter
from pixiv_sync.store import collect_existing_ids, rebuild_existing

CACHE_SUBDIR = Path(".cache") / "pixiv-sync"


class PixivSyncPlugin(PluginBase):
    settings_schema = [
        {
            "key": "refresh_token",
            "label": "Pixiv Refresh Token",
            "type": "text",
            "central": False,  # 敏感项，不出现在集中设置面板
            "placeholder": "粘贴 refresh_token（非 access_token）",
            "help": "获取: gppt (github.com/eggplants/get-pixivpy-token) 或插件内 OAuth 向导",
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
        # 注：原图下载与多图子文件夹为固定行为（默认开启），不提供设置开关；
        #     如需调整请直接改 backend/pixiv_sync/download.py 中 all_image_urls 的
        #     want_original 与 process_illust 的 subfolder 判定。
        {
            "key": "workers",
            "label": "并发下载数",
            "type": "number",
            "default": 4,
            "min": 1,
            "max": 8,
            "help": "同时下载的画师/作品数；机械盘建议 1-2，SSD 可 4-8",
        },
        {
            "key": "max_download",
            "label": "单次同步上限（条）",
            "type": "number",
            "default": 100,
            "min": 1,
            "max": 10000,
            "help": "每次「同步画师/同步喜欢」最多下载的作品数；0 = 不限。想分批下载可设小值，下完再点同步继续",
        },
        {
            "key": "max_refresh",
            "label": "单次刷新上限（条）",
            "type": "number",
            "default": 500,
            "min": 1,
            "max": 10000,
            "help": "每次「刷新关注/喜欢名单」最多拉取并加入清单的待下载条数；列表请求受限速，设小值分批刷新更稳；0 = 不限",
        },
        {
            "key": "rate_limit",
            "label": "API 请求速率（次/秒）",
            "type": "number",
            "default": 3,
            "min": 1,
            "max": 10,
            "help": "app-api 请求限速，间隔带随机抖动；pixiv 限流阈值约 3/s，调高有 429 风险",
        },
        {
            "key": "scan_workers",
            "label": "并行拉取画师数（滑动窗口）",
            "type": "number",
            "default": 4,
            "min": 1,
            "max": 8,
            "help": "刷新名单时同时拉取列表的画师数；总速率仍受 rate_limit 限制，此值只是滑动窗口大小",
        },
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self._host = None
        self._pixiv_client: Optional[PixivClient] = None
        self._lock = threading.Lock()          # 保护任务线程启动/取消
        self._task_lock = threading.Lock()      # 保护计数 / ids / failed 集合 / 清单写
        self._rate_limiter = RateLimiter(rate=3.0)  # app-api 请求限速 3/s
        self._task: Optional[Dict[str, Any]] = None
        self._thread: Optional[threading.Thread] = None
        self._cancel_flag = False
        self._downloaded_ids: Optional[Set[int]] = None
        self._failed_ids: Optional[Set[int]] = None
        self._oauth_verifier: Optional[str] = None
        self._db_wrapper: Optional[WorksDB] = None
        self._task = tasks.load_task(self._tasks_file())

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

    # ---------- 缓存文件路径 ----------

    def _ids_file(self) -> Path:
        return self._cache_dir() / "downloaded_ids.json"

    def _failed_file(self) -> Path:
        return self._cache_dir() / "failed_ids.json"

    def _tasks_file(self) -> Path:
        return self._cache_dir() / "tasks.json"

    def _artists_file(self) -> Path:
        return self._cache_dir() / "artists.json"

    def _works_file(self) -> Path:
        return self._cache_dir() / "works.db"

    # ---------- 客户端 ----------

    def _client(self) -> PixivClient:
        if self._pixiv_client is None:
            kwargs: Dict[str, Any] = {"timeout": 30}
            proxy = (self.setting("proxy") or "").strip()
            if proxy:
                kwargs["proxies"] = {"https": proxy, "http": proxy}
            self._pixiv_client = PixivClient(**kwargs)
        return self._pixiv_client

    # ---------- 去重记录 / 永久跳过记录 ----------

    def _load_ids(self) -> Set[int]:
        if self._downloaded_ids is None:
            self._downloaded_ids = store.load_ids(self._ids_file())
        return self._downloaded_ids

    def _save_ids(self):
        store.save_ids(self._ids_file(), self._load_ids())

    def _load_failed_ids(self) -> Set[int]:
        if self._failed_ids is None:
            self._failed_ids = store.load_failed_ids(self._failed_file())
        return self._failed_ids

    def _save_failed_ids(self):
        store.save_failed_ids(self._failed_file(), self._load_failed_ids())

    # ---------- 清单 SQLite（懒连接） ----------

    def _db(self) -> WorksDB:
        if self._db_wrapper is None:
            self._db_wrapper = WorksDB(self._works_file(), self._task_lock)
        return self._db_wrapper

    # ---------- 已有图片扫描（手动放入的旧图并入去重） ----------

    def _scan_existing_ids(self) -> int:
        """扫描下载目录（<root>/pixiv/）中已存在的图片，按命名规则提取作品 id 并入去重集合。

        用于识别用户手动放入的旧图：文件名符合 `{id}.jpg` / `{id}_p0.jpg` 规则即可被识别，
        之后全量更新会直接跳过，不会重复检查/下载。
        """
        root = self._root() / "pixiv"
        ids = self._load_ids()
        before = len(ids)
        ids |= collect_existing_ids(root)
        found = len(ids) - before
        if found:
            with self._task_lock:
                store.save_ids(self._ids_file(), ids)
        return found

    # ---------- 待下载清单 / 下载 任务 ----------

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

    def sync_following(self) -> Dict:
        return self._start("following")

    def sync_bookmarks(self) -> Dict:
        return self._start("bookmarks")

    def _run_sync(self, kind: str):
        task = tasks.new_task(kind)
        tasks.persist_task(self._tasks_file(), task)
        self._task = task
        ids = self._load_ids()
        failed = self._load_failed_ids()
        try:
            token = (self.setting("refresh_token") or "").strip()
            if not token:
                raise PixivError("未配置 refresh_token，请先在设置中填写")
            self._client().auth(refresh_token=token)
            # pixiv 可能在刷新时轮换 refresh_token：回写设置，避免下次用旧值失效
            rotated = self._client().refresh_token
            if rotated and rotated != token:
                self.update_setting("refresh_token", rotated)
                print("[pixiv-sync] refresh_token 已轮换，自动回写设置")
            # 识别用户手动放入的旧图（按命名规则提取 id 并入去重集合）
            scanned = self._scan_existing_ids()
            if scanned:
                print(f"[pixiv-sync] 扫描到 {scanned} 个已存在作品，并入去重集合")
            task["state"] = "running"
            tasks.persist_task(self._tasks_file(), task)

            download.download_pending(self, task, ids, failed, kind)

            task["state"] = "cancelled" if self._cancel_flag else "done"
        except RateLimitError as e:
            task["state"] = "failed"
            task["error"] = str(e)
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
            store.save_failed_ids(self._failed_file(), failed)
            tasks.persist_task(self._tasks_file(), task)

    def _start_refresh(self, kind: str) -> Dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "error": "已有任务在运行"}
            self._cancel_flag = False
            self._thread = threading.Thread(
                target=self._run_refresh, args=(kind,), daemon=True
            )
            self._thread.start()
        return {"ok": True}

    def refresh_following_lists(self) -> Dict:
        """刷新关注画师作品名单：拉取列表生成待下载清单（不下载）。"""
        return self._start_refresh("following")

    def refresh_bookmarks_lists(self) -> Dict:
        """刷新喜欢画作名单：拉取收藏列表生成待下载清单（不下载）。"""
        return self._start_refresh("bookmarks")

    def _run_refresh(self, kind: str):
        task = tasks.new_task(f"refresh_{kind}")
        task["state"] = "running"
        tasks.persist_task(self._tasks_file(), task)
        self._task = task
        try:
            token = (self.setting("refresh_token") or "").strip()
            if not token:
                raise PixivError("未配置 refresh_token，请先在设置中填写")
            self._client().auth(refresh_token=token)
            ids = self._load_ids()
            if kind == "following":
                items, scan_data = scan.collect_following_pending(self, task, ids)
            else:
                items, scan_data = scan.collect_bookmarks_pending(self, task, ids)
            self._db().save_pending(kind, items, scan_data)
            task["state"] = "cancelled" if self._cancel_flag else "done"
            task["total"] = len(items)
            done_cnt = sum(1 for i in items if i.get("done"))
            task["current"] = (
                f"清单 {len(items)} 条（已下 {done_cnt}）· 已扫描"
                + ("完成" if (scan_data or {}).get("complete") else "部分（可再点刷新继续）")
            )
        except RateLimitError as e:
            task["state"] = "failed"
            task["error"] = str(e)
        except PixivError as e:
            task["state"] = "failed"
            task["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            task["state"] = "failed"
            task["error"] = f"{type(e).__name__}: {e}"
        finally:
            task["finished_at"] = time.time()
            tasks.persist_task(self._tasks_file(), task)

    def cancel_task(self) -> Dict:
        with self._lock:
            if self._task and self._task.get("state") in ("queued", "running"):
                self._cancel_flag = True
        return {"ok": True}

    def retry_failed(self) -> Dict:
        """一键清除永久跳过的 404 记录，并立即重新同步一次（重试失败作品）。

        重新同步的来源取最近一次任务（画师/喜欢）；无历史任务时默认同步画师。
        清空后失败的清单条目保持未下载状态，会自然回到待下载队列。
        """
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "error": "已有任务在运行"}
            failed = self._load_failed_ids()
            with self._task_lock:
                self._failed_ids = set()
            store.save_failed_ids(self._failed_file(), set())
            kind = "following"
            t = tasks.load_task(self._tasks_file())
            if t and t.get("kind") in ("following", "bookmarks"):
                kind = t["kind"]
            self._cancel_flag = False
            self._thread = threading.Thread(
                target=self._run_sync, args=(kind,), daemon=True
            )
            self._thread.start()
        return {"ok": True, "cleared": len(failed), "kind": kind}

    # ---------- 内置 OAuth 向导 ----------

    def start_oauth(self) -> Dict:
        return oauth.start(self)

    def finish_oauth(self, code: str) -> Dict:
        return oauth.finish(self, code)

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

    # ---------- 下载记录维护 ----------

    def _max_download(self) -> int:
        try:
            return max(0, int(self.setting("max_download", 100)))
        except (TypeError, ValueError):
            return 100

    def _max_refresh(self) -> int:
        try:
            return max(0, int(self.setting("max_refresh", 500)))
        except (TypeError, ValueError):
            return 500

    def _scan_workers(self) -> int:
        """刷新名单的并行拉取画师数（滑动窗口）。"""
        try:
            return max(1, min(8, int(self.setting("scan_workers", 4))))
        except (TypeError, ValueError):
            return 4

    def _workers(self) -> int:
        try:
            return max(1, min(8, int(self.setting("workers", 4))))
        except (TypeError, ValueError):
            return 4

    def refresh_downloaded(self) -> Dict:
        """刷新已下载记录：扫描本地重建 ids（手动删过的移除、手动加的导入、0 字节清理）。"""
        try:
            existing, zero = rebuild_existing(self._root() / "pixiv")
            with self._task_lock:
                self._downloaded_ids = set(existing)
                store.save_ids(self._ids_file(), existing)
            return {"ok": True, "total": len(existing), "zero_removed": zero}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def verify_downloaded(self) -> Dict:
        """校验已下载内容：移除记录中本地无有效文件的失效 id，下次同步自动重下。"""
        try:
            existing, zero = rebuild_existing(self._root() / "pixiv")
            ids = self._load_ids()
            stale = [iid for iid in ids if iid not in existing]
            with self._task_lock:
                for iid in stale:
                    ids.discard(iid)
                store.save_ids(self._ids_file(), ids)
            return {
                "ok": True,
                "stale_removed": len(stale),
                "zero_removed": zero,
                "total": len(ids),
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------- 状态 / API ----------

    def get_status(self) -> Dict:
        with self._lock:
            task = dict(self._task) if self._task else None
        root = None
        try:
            root = str(self._root())
        except Exception:
            pass
        ids = self._load_ids()

        def cnt(db, kind: str, done: Optional[int] = None) -> int:
            try:
                return db.counts(kind, done)
            except Exception:
                return 0

        db = self._db()
        following_total = cnt(db, "following")
        following_done = cnt(db, "following", 1)
        bookmarks_total = cnt(db, "bookmarks")
        bookmarks_done = cnt(db, "bookmarks", 1)
        return {
            "task": task,
            "root_dir": root,
            "token_configured": bool((self.setting("refresh_token") or "").strip()),
            "downloaded_total": len(ids),
            "running": bool(self._thread and self._thread.is_alive()),
            "selected_artists": len(self._load_selected_artists()),
            "selected_file": str(self._selected_file()),
            # 清单统计
            "following_total": following_total,
            "following_done": following_done,
            "pending_following": following_total - following_done,
            "bookmarks_total": bookmarks_total,
            "bookmarks_done": bookmarks_done,
            "pending_bookmarks": bookmarks_total - bookmarks_done,
            "other_done": max(0, len(ids) - following_done - bookmarks_done),
            # 永久跳过（404/已删除）
            "failed_skipped": len(self._load_failed_ids()),
        }

    def register_api(self) -> dict:
        return {
            "get_status": self.get_status,
            "sync_following": self.sync_following,
            "sync_bookmarks": self.sync_bookmarks,
            "refresh_following_lists": self.refresh_following_lists,
            "refresh_bookmarks_lists": self.refresh_bookmarks_lists,
            "refresh_downloaded": self.refresh_downloaded,
            "verify_downloaded": self.verify_downloaded,
            "retry_failed": self.retry_failed,
            "cancel_task": self.cancel_task,
            "open_config": self.open_config,
            "start_oauth": self.start_oauth,
            "finish_oauth": self.finish_oauth,
            "get_settings": self.get_settings,
            "save_settings": self.save_settings,
        }

    def get_extensions(self) -> List[dict]:
        """注册到 image-viewer 左侧栏的扩展入口。"""
        return [
            {
                "host": "image-viewer",
                "id": "pixiv-sync",
                "label": "Pixiv 同步",
                "icon": "🎨",
"description": "同步下载关注画师新作与收藏画作",
            "section": "Pixiv 同步",
            "embedUrl": "/plugins/pixiv-sync/frontend/index.html",
            "placement": "sidebar",
            "scope": "all",
            }
        ]

    def on_settings_changed(self, changed_keys):
        if changed_keys & {"refresh_token", "proxy", "download_dir"}:
            self._pixiv_client = None
            self._downloaded_ids = None
        if "rate_limit" in changed_keys:
            try:
                self._rate_limiter.set_rate(float(self.setting("rate_limit", 3)))
            except (TypeError, ValueError):
                pass