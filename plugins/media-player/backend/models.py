"""媒体播放器统一数据模型：音频 + 视频。"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class MediaItem:
    id: str
    path: str
    kind: str              # audio / video
    title: str
    artist: str
    album: str
    album_key: str
    duration: float = 0
    size: int = 0
    mtime: float = 0
    cover_path: str = ""
    has_cover: bool = False
    album_artist: str = ""
    track: int = 0

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'path': self.path,
            'kind': self.kind,
            'title': self.title,
            'artist': self.artist,
            'album': self.album,
            'album_key': self.album_key,
            'duration': self.duration,
            'size': self.size,
            'mtime': self.mtime,
            'cover_path': self.cover_path,
            'has_cover': self.has_cover,
            'album_artist': self.album_artist,
            'track': self.track,
        }

    @classmethod
    def from_cache(cls, data: dict) -> 'MediaItem':
        return cls(
            id=data.get('id', ''),
            path=data.get('path', ''),
            kind=data.get('kind', 'audio'),
            title=data.get('title', ''),
            artist=data.get('artist', ''),
            album=data.get('album', ''),
            album_key=data.get('album_key', ''),
            duration=data.get('duration', 0),
            size=data.get('size', 0),
            mtime=data.get('mtime', 0),
            cover_path=data.get('cover_path', ''),
            has_cover=data.get('has_cover', False),
            album_artist=data.get('album_artist', ''),
            track=data.get('track', 0),
        )


@dataclass
class MediaAlbum:
    key: str
    name: str
    kind: str
    cover_path: str = ""
    artist: str = ""
    items: List[MediaItem] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def total_duration(self) -> float:
        return sum(item.duration or 0 for item in self.items)

    def to_dict(self) -> dict:
        return {
            'key': self.key,
            'name': self.name,
            'kind': self.kind,
            'cover_path': self.cover_path,
            'artist': self.artist,
            'count': self.count,
            'total_duration': self.total_duration,
        }
