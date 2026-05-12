import os
import json
from pathlib import Path
from typing import Dict, List


class MangaModule:
    def __init__(self, manga_dir: str):
        self.manga_dir = Path(manga_dir).resolve()
        self.cover_dir = self.manga_dir / '.cache' / 'covers'
        self.cover_dir.mkdir(parents=True, exist_ok=True)
        self._cache = None

    def _scan_manga(self):
        """扫描本地漫画目录，读取album_info.json"""
        if self._cache is not None:
            return self._cache

        manga_list = []
        if not self.manga_dir.exists():
            self._cache = manga_list
            return manga_list

        for entry in os.scandir(self.manga_dir):
            if not entry.is_dir() or entry.name.startswith('.'):
                continue
            info_path = os.path.join(entry.path, 'album_info.json')
            if os.path.exists(info_path):
                try:
                    with open(info_path, 'r', encoding='utf-8') as f:
                        info = json.load(f)
                    manga_list.append({
                        'comic_id': info.get('album_id', entry.name),
                        'title': info.get('title', entry.name),
                        'author': info.get('author', '未知'),
                        'tags': info.get('tags', []),
                        'page_count': info.get('total_page_count', 0),
                        'chapter_count': info.get('chapter_count', 1),
                        'cover_url': f'/file/manga/{entry.name}/cover.jpg',
                        'folder_name': entry.name
                    })
                except Exception:
                    pass

        self._cache = manga_list
        return manga_list

    def list_manga(self) -> List[Dict]:
        return self._scan_manga()

    def search(self, keyword: str) -> List[Dict]:
        if not keyword:
            return self.list_manga()
        kw = keyword.lower()
        return [
            m for m in self.list_manga()
            if kw in m['title'].lower() or kw in m['author'].lower()
        ]

    def get_filters(self) -> Dict:
        manga = self.list_manga()
        tags = sorted(set(t for m in manga for t in m['tags']))
        authors = sorted(set(m['author'] for m in manga))
        return {"tags": tags, "authors": authors}

    def filter(self, filter_type: str, value: str) -> List[Dict]:
        manga = self.list_manga()
        if filter_type == 'tag':
            return [m for m in manga if value in m['tags']]
        elif filter_type == 'author':
            return [m for m in manga if m['author'] == value]
        return manga