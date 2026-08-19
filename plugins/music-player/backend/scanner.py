import os
import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from shell.backend.plugin_utils import load_sibling



_models = load_sibling(__file__, 'models', 'music_player')
_metadata = load_sibling(__file__, 'metadata', 'music_player')

SongMeta = _models.SongMeta
AlbumMeta = _models.AlbumMeta
MetadataReader = _metadata.MetadataReader
SUPPORTED_EXTS = _metadata.SUPPORTED_EXTS

META_CACHE_VERSION = 3


class MusicScanner:

    def __init__(self, root_dir: Path, cache_dir: Path, cover_dir: Path):
        self.root_dir = root_dir
        self.cache_dir = cache_dir
        self.cover_dir = cover_dir
        self.meta_cache_file = cache_dir / 'music_meta.json'
        self._songs: Dict[str, SongMeta] = {}
        self._albums: Dict[str, AlbumMeta] = {}
        self._loaded = False

    def _load_cache(self):
        if self._loaded:
            return
        self._loaded = True
        if self.meta_cache_file.exists():
            try:
                with open(self.meta_cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                cache_ver = data.get('version', 0)
                if cache_ver != META_CACHE_VERSION:
                    print(f"[MusicScanner] 缓存版本不匹配 ({cache_ver} != {META_CACHE_VERSION}), 重建")
                    self.meta_cache_file.unlink()
                    return
                for sid, s in data.get('songs', {}).items():
                    self._songs[sid] = SongMeta(**s)
                self._rebuild_albums()
                print(f"[MusicScanner] 从缓存加载 {len(self._songs)} 首歌曲")
            except Exception:
                pass

    def _save_cache(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        songs_dict = {sid: s.to_dict() for sid, s in self._songs.items()}
        try:
            with open(self.meta_cache_file, 'w', encoding='utf-8') as f:
                json.dump({'version': META_CACHE_VERSION, 'songs': songs_dict, 'updated': time.time()},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[MusicScanner] 保存缓存失败: {e}")

    def _rebuild_albums(self):
        self._albums.clear()
        for song in self._songs.values():
            key = f"{song.album}||{song.album_artist}"
            if key not in self._albums:
                self._albums[key] = AlbumMeta(
                    key=key,
                    name=song.album,
                    artist=song.album_artist,
                    cover_path=song.cover_path,
                )
            self._albums[key].songs.append(song)
        for album in self._albums.values():
            album.songs.sort(key=lambda s: (s.track, s.title))
            if not album.cover_path and album.songs:
                album.cover_path = album.songs[0].cover_path

    def scan(self, force: bool = False) -> int:
        self._load_cache()
        if not self.root_dir.exists():
            print(f"[MusicScanner] 根目录不存在: {self.root_dir}")
            return 0

        updated = 0
        seen = set()
        for entry in self.root_dir.rglob('*'):
            if not entry.is_file():
                continue
            if entry.name.startswith('.') or '.cache' in entry.parts:
                continue
            if entry.suffix.lower() not in SUPPORTED_EXTS:
                continue

            try:
                rel_path = str(entry.relative_to(self.root_dir).as_posix())
            except ValueError:
                continue

            seen.add(rel_path)
            stat = entry.stat()

            cached = self._songs.get(rel_path)
            if not force and cached and cached.mtime == stat.st_mtime and cached.file_size == stat.st_size:
                continue

            meta = MetadataReader.read(entry)
            print(f"[MusicScanner] {rel_path} → title={meta['title']}, artist={meta['artist']}, album={meta['album']}")
            song = SongMeta(
                id=rel_path,
                title=meta['title'],
                artist=meta['artist'],
                album=meta['album'],
                album_artist=meta['album_artist'],
                track=meta['track'],
                duration=meta['duration'],
                file_path=rel_path,
                file_size=stat.st_size,
                mtime=stat.st_mtime,
            )

            cover_name = MetadataReader.extract_cover(entry, rel_path, self.cover_dir)
            if cover_name:
                song.cover_path = f'.cache/covers/{cover_name}'
                song.has_cover = True

            self._songs[rel_path] = song
            updated += 1

        removed = [sid for sid in self._songs if sid not in seen]
        for sid in removed:
            del self._songs[sid]

        if updated > 0 or removed:
            self._save_cache()
            self._rebuild_albums()

        return updated

    def get_albums(self, sort_by: str = 'name') -> List[dict]:
        self._load_cache()
        albums = list(self._albums.values())
        if sort_by == 'artist':
            albums.sort(key=lambda a: a.artist.lower())
        else:
            albums.sort(key=lambda a: a.name.lower())
        return [a.to_dict() for a in albums]

    def get_album_songs(self, album_key: str) -> List[dict]:
        self._load_cache()
        album = self._albums.get(album_key)
        if not album:
            return []
        return [s.to_dict() for s in album.songs]

    def get_song(self, song_id: str) -> Optional[dict]:
        self._load_cache()
        song = self._songs.get(song_id)
        return song.to_dict() if song else None

    def search(self, keyword: str) -> List[dict]:
        self._load_cache()
        if not keyword:
            return [s.to_dict() for s in self._songs.values()]
        kw = keyword.lower()
        results = []
        for song in self._songs.values():
            if (kw in song.title.lower() or
                kw in song.artist.lower() or
                kw in song.album.lower()):
                results.append(song.to_dict())
        results.sort(key=lambda s: s['title'].lower())
        return results

    @property
    def song_count(self) -> int:
        self._load_cache()
        return len(self._songs)

    @property
    def album_count(self) -> int:
        self._load_cache()
        return len(self._albums)
