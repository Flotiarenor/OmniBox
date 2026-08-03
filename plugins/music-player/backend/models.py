from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SongMeta:
    id: str
    title: str
    artist: str
    album: str
    album_artist: str
    track: int
    duration: int
    file_path: str
    file_size: int
    mtime: float
    cover_path: str = ""
    has_cover: bool = False

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'title': self.title,
            'artist': self.artist,
            'album': self.album,
            'album_artist': self.album_artist,
            'track': self.track,
            'duration': self.duration,
            'file_path': self.file_path,
            'file_size': self.file_size,
            'mtime': self.mtime,
            'cover_path': self.cover_path,
            'has_cover': self.has_cover,
        }


@dataclass
class AlbumMeta:
    key: str
    name: str
    artist: str
    songs: List[SongMeta] = field(default_factory=list)
    cover_path: str = ""

    @property
    def song_count(self) -> int:
        return len(self.songs)

    @property
    def total_duration(self) -> int:
        return sum(s.duration for s in self.songs)

    def to_dict(self) -> dict:
        return {
            'key': self.key,
            'name': self.name,
            'artist': self.artist,
            'cover_path': self.cover_path,
            'song_count': self.song_count,
            'total_duration': self.total_duration,
        }


@dataclass
class PlaylistData:
    id: str
    name: str
    song_ids: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'name': self.name,
            'song_ids': self.song_ids,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }
