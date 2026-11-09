"""书架、收藏与最近阅读：顶层漫画目录聚合成书架，并维护 `manga_state.json`。

职责边界：

* 书架缓存与失效（「只在磁盘确实变了时重建」）；
* 收藏 / 最近阅读的读写与合法性检查；
* 详情与页面列表（目录扫描规则由入口注入的 `scanner` 模块提供）。

本模块不 import 兄弟模块（分层门禁 R3），也不碰下载中心 —— 两者只在数据根上
重合，没有共享状态。
"""

import json
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from shell.backend.json_cache import write_json

log = logging.getLogger(__name__)


class Library:
    """书架视图与阅读状态的唯一持有者。

    状态自持：书架缓存与脏标记、收藏/最近阅读列表、状态文件的读写都在这里。
    换根由入口先 `PathLayout.rebind()`，再调本类的 `rebind()` 重读状态。
    """

    def __init__(self, paths: Any, scanner: Any, recent_count: int,
                 recent_limit: int, recent_min: int, recent_max: int) -> None:
        self._paths = paths
        self._scanner = scanner
        self.recent_count = recent_count
        self._recent_limit = recent_limit
        self._recent_min = recent_min
        self._recent_max = recent_max
        #: 状态文件的读写串行化：桥接调用各起一条线程，打开 / 关闭阅读器与切换收藏
        #: 会同时走到 `_save_state`（见它的 docstring）。
        self._lock = threading.RLock()
        # 书架列表缓存：由统一刷新基建驱动失效（见 freshness 组件）。
        self._shelf: Optional[List[Dict]] = None
        self._shelf_dirty = True
        self._state: Dict[str, List] = self._load_state()

    # ===== 阅读状态（收藏 / 最近阅读）=====

    def _load_state(self) -> Dict:
        """读 `manga_state.json`；缺失或损坏时退回空收藏与空最近阅读。

        与 `_save_state` 共用一把锁：写者用 `os.replace` 换名，读者在改名那一瞬间开着
        目标文件时，Windows 上会让写者拿到 WinError 32。
        """
        with self._lock:
            return self._read_state_locked()

    def _read_state_locked(self) -> Dict:
        """已持锁的读实现（`_load_state` 的唯一调用方）。"""
        defaults: Dict[str, List] = {"favorites": [], "recent": []}
        state_file = self._paths.state_file
        if state_file.exists():
            try:
                with open(state_file, 'r', encoding='utf-8') as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    favorites = data.get("favorites", [])
                    recent = data.get("recent", [])
                    return {
                        "favorites": favorites if isinstance(favorites, list) else [],
                        "recent": recent if isinstance(recent, list) else [],
                    }
            except Exception:
                pass
        return defaults

    def _save_state(self) -> None:
        """原子写阅读状态（收藏 / 最近阅读）。

        用 `shell.backend.json_cache.write_json`（同目录临时文件 + flush + fsync +
        os.replace，与媒体播放器的状态文件同一份机制）：`open('w')` 直接覆写时，写到一半
        被强杀会留下截断的 JSON，下次启动 `_load_state` 退回空表 —— 收藏与最近阅读整体
        丢失。写入还串行化在实例锁上：打开 / 关闭阅读器与切换收藏由不同桥接线程发起。
        """
        with self._lock:
            state_file = self._paths.state_file
            if write_json(state_file, self._state, indent=2, create_parent=True):
                return
            log.error(f'[MangaLibrary] 保存阅读状态失败: {state_file}')

    def rebind(self) -> None:
        """换根后重读阅读状态并作废书架缓存（由入口编排）。"""
        self._state = self._load_state()
        self.reset()

    def set_recent_count(self, count: Any) -> None:
        """夹取「最近阅读」展示数量；非数字时保留旧值（设置项被手改成脏值）。"""
        try:
            self.recent_count = max(self._recent_min,
                                    min(self._recent_max, int(count)))
        except (TypeError, ValueError):
            return

    # ===== 书架缓存 =====

    def reset(self) -> None:
        """书架缓存作废（磁盘变化、收藏变更、换根时调用）。"""
        self._shelf = None
        self._shelf_dirty = True

    def rescan(self) -> List[Dict]:
        """书架列表（真正的缓存：只在"磁盘确实变了"时重建）。

        失效由统一刷新基建驱动 —— 目录级短路判定"没变"时连 `derive` 都不会被调用，
        所以切视图、切分栏、`manga_get_state` + `manga_list` 各调一次，全都只读内存。
        """
        shelf = self._shelf
        if shelf is None or self._shelf_dirty:
            shelf = self._scanner.scan_manga(self._paths.root, self._state['favorites'])
            self._shelf = shelf
            self._shelf_dirty = False
        return shelf

    def list_manga(self) -> List[Dict]:
        return self.rescan()

    def search(self, keyword: str) -> List[Dict]:
        if not keyword:
            return self.list_manga()
        kw = keyword.lower()
        return [m for m in self.list_manga()
                if kw in m['title'].lower() or kw in m['author'].lower()]

    def get_state(self) -> Dict:
        """书架三视图的投影：收藏与最近阅读。

        `recent` 的每一项在漫画字段之外带 `page`（上次阅读到第几页，从 0 开始），
        供阅读器续读；`favorites` 的每一项与 `manga_list` 同形，不带该字段。
        """
        favorites = self._state['favorites']
        all_manga = {m['folder_name']: m for m in self.list_manga()}
        recent: List[Dict] = []
        for entry in self._state['recent']:
            manga = all_manga.get(entry['id'])
            if manga is None:
                continue
            recent.append({**manga, 'page': entry.get('page', 0)})
        return {
            "recent": recent[:self.recent_count],
            "favorites": [all_manga[fid] for fid in favorites if fid in all_manga],
        }

    def retain(self, valid_folders) -> List[str]:
        """全量校验后的对账：书架只保留磁盘上还有图片的漫画。

        返回被丢弃的目录名（空列表表示没有变化）；调用方据此汇报"清理了几部"。
        """
        shelf = self.rescan()
        stale = [m['folder_name'] for m in shelf
                 if m['folder_name'] not in valid_folders]
        if stale:
            log.info(f'[MangaLibrary] 校验清理已消失的漫画 {len(stale)} 部')
            self._shelf = [m for m in shelf if m['folder_name'] in valid_folders]
            self._shelf_dirty = False
        return stale

    # ===== 收藏与最近阅读 =====

    def toggle_favorite(self, folder_name: str) -> bool:
        """切换收藏。

        `folder_name` 会被写进 `manga_state.json`，因此必须是**根内的单个目录名**：
        它不参与路径拼接（危害限于脏数据），但放进来 `../` 或绝对路径只会让
        "最近阅读/收藏"里出现永远匹配不到书架的条目。统一在这里挡掉。
        """
        if not self._is_valid_folder_name(folder_name):
            return False
        favs = self._state['favorites']
        if folder_name in favs:
            favs.remove(folder_name)
            is_fav = False
        else:
            favs.append(folder_name)
            is_fav = True
        self._save_state()
        # 书架卡片带 is_fav：收藏变了必须重建列表（缓存由基建之外的因素失效）
        self.reset()
        return is_fav

    def _is_valid_folder_name(self, folder_name: str) -> bool:
        """合法的漫画目录名：非空、不含路径分隔符、不是 `.` / `..`。"""
        name = str(folder_name or '').strip()
        if not name or name in ('.', '..'):
            return False
        return not any(sep in name for sep in ('/', '\\'))

    def update_recent(self, folder_name: str, page: int = 0) -> Dict:
        if not self._is_valid_folder_name(folder_name):
            return {"status": "error", "error": "非法目录名"}
        try:
            page = max(0, int(page))
        except (TypeError, ValueError):
            page = 0
        recent = self._state['recent']
        recent = [r for r in recent if r['id'] != folder_name]
        recent.insert(0, {"id": folder_name, "page": page,
                          "time": datetime.now().isoformat()})
        self._state['recent'] = recent[:self._recent_limit]
        self._save_state()
        return {"status": "ok"}

    # ===== 详情与页面 =====

    def get_detail(self, folder_name: str) -> Dict:
        """详情：每次调用都按当前磁盘内容重算（读 `album_info.json`、列章节），不缓存。"""
        folder_path = self._scanner.resolve_safe_path(self._paths.root, folder_name)
        if folder_path is None or not folder_path.exists():
            return {}

        info_path = folder_path / 'album_info.json'
        info = {}
        if info_path.exists():
            try:
                with open(info_path, 'r', encoding='utf-8') as handle:
                    info = json.load(handle)
            except Exception:
                pass

        sub_dirs = self._scanner.visible_subdirs(folder_path)
        is_multi_chapter = len(sub_dirs) > 0

        chapters = []
        if is_multi_chapter:
            for directory in self._scanner.natural_sorted(sub_dirs):
                chapters.append({
                    "name": directory.name,
                    "cover_url": self._scanner.find_cover(directory, self._paths.root),
                    "path": directory.name,
                })

        return {
            "folder_name": folder_name,
            "title": info.get("title", folder_name),
            "author": info.get("author", "未知"),
            "is_fav": folder_name in self._state['favorites'],
            "is_multi_chapter": is_multi_chapter,
            "info": info,
            "chapters": chapters,
        }

    def get_pages(self, folder_name: str, chapter_path: str = "") -> List[str]:
        return self._scanner.list_pages(self._paths.root, folder_name, chapter_path)
