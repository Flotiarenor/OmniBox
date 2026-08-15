import os
import json
import re
from pathlib import Path
from typing import Dict, List
from datetime import datetime

from shell.backend.plugin_base import PluginBase

try:
    from natsort import natsorted
except ImportError:
    _NUM_RE = re.compile(r'\d+')
    def natsorted(seq):
        # 内置自然排序兜底：将数字零填充后按字典序比较，兼容 1/01/00001/10 等命名
        return sorted(seq, key=lambda x: _NUM_RE.sub(lambda m: m.group(0).zfill(16), str(x)))

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}


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
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {"favorites": [], "recent": []}

    def _save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    def _find_cover(self, folder_path: Path) -> str:
        search_dirs = []
        sub_dirs = [d for d in folder_path.iterdir()
                    if d.is_dir() and not d.name.startswith('.') and d.name != 'ai']
        if sub_dirs:
            search_dirs.append(natsorted(sub_dirs)[0])
        search_dirs.append(folder_path)

        for d in search_dirs:
            try:
                images = [f for f in d.iterdir()
                          if f.is_file() and f.suffix.lower() in _IMAGE_EXTS]
            except OSError:
                continue
            if not images:
                continue

            # 显式封面优先
            for f in images:
                if 'cover' in f.stem.lower() or '封面' in f.stem:
                    return f.relative_to(self.manga_dir).as_posix()

            # 纯数字命名的第一页（兼容 00001 / 01 / 1 / 00000 等）
            numeric = [f for f in images if f.stem.isdigit()]
            if numeric:
                return natsorted(numeric)[0].relative_to(self.manga_dir).as_posix()

            # 其余情况取自然序第一张
            return natsorted(images)[0].relative_to(self.manga_dir).as_posix()
        return ""

    def _scan_manga(self):
        if self._cache is not None:
            return self._cache
        manga_list = []
        if not self.manga_dir.exists():
            self._cache = manga_list
            return manga_list

        fav_set = set(self._state['favorites'])

        for entry in os.scandir(self.manga_dir):
            if not entry.is_dir() or entry.name.startswith('.') or entry.name == 'ai':
                continue
            info_path = os.path.join(entry.path, 'album_info.json')
            info = {}
            if os.path.exists(info_path):
                try:
                    with open(info_path, 'r', encoding='utf-8') as f:
                        info = json.load(f)
                except Exception:
                    pass

            cover_url = self._find_cover(Path(entry.path))

            manga_list.append({
                'comic_id': info.get('album_id', entry.name),
                'title': info.get('title', entry.name),
                'author': info.get('author', '未知'),
                'tags': info.get('tags', []),
                'page_count': info.get('total_page_count', 0),
                'cover_url': cover_url,
                'folder_name': entry.name,
                'is_fav': entry.name in fav_set
            })
        self._cache = manga_list
        return manga_list

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
        folder_path = self.manga_dir / folder_name
        if not folder_path.exists():
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
            for d in natsorted(sub_dirs):
                chapters.append({
                    "name": d.name,
                    "cover_url": self._find_cover(d),
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
        base_dir = self.manga_dir / folder_name
        target_dir = base_dir / chapter_path if chapter_path else base_dir

        if not target_dir.exists():
            return []

        image_extensions = ['*.jpg', '*.jpeg', '*.png', '*.webp']
        image_files = []
        all_files = []
        for ext in image_extensions:
            all_files.extend(target_dir.glob(ext))

        for f in natsorted(all_files):
            rel_path = f.relative_to(self.manga_dir)
            image_files.append(rel_path.as_posix())

        return image_files
