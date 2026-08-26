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
import random
import re
import shutil
import threading
import time
import webbrowser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from shell.backend.plugin_base import PluginBase

from pixiv_mini import PixivClient, PixivError

CACHE_SUBDIR = Path(".cache") / "pixiv-sync"


class RateLimitError(PixivError):
    """Pixiv 限流（HTTP 429）：任务应立即停止，等待冷却后重试。"""


class _RateLimiter:
    """全局请求速率控制（令牌桶），避免触发 pixiv app-api 的 429 限流。

    rate 为每秒请求数（可配置）；间隔带随机抖动（-20% ~ +40%），
    避免固定节律、更接近自然请求模式。
    """

    def __init__(self, rate: float = 3.0):
        self._lock = threading.Lock()
        self._last = 0.0
        self._rate = max(0.5, min(10.0, rate))

    def set_rate(self, rate: float):
        with self._lock:
            self._rate = max(0.5, min(10.0, float(rate)))

    def wait(self):
        with self._lock:
            now = time.time()
            base = 1.0 / self._rate
            interval = base * (1 + random.uniform(-0.2, 0.4))
            wait = max(0.0, self._last + interval - now)
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
            "help": "刷新名单时同时拉取列表的画师数；总速率仍受 rate_limit 限制，此值只是滑动窗口大小（非本插件被禁用时可独立生效）",
        },
        {
            "key": "pixeval_dir",
            "label": "Pixeval 目录（导入用）",
            "type": "text",
            "placeholder": "如 G:\\图库\\PIXEVAL，留空 = 不导入",
            "help": "第三方客户端 pixeval 的下载目录。设置后同步时会自动把其中图片按画师导入（文件名规范化；与本地重复的以本地为准并删除 pixeval 副本）",
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
        self._oauth_verifier: Optional[str] = None
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
            data: Dict[str, Any] = {"ids": sorted(self._load_ids())}
            sources = self._load_sources()
            if sources:
                data["sources"] = sources
            self._ids_file().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[pixiv-sync] 保存去重记录失败: {e}")

    def _load_sources(self) -> Dict[str, str]:
        """作品来源标记：{作品id: "following"|"bookmarks"}；旧数据无标记视为 other。"""
        try:
            data = json.loads(self._ids_file().read_text(encoding="utf-8"))
            s = data.get("sources")
            if isinstance(s, dict):
                return {str(k): str(v) for k, v in s.items()}
        except Exception:
            pass
        return {}

    def _mark_sources(self, id_to_kind: Dict[int, str]):
        """给作品 id 标记来源（合并写入 ids 文件，保留其他字段）。"""
        try:
            with self._task_lock:
                data = json.loads(self._ids_file().read_text(encoding="utf-8")) \
                    if self._ids_file().exists() else {}
                if not isinstance(data, dict):
                    data = {}
                sources = dict(data.get("sources") or {})
                for iid, kind in id_to_kind.items():
                    sources[str(iid)] = kind
                data["sources"] = sources
                if "ids" not in data or not isinstance(data.get("ids"), list):
                    data["ids"] = sorted(self._load_ids())
                self._ids_file().write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf-8"
                )
        except Exception as e:
            print(f"[pixiv-sync] 保存来源标记失败: {e}")

    # 图片文件名 → 作品 id：123456.jpg / 123456_p0.jpg / 123456_p0.png ...
    _ID_NAME_RE = re.compile(r"^(\d+)(?:_p\d+)?\.(?:jpe?g|png|gif|webp)$", re.IGNORECASE)

    def _collect_image_files(self, base: Path) -> List[tuple]:
        """收集目录下所有按规则命名的图片文件，返回 [(作品id, 文件Path)]。"""
        items: List[tuple] = []
        try:
            for current, dir_names, filenames in os.walk(base):
                dir_names[:] = [d for d in dir_names if not d.startswith(".")]
                for name in filenames:
                    m = self._ID_NAME_RE.match(name)
                    if m:
                        items.append((int(m.group(1)), Path(current) / name))
        except OSError:
            pass
        return items

    @staticmethod
    def _move_or_drop(src: Path, dest: Path):
        """移动文件；目标已存在则删除源（视为重复副本）。"""
        if dest.exists():
            src.unlink()
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))

    @staticmethod
    def _merge_dir(src: Path, dest: Path):
        """把 src 目录内容合并进 dest（不覆盖同名文件）。"""
        for item in src.iterdir():
            target = dest / item.name
            if item.is_dir():
                if target.exists():
                    PixivSyncPlugin._merge_dir(item, target)
                    item.rmdir()
                else:
                    item.rename(target)
            elif not target.exists():
                shutil.move(str(item), str(target))

    def _migrate_failed_file(self) -> Path:
        return self._cache_dir() / "migrate_failed_ids.json"

    def _load_migrate_failed(self) -> Set[int]:
        """已确认无法迁移的作品 id（如作品在 pixiv 已删除返回 404）。"""
        try:
            data = json.loads(self._migrate_failed_file().read_text(encoding="utf-8"))
            return set(int(x) for x in data) if isinstance(data, list) else set()
        except Exception:
            return set()

    def _save_migrate_failed(self, ids: Set[int]):
        try:
            self._cache_dir().mkdir(parents=True, exist_ok=True)
            self._migrate_failed_file().write_text(
                json.dumps(sorted(ids)), encoding="utf-8"
            )
        except Exception:
            pass

    def _migrate_legacy_layout(self) -> int:
        """迁移旧目录结构（v0.1）到统一画师目录 <root>/pixiv/{画师名}/。

        旧: pixiv/following/{画师}/...   +   pixiv/bookmarks/...
        新: pixiv/{画师}/...
        - following 目录整体上移；
        - bookmarks 中与已有画师作品 id 相同的文件直接归位（画师名从目录推断，零联网）；
        - 其余文件联网 illust_detail 查画师名归位（限速 3/s，失败保留原地）。
        返回 bookmarks 归位/去重处理的文件数；无旧结构返回 0。
        """
        root = self._root() / "pixiv"
        if not root.exists():
            return 0

        # 1. following/{画师} → {画师}（目录上移/合并）
        following = root / "following"
        if following.exists():
            try:
                for d in sorted(following.iterdir()):
                    if not d.is_dir():
                        continue
                    dest = root / d.name
                    if dest.exists():
                        self._merge_dir(d, dest)
                        d.rmdir()
                    else:
                        d.rename(dest)
                following.rmdir()
            except OSError as e:
                print(f"[pixiv-sync] 迁移 following 目录失败: {e}")

        # 2. bookmarks 归位
        bookmarks = root / "bookmarks"
        if not bookmarks.exists():
            return 0

        # 2.1 本地映射：作品 id → 所在画师目录（来自已有画师目录，跳过 bookmarks 自身）
        id_to_dir: Dict[int, Path] = {}
        bm_resolved = bookmarks.resolve()
        for current, dir_names, filenames in os.walk(root):
            dir_names[:] = [d for d in dir_names if not d.startswith(".")]
            current_p = Path(current)
            if current_p.resolve() == bm_resolved or bm_resolved in current_p.resolve().parents:
                continue
            for name in filenames:
                m = self._ID_NAME_RE.match(name)
                if m:
                    id_to_dir.setdefault(int(m.group(1)), current_p)

        # 2.2 归位
        client = self._client()
        migrated = 0
        pending_lookup: List[Path] = []
        for iid, src in self._collect_image_files(bookmarks):
            artist_dir = id_to_dir.get(iid)
            if artist_dir is not None:
                rel = src.relative_to(bookmarks)
                self._move_or_drop(src, artist_dir / rel)
                migrated += 1
            else:
                pending_lookup.append(src)

        # 2.3 联网查询画师名（限速 3/s），失败保留原地
        failed_ids = self._load_migrate_failed()
        for src in pending_lookup:
            m = self._ID_NAME_RE.match(src.name)
            if not m:
                continue
            iid = int(m.group(1))
            if iid in failed_ids:
                continue  # 已确认无法迁移（如作品已删除 404），不再重复查询
            try:
                self._rate_limiter.wait()
                detail = client.illust_detail(iid)
                user = (detail.get("illust") or {}).get("user") or {}
                uid = user.get("id")
                if not uid:
                    failed_ids.add(iid)
                    continue
                artist_dir = self._artist_dir(int(uid), str(user.get("name") or uid))
                rel = src.relative_to(bookmarks)
                self._move_or_drop(src, artist_dir / rel)
                migrated += 1
            except Exception as e:  # noqa: BLE001
                # 429 → 立即停止（避免继续请求加剧限流）；
                # 404（作品已删除）→ 记录为永久失败，后续跳过；其他 → 下次重试
                if "429" in str(e):
                    raise RateLimitError(
                        "触发 Pixiv 限流（429），迁移已停止，请等待冷却后重试"
                    ) from None
                if "404" in str(e):
                    failed_ids.add(iid)
                print(f"[pixiv-sync] 迁移 {src.name} 失败（保留原地）: {e}")
        self._save_migrate_failed(failed_ids)

        # 清理 bookmarks 下残留的空子目录（文件已归位），再尝试删除目录本身。
        # Windows 下先清除 thumbs.db / Desktop.ini 等隐藏文件，否则 rmdir 会失败残留空文件夹。
        _JUNK = {"thumbs.db", "desktop.ini"}
        for d in sorted(
            (p for p in bookmarks.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                for f in d.iterdir():
                    if f.is_file() and f.name.lower() in _JUNK:
                        f.unlink()
                d.rmdir()
            except OSError:
                pass
        try:
            bookmarks.rmdir()  # 空目录才删除；仍有无法识别的文件会失败忽略
        except OSError:
            pass
        return migrated

    def _scan_existing_ids(self) -> int:
        """扫描下载目录（<root>/pixiv/）中已存在的图片，按命名规则提取作品 id 并入去重集合。

        用于识别用户手动放入的旧图：文件名符合 `{id}.jpg` / `{id}_p0.jpg` 规则即可被识别，
        之后全量更新会直接跳过，不会重复检查/下载。
        """
        root = self._root() / "pixiv"
        if not root.exists():
            return 0
        ids = self._load_ids()
        found = 0
        try:
            for current, dir_names, filenames in os.walk(root):
                dir_names[:] = [d for d in dir_names if not d.startswith(".")]
                for name in filenames:
                    m = self._ID_NAME_RE.match(name)
                    if not m:
                        continue
                    iid = int(m.group(1))
                    if iid not in ids:
                        ids.add(iid)
                        found += 1
        except OSError as e:
            print(f"[pixiv-sync] 扫描已有作品失败: {e}")
        if found:
            self._save_ids()
        return found

    # ---------- pixeval 目录导入（第三方客户端兼容） ----------

    # pixeval 文件名：135504321.png / 119079950p0.png / 100276361_p0.jpg / 87737976_p1(1).jpg
    _PIXEVAL_RE = re.compile(
        r"^(\d+)(?:_?p\d+)?(?:\(\d+\))?\.(?:jpe?g|png|gif|webp)$", re.IGNORECASE
    )

    @staticmethod
    def _pixeval_page(name: str) -> Optional[int]:
        m = re.search(r"p(\d+)", name, re.IGNORECASE)
        return int(m.group(1)) if m else None

    def _migrate_pixeval(self) -> int:
        """导入 pixeval 下载目录（<pixeval_dir>/{画师名}/{图片}）到 pixiv/{画师名}/。

        - 文件名规范化：单图 → {id}{ext}；多图 → {id}/{id}_p{页码}{ext}；
        - 与本地重复（id 已在去重集合或 pixiv/ 已有）→ 删除 pixeval 副本（以本地为准）；
        - 无法识别的文件保留原地。返回处理的文件数。
        """
        src_root = (self.setting("pixeval_dir") or "").strip()
        if not src_root or not Path(src_root).is_dir():
            return 0
        src_root = Path(src_root)
        ids = self._load_ids()
        target_root = self._root() / "pixiv"
        moved = deleted = 0

        artist_dirs = sorted(
            d for d in src_root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        for artist_dir in artist_dirs:
            artist_name = self._sanitize(artist_dir.name) or "未分类"
            target_artist = target_root / artist_name
            # 按作品 id 分组
            groups: Dict[int, List[Path]] = {}
            for f in artist_dir.iterdir():
                if not f.is_file() or f.name.startswith("."):
                    continue
                m = self._PIXEVAL_RE.match(f.name)
                if m:
                    groups.setdefault(int(m.group(1)), []).append(f)
            for iid, files in groups.items():
                if iid in ids:
                    # 重复：以本地为准，删除 pixeval 副本
                    for f in files:
                        try:
                            f.unlink()
                            deleted += 1
                        except OSError:
                            pass
                    continue
                # 单图/多图判断：存在页码 >=1 的文件 → 多图
                pages = [p for p in (self._pixeval_page(f.name) for f in files) if p is not None]
                is_multi = any(p >= 1 for p in pages)
                for f in files:
                    ext = f.suffix.lower() or ".jpg"
                    if is_multi:
                        page = self._pixeval_page(f.name)
                        dest = target_artist / str(iid) / f"{iid}_p{page}{ext}"
                    else:
                        dest = target_artist / f"{iid}{ext}"
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if dest.exists():  # 目标已有同名文件（幂等）
                            f.unlink()
                            deleted += 1
                        else:
                            shutil.move(str(f), str(dest))
                            moved += 1
                    except OSError as e:
                        print(f"[pixiv-sync] pixeval 迁移 {f.name} 失败: {e}")
                ids.add(iid)
        if moved or deleted:
            self._save_ids()
        if moved or deleted:
            print(f"[pixiv-sync] pixeval 导入完成: 移动 {moved}，删除重复 {deleted}")
        return moved + deleted

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

    # ---------- 名单/内容 独立控制 ----------

    def _fetch_following(self, task: Optional[Dict[str, Any]] = None) -> List[tuple]:
        """翻页拉取全部关注画师列表 [(user_id, name)]。"""
        client = self._client()
        following: List[tuple] = []
        qs = None
        while not self._cancel_flag:
            try:
                self._rate_limiter.wait()  # 全局限速 3/s
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
            self._persist_task(task)
        return following

    # ---------- 待下载清单（刷新生成，同步按清单下载） ----------

    def _pending_file(self, kind: str) -> Path:
        return self._cache_dir() / f"pending_{kind}.json"

    def _load_pending_data(self, kind: str) -> tuple:
        """读取清单，返回 (items, scan)。scan 为刷新断点进度（dict 或 None）。"""
        try:
            data = json.loads(self._pending_file(kind).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                items = data.get("items", []) if isinstance(data.get("items"), list) else []
                scan = data.get("scan")
                return items, (scan if isinstance(scan, dict) else None)
        except Exception:
            pass
        return [], None

    def _load_pending(self, kind: str) -> List[Dict[str, Any]]:
        items, _ = self._load_pending_data(kind)
        return items

    def _save_pending(self, kind: str, items: List[Dict[str, Any]], scan: Optional[dict] = None):
        try:
            if scan is None:
                _, scan = self._load_pending_data(kind)
            data: Dict[str, Any] = {"saved_at": time.time(), "items": items}
            if scan:
                data["scan"] = scan
            self._cache_dir().mkdir(parents=True, exist_ok=True)
            self._pending_file(kind).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            print(f"[pixiv-sync] 保存待下载清单失败: {e}")

    def refresh_following_lists(self) -> Dict:
        """刷新关注画师作品名单：拉取列表生成待下载清单（不下载）。"""
        return self._start_refresh("following")

    def refresh_bookmarks_lists(self) -> Dict:
        """刷新喜欢画作名单：拉取收藏列表生成待下载清单（不下载）。"""
        return self._start_refresh("bookmarks")

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

    def _run_refresh(self, kind: str):
        task = self._new_task(f"refresh_{kind}")
        task["state"] = "running"
        self._persist_task(task)
        try:
            token = (self.setting("refresh_token") or "").strip()
            if not token:
                raise PixivError("未配置 refresh_token，请先在设置中填写")
            client = self._client()
            client.auth(refresh_token=token)
            ids = self._load_ids()
            if kind == "following":
                items, scan = self._collect_following_pending(client, task, ids)
            else:
                items, scan = self._collect_bookmarks_pending(client, task, ids)
            self._save_pending(kind, items, scan)
            task["state"] = "cancelled" if self._cancel_flag else "done"
            task["total"] = len(items)
            done_cnt = sum(1 for i in items if i.get("done"))
            task["current"] = (
                f"清单 {len(items)} 条（已下 {done_cnt}）· 已扫描"
                + ("完成" if (scan or {}).get("complete") else "部分（可再点刷新继续）")
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
            self._persist_task(task)

    def _collect_following_pending(
        self, client, task: Dict[str, Any], ids: Set[int]
    ) -> tuple:
        """扫描关注画师作品，返回 (items, scan)。画师级断点：scan.done_uids 记录已处理画师。

        每次刷新处理一批画师（受 max_refresh 待下载条数限制），未处理的画师下次继续；
        全部画师处理完 scan.complete=True。清单按旧→新排序（老图优先下载）。
        """
        items, scan = self._load_pending_data("following")
        done_uids = set(scan.get("done_uids", [])) if isinstance(scan, dict) else set()
        if scan and scan.get("complete"):
            done_uids = set()  # 上一轮完整结束，新一轮从头
        following = self._fetch_following(task)
        selected = self._load_selected_artists()
        if selected:
            following = [(u, n) for u, n in following
                         if str(u) in selected or n in selected]
        if not following:
            raise PixivError("关注列表为空或画师名单未匹配到任何画师")

        refresh_limit = self._max_refresh()

        def fetch_artist(uid: int) -> List[Dict[str, Any]]:
            """完整扫描一个画师的全部作品（从最新翻到最旧）。"""
            illusts: List[Dict[str, Any]] = []
            q = None
            while not self._cancel_flag:
                try:
                    self._rate_limiter.wait()  # 全局限速（多线程共享）
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
        window = self._scan_workers()  # 滑动窗口大小（可配置）
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
                if self._cancel_flag:
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
                        self._cancel_flag = True
                        raise
                    except Exception as e:  # noqa: BLE001
                        print(f"[pixiv-sync] 拉取列表失败: {e}")
                        continue
                    # 替换该画师的 items（更新），其他画师保留（断点累积）
                    kept = [i for i in items if (i.get("user") or {}).get("id") != uid]
                    for ill in reversed(illusts):  # 反转：旧作品在前
                        iid = ill.get("id")
                        if iid is not None:
                            item = dict(ill)
                            item["done"] = int(iid) in ids
                            kept.append(item)
                    items = kept
                    done_uids.add(uid)
                    task["current"] = (
                        f"已扫描画师 {len(done_uids)}/{len(following)}，清单 {len(items)} 条"
                    )
                    self._persist_task(task)
                    self._save_pending("following", items, {"done_uids": sorted(done_uids)})
                # 滑动：补充新画师进窗口
                for _ in done:
                    try:
                        uid = next(it)
                    except StopIteration:
                        continue
                    running[pool.submit(fetch_artist, uid)] = uid

        complete = len(done_uids) >= len(following)
        return items, {"done_uids": sorted(done_uids), "complete": complete}

    def _collect_bookmarks_pending(
        self, client, task: Dict[str, Any], ids: Set[int]
    ) -> tuple:
        """扫描收藏列表，返回 (items, scan)。页级断点：scan.offset 记录已翻页位置。

        每次刷新翻到 max_refresh 条待下载后暂停，下次从断点继续；翻完 scan.complete=True。
        """
        items, scan = self._load_pending_data("bookmarks")
        offset = scan.get("offset") if isinstance(scan, dict) else None
        if scan and scan.get("complete"):
            items = []  # 上一轮完整结束，新一轮从头
            offset = None
        refresh_limit = self._max_refresh()
        seen = {i.get("id") for i in items}
        start_pending = sum(1 for i in items if not i.get("done"))
        qs = {"offset": offset} if offset is not None else None

        while not self._cancel_flag:
            try:
                self._rate_limiter.wait()
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
                    item = dict(ill)
                    item["done"] = int(iid) in ids
                    items.append(item)
                    seen.add(iid)
                    # 本批新增待下载达上限 → 保存进度，下次从断点继续
                    if refresh_limit and (sum(1 for i in items if not i.get("done")) - start_pending) >= refresh_limit:
                        cur_offset = qs.get("offset") if qs else 0
                        self._save_pending("bookmarks", items, {"offset": cur_offset, "complete": False})
                        task["current"] = f"清单 {len(items)} 条（部分扫描，可再点刷新继续）"
                        self._persist_task(task)
                        return items, {"offset": cur_offset, "complete": False}
                nxt = page.get("next_url")
                if not nxt:
                    break
                qs = client.parse_qs(nxt)
                self._save_pending("bookmarks", items, {"offset": qs.get("offset"), "complete": False})
            except PixivError as e:
                if "429" in str(e):
                    raise RateLimitError("触发 Pixiv 限流（429），任务已停止，请等待冷却后重试") from None
                raise
        task["current"] = f"清单 {len(items)} 条（扫描完成）"
        self._persist_task(task)
        return items, {"complete": True}

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

    def _rebuild_downloaded(self) -> tuple:
        """扫描本地重建有效文件 id 集合（清理 0 字节文件），返回 (existing_ids, 清理数)。"""
        root = self._root() / "pixiv"
        existing: Set[int] = set()
        zero = 0
        if root.exists():
            for current, dir_names, filenames in os.walk(root):
                dir_names[:] = [d for d in dir_names if not d.startswith(".")]
                for name in filenames:
                    m = self._ID_NAME_RE.match(name)
                    if not m:
                        continue
                    iid = int(m.group(1))
                    p = Path(current) / name
                    try:
                        if p.stat().st_size == 0:
                            p.unlink()
                            zero += 1
                            continue
                    except OSError:
                        continue
                    existing.add(iid)
        return existing, zero

    def refresh_downloaded(self) -> Dict:
        """刷新已下载记录：扫描本地重建 ids（手动删过的移除、手动加入的导入、0 字节清理）。"""
        try:
            existing, zero = self._rebuild_downloaded()
            with self._task_lock:
                self._downloaded_ids = existing
                self._save_ids()
            return {"ok": True, "total": len(existing), "zero_removed": zero}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def verify_downloaded(self) -> Dict:
        """校验已下载内容：移除记录中本地无有效文件的失效 id，下次同步自动重下。"""
        try:
            existing, zero = self._rebuild_downloaded()
            ids = self._load_ids()
            stale = [iid for iid in ids if iid not in existing]
            with self._task_lock:
                for iid in stale:
                    ids.discard(iid)
                self._save_ids()
            return {
                "ok": True,
                "stale_removed": len(stale),
                "zero_removed": zero,
                "total": len(ids),
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def import_pixeval(self) -> Dict:
        """手动触发 pixeval 目录导入（纯本地文件操作，无需登录）。"""
        src = (self.setting("pixeval_dir") or "").strip()
        if not src or not Path(src).is_dir():
            return {"ok": False, "error": "未配置有效的 Pixeval 目录（设置 → Pixeval 目录）"}
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "error": "已有任务在运行"}
            self._cancel_flag = False
            self._thread = threading.Thread(target=self._run_import_pixeval, daemon=True)
            self._thread.start()
        return {"ok": True}

    def _run_import_pixeval(self):
        task = self._new_task("pixeval")
        task["state"] = "running"
        task["current"] = "导入 pixeval 目录…"
        self._persist_task(task)
        try:
            n = self._migrate_pixeval()
            task["total"] = n
            task["done"] = n
            task["state"] = "cancelled" if self._cancel_flag else "done"
        except Exception as e:  # noqa: BLE001
            task["state"] = "failed"
            task["error"] = f"{type(e).__name__}: {e}"
        finally:
            task["finished_at"] = time.time()
            task["current"] = ""
            self._persist_task(task)

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

    # ---------- OAuth 向导：重新获取 refresh_token（PKCE 授权码流程） ----------

    def start_oauth(self) -> Dict:
        """生成 PKCE 挑战并打开 Pixiv 登录页（第一步）。

        返回 challenge 供前端拼控制台脚本：在已登录的 pixiv.net 页面
        控制台执行同源 fetch 拿 code（cookie 自动带上，绕开重定向拦截）。
        """
        try:
            verifier, challenge = self._client().generate_pkce()
            self._oauth_verifier = verifier
            url = self._client().login_url(challenge)
            webbrowser.open(url)
            return {"ok": True, "url": url, "challenge": challenge}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"打开登录页失败: {e}"}

    def finish_oauth(self, code: str) -> Dict:
        """用授权码换取 token 并自动保存 refresh_token（第二步）。"""
        try:
            if not getattr(self, "_oauth_verifier", None):
                return {"ok": False, "error": "请先点击「获取 Token」打开登录页"}
            code = (code or "").strip()
            if not code:
                return {"ok": False, "error": "code 为空，请从浏览器地址栏复制 code 参数值"}
            client = self._client()
            client.auth_with_code(code, self._oauth_verifier)
            self._oauth_verifier = None
            if client.refresh_token:
                self.update_setting("refresh_token", client.refresh_token)
                self._pixiv_client = None  # 新 token，重置客户端
            return {"ok": True, "user_id": client.user_id}
        except PixivError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _run_sync(self, kind: str):
        task = self._new_task(kind)
        self._persist_task(task)
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
            # 旧目录结构迁移（following/ + bookmarks/ → 统一 pixiv/{画师名}/，一次性）
            task["current"] = "检查旧目录结构…"
            self._persist_task(task)
            migrated = self._migrate_legacy_layout()
            if migrated:
                print(f"[pixiv-sync] 旧目录迁移完成，处理 {migrated} 个文件")
            # 识别用户手动放入的旧图（按命名规则提取 id 并入去重集合）
            scanned = self._scan_existing_ids()
            if scanned:
                print(f"[pixiv-sync] 扫描到 {scanned} 个已存在作品，并入去重集合")
            task["state"] = "running"
            self._persist_task(task)

            if kind == "following":
                self._sync_following(task)
            else:
                self._sync_bookmarks(task)

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
            self._persist_task(task)

    def _sync_following(self, task: Dict[str, Any]):
        """同步画师：按「刷新关注名单」生成的清单下载待下载部分（先刷新再同步）。

        每次最多下载 max_download 条；下载成功的在清单中标记 done（保留），
        可重复点同步继续下载剩余。
        """
        ids = self._load_ids()
        self._download_pending(task, ids, "following")

    def _download_pending(self, task: Dict[str, Any], ids: Set[int], kind: str):
        """从清单下载待下载部分（共用逻辑：画师/喜欢）。"""
        items = self._load_pending(kind)
        if not items:
            raise PixivError(
                f"待下载清单为空，请先点「刷新{'关注名单' if kind == 'following' else '喜欢名单'}」"
            )
        # 待下载 = 未标记 done 且不在已下载集合
        todo = [
            i for i in items
            if not i.get("done") and int(i.get("id", -1)) not in ids
        ]
        limit = self._max_download()
        if limit:
            todo = todo[:limit]
        if not todo:
            task["total"] = 0
            task["current"] = "清单中已无待下载作品"
            self._persist_task(task)
            return
        task["total"] = len(todo)
        task["current"] = f"开始下载 {len(todo)} 个作品…"
        self._persist_task(task)

        # 按画师解析目标目录（主线程，含改名迁移/缓存）
        pending: List[tuple] = []
        for ill in todo:
            user = ill.get("user") or {}
            uid = user.get("id")
            if uid:
                sub = self._artist_dir(int(uid), str(user.get("name") or uid))
            else:
                sub = self._root() / "pixiv" / "未分类"
            pending.append((sub, ill))

        workers = self._workers()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._process_illust, illust, task, ids, sub)
                for sub, illust in pending
            ]
            for future in futures:
                try:
                    future.result()
                except Exception as e:  # noqa: BLE001
                    print(f"[pixiv-sync] 下载任务异常: {e}")

        # 标记来源 + 更新清单：下载成功的标记 done（保留在清单，供统计）
        just_done = {int(i["id"]) for i in todo if int(i.get("id", -1)) in ids}
        if just_done:
            self._mark_sources({iid: kind for iid in just_done})
            for item in items:
                if int(item.get("id", -1)) in just_done:
                    item["done"] = True
            self._save_pending(kind, items)

    def _workers(self) -> int:
        try:
            return max(1, min(8, int(self.setting("workers", 4))))
        except (TypeError, ValueError):
            return 4

    def _sync_bookmarks(self, task: Dict[str, Any]):
        """同步喜欢：按「刷新喜欢名单」生成的待下载清单下载。

        与画师同步共用统一去重集合：关注画师的作品若已被画师同步下载，
        这里直接跳过（不重复下载）；非关注画师的作品下载到其画师目录。
        """
        ids = self._load_ids()
        self._download_pending(task, ids, "bookmarks")

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
                # True=新下载；False=文件已存在（视为已下载）；PixivError=失败
                self._client().download(url, path=str(target), name=name)
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
        """返回画师作品目录（以名字命名，统一放在 <root>/pixiv/ 下）。

        - 画师 id → 名字 记入本地缓存，画师改名后仍能识别为同一人；
        - 检测到改名时把旧名字目录迁移合并到新名字目录，避免"分家"。
        - 关注画师与收藏画师共用同一目录：作品归属画师，来源不再区分。
        """
        base = self._root() / "pixiv"
        cache = self._load_artist_cache()
        key = str(uid)
        new_name = self._sanitize(name) or str(uid)
        old_name = cache.get(key)

        # 数字名保护：传入的名字缺失（回退成 uid 数字）时，沿用缓存名，
        # 避免缓存被数字覆盖、目录来回改名的抖动
        if new_name == str(uid) and old_name:
            new_name = old_name

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
        ids = self._load_ids()
        pf_items = self._load_pending("following")
        pb_items = self._load_pending("bookmarks")
        pf_pending = sum(1 for i in pf_items if not i.get("done"))
        pb_pending = sum(1 for i in pb_items if not i.get("done"))
        following_done = len(pf_items) - pf_pending
        bookmarks_done = len(pb_items) - pb_pending
        return {
            "task": task,
            "root_dir": root,
            "token_configured": bool((self.setting("refresh_token") or "").strip()),
            "downloaded_total": len(ids),
            "running": bool(self._thread and self._thread.is_alive()),
            "selected_artists": len(self._load_selected_artists()),
            "selected_file": str(self._selected_file()),
            # 统计：清单全部/待下/已下 + 其他来源
            "pending_following": pf_pending,
            "following_total": len(pf_items),
            "following_done": following_done,
            "pending_bookmarks": pb_pending,
            "bookmarks_total": len(pb_items),
            "bookmarks_done": bookmarks_done,
            "other_done": max(0, len(ids) - following_done - bookmarks_done),
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
            "import_pixeval": self.import_pixeval,
            "cancel_task": self.cancel_task,
            "open_config": self.open_config,
            "start_oauth": self.start_oauth,
            "finish_oauth": self.finish_oauth,
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
        if "rate_limit" in changed_keys:
            try:
                self._rate_limiter.set_rate(float(self.setting("rate_limit", 3)))
            except (TypeError, ValueError):
                pass
