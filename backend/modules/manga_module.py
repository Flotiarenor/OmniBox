import os
import json
from pathlib import Path
from typing import Dict, List
from datetime import datetime

try:
    from natsort import natsorted
except ImportError:
    natsorted = sorted

class MangaModule:
    def __init__(self, manga_dir: str):
        self.manga_dir = Path(manga_dir).resolve()
        self.cover_dir = self.manga_dir / '.cache' / 'covers'
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.manga_dir / '.cache' / 'manga_state.json'
        self._cache = None
        self._state = self._load_state()

    def _load_state(self) -> Dict:
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception: pass
        return {"favorites": [], "recent": []}

    def _save_state(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)

    def _find_cover(self, folder_path: Path) -> str:
        search_dirs = []
        sub_dirs = [d for d in folder_path.iterdir() 
                    if d.is_dir() and not d.name.startswith('.') and d.name != 'ai']
        if sub_dirs: search_dirs.append(natsorted(sub_dirs)[0])
        search_dirs.append(folder_path)
        
        for d in search_dirs:
            for prefix in ['00001', 'cover', '01']:
                for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                    cover_file = d / f"{prefix}{ext}"
                    if cover_file.exists():
                        rel_path = cover_file.relative_to(self.manga_dir)
                        return f"/file/manga/{rel_path.as_posix()}"
        return ""

    def _scan_manga(self):
        if self._cache is not None: return self._cache
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
                    with open(info_path, 'r', encoding='utf-8') as f: info = json.load(f)
                except Exception: pass
            
            cover_url = self._find_cover(Path(entry.path))
            
            manga_list.append({
                'comic_id': info.get('album_id', entry.name),
                'title': info.get('title', entry.name),
                'author': info.get('author', '未知'),
                'tags': info.get('tags', []),
                'page_count': info.get('total_page_count', 0),
                'cover_url': cover_url,
                'folder_name': entry.name,
                'is_fav': entry.name in fav_set  # 注入收藏状态
            })
        self._cache = manga_list
        return manga_list

    def list_manga(self) -> List[Dict]: 
        self._cache = None # 强制刷新以获取最新收藏状态
        return self._scan_manga()

    def search(self, keyword: str) -> List[Dict]:
        if not keyword: return self.list_manga()
        kw = keyword.lower()
        return [m for m in self.list_manga() if kw in m['title'].lower() or kw in m['author'].lower()]

    def get_state(self) -> Dict:
        recent_ids = [r['id'] for r in self._state['recent']]
        fav_ids = self._state['favorites']
        all_manga = {m['folder_name']: m for m in self.list_manga()}
        return {
            "recent": [all_manga[rid] for rid in recent_ids if rid in all_manga],
            "favorites": [all_manga[fid] for fid in fav_ids if fid in all_manga]
        }

    def toggle_favorite(self, folder_name: str) -> bool:
        favs = self._state['favorites']
        if folder_name in favs:
            favs.remove(folder_name); is_fav = False
        else:
            favs.append(folder_name); is_fav = True
        self._save_state()
        self._cache = None # 清缓存
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
        if not folder_path.exists(): return {}
        
        info_path = folder_path / 'album_info.json'
        info = {}
        if info_path.exists():
            try:
                with open(info_path, 'r', encoding='utf-8') as f: info = json.load(f)
            except: pass

        # 判断是否为多章节
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
            "info": info,  # 返回完整 JSON 供前端展示
            "chapters": chapters
        }

    def get_pages(self, folder_name: str, chapter_path: str = "") -> List[str]:
        base_dir = self.manga_dir / folder_name
        target_dir = base_dir / chapter_path if chapter_path else base_dir
        
        if not target_dir.exists(): return []

        image_extensions = ['*.jpg', '*.jpeg', '*.png', '*.webp']
        image_files = []
        all_files = []
        for ext in image_extensions:
            all_files.extend(target_dir.glob(ext))
            
        for f in natsorted(all_files):
            rel_path = f.relative_to(self.manga_dir)
            image_files.append(f"/file/manga/{rel_path.as_posix()}")
            
        return image_files