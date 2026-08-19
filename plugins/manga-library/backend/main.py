import os
import json
from pathlib import Path
from typing import Dict, List
from datetime import datetime

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling



_scanner = load_sibling(__file__, 'scanner', 'manga_library')
find_cover = _scanner.find_cover
list_pages = _scanner.list_pages
natural_sorted = _scanner.natural_sorted
resolve_safe_path = _scanner.resolve_safe_path
scan_manga = _scanner.scan_manga

class MangaLibraryPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "漫画根目录", "type": "text",
         "placeholder": "默认: ./data", "help": "存放漫画文件夹的根目录"},
        {"key": "recent_count", "label": "最近阅读显示数量", "type": "number",
         "default": 10, "min": 1, "max": 50, "help": "首页「最近阅读」展示的漫画数量"},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir') or str(super().get_data_root())
        self.recent_count = int(self.setting('recent_count', 10))
        self.manga_dir = Path(root).resolve()
        self.cover_dir = self.manga_dir / '.cache' / 'covers'
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.manga_dir / '.cache' / 'manga_state.json'
        self._cache = None
        self._state = self._load_state()

    # ===== 文件服务根目录 =====

    def get_data_root(self) -> Path:
        return self.manga_dir

    # ===== 设置持久化 =====



    def get_settings(self) -> Dict:
        return {
            "root_dir": str(self.manga_dir),
            "recent_count": self.recent_count,
        }

    def on_settings_changed(self, changed_keys):
        if 'root_dir' in changed_keys:
            new_dir = self.setting('root_dir')
            if new_dir and Path(new_dir).is_dir():
                self._apply_root_dir(Path(new_dir).resolve())
        if 'recent_count' in changed_keys:     
            count = self.setting('recent_count', 10)  
            try:
                if count == None:
                    print("[MangaLibrary] recent_count is None")
                    raise ValueError
                self.recent_count = max(1, min(50, int(count)))
            except (ValueError, TypeError):
                pass

    def _apply_root_dir(self, new_dir: Path):
        self.manga_dir = new_dir
        self.cover_dir = self.manga_dir / '.cache' / 'covers'
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.manga_dir / '.cache' / 'manga_state.json'
        self._state = self._load_state()
        self._cache = None

    # ===== API 注册 =====

    def register_api(self) -> dict:
        return {
            'manga_list': self.list_manga,
            'manga_search': self.search,
            'manga_get_state': self.get_state,
            'manga_toggle_favorite': self.toggle_favorite,
            'manga_update_recent': self.update_recent,
            'manga_get_detail': self.get_detail,
            'manga_get_pages': self.get_pages,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    # ===== 核心业务（由旧版 MangaModule 迁移） =====

    def _load_state(self) -> Dict:
        defaults = {"favorites": [], "recent": []}
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
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

    def _save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    def _scan_manga(self):
        if self._cache is None:
            self._cache = scan_manga(self.manga_dir, self._state['favorites'])
        return self._cache


    def list_manga(self) -> List[Dict]:
        self._cache = None
        return self._scan_manga()

    def search(self, keyword: str) -> List[Dict]:
        if not keyword:
            return self.list_manga()
        kw = keyword.lower()
        return [m for m in self.list_manga() if kw in m['title'].lower() or kw in m['author'].lower()]

    def get_state(self) -> Dict:
        recent_ids = [r['id'] for r in self._state['recent']]
        fav_ids = self._state['favorites']
        all_manga = {m['folder_name']: m for m in self.list_manga()}
        return {
            "recent": [all_manga[rid] for rid in recent_ids if rid in all_manga][:self.recent_count],
            "favorites": [all_manga[fid] for fid in fav_ids if fid in all_manga]
        }

    def toggle_favorite(self, folder_name: str) -> bool:
        favs = self._state['favorites']
        if folder_name in favs:
            favs.remove(folder_name)
            is_fav = False
        else:
            favs.append(folder_name)
            is_fav = True
        self._save_state()
        self._cache = None
        return is_fav

    def update_recent(self, folder_name: str, page: int = 0) -> Dict:
        recent = self._state['recent']
        recent = [r for r in recent if r['id'] != folder_name]
        recent.insert(0, {"id": folder_name, "page": page, "time": datetime.now().isoformat()})
        self._state['recent'] = recent[:20]
        self._save_state()
        return {"status": "ok"}

    def get_detail(self, folder_name: str) -> Dict:
        folder_path = resolve_safe_path(self.manga_dir, folder_name)
        if folder_path is None or not folder_path.exists():
            return {}

        info_path = folder_path / 'album_info.json'
        info = {}
        if info_path.exists():
            try:
                with open(info_path, 'r', encoding='utf-8') as f:
                    info = json.load(f)
            except Exception:
                pass

        sub_dirs = [d for d in folder_path.iterdir()
                    if d.is_dir() and not d.name.startswith('.') and d.name != 'ai']
        is_multi_chapter = len(sub_dirs) > 0

        chapters = []
        if is_multi_chapter:
            for d in natural_sorted(sub_dirs):
                chapters.append({
                    "name": d.name,
                    "cover_url": find_cover(d, self.manga_dir),
                    "path": d.name
                })

        return {
            "folder_name": folder_name,
            "title": info.get("title", folder_name),
            "author": info.get("author", "未知"),
            "is_fav": folder_name in self._state['favorites'],
            "is_multi_chapter": is_multi_chapter,
            "info": info,
            "chapters": chapters
        }

    def get_pages(self, folder_name: str, chapter_path: str = "") -> List[str]:
        return list_pages(self.manga_dir, folder_name, chapter_path)
