"""ncm-cli 输出的数据结构与解析。

为什么单独成组件：解析只认 ncm-cli 的 JSON 形状（`songs` / `playlists` / `albums` 一层层
嵌套、字段有 camelCase 与 snake_case 两套），与"命令怎么执行""结果怎么缓存"没有关系；
拆开后判据只剩一个——输出形状变了，改这里。

无状态：解析器不持有任何缓存与连接，构造一次可复用；`Song` / `Playlist` / `Album` 是
解析的产出类型，跟着解析走（入口按构造参数把解析器注入 `NeteaseMusicAPI`）。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class Song:
    id: str = ''
    original_id: str = ''
    name: str = ''
    artists: List[str] = field(default_factory=list)
    album: str = ''
    duration: int = 0
    visible: bool = True
    tags: List[str] = field(default_factory=list)
    cover_url: Optional[str] = None

    @property
    def url(self) -> str:
        return f"https://music.163.com/#/song?id={self.original_id}"


@dataclass
class Playlist:
    id: str = ''
    original_id: str = ''
    name: str = ''
    track_count: int = 0
    play_count: int = 0
    cover_url: Optional[str] = None

    @property
    def url(self) -> str:
        return f"https://music.163.com/#/playlist?id={self.original_id}"


@dataclass
class Album:
    id: str = ''
    original_id: str = ''
    name: str = ''
    artist: str = ''
    track_count: int = 0

    @property
    def url(self) -> str:
        return f"https://music.163.com/#/album?id={self.original_id}"


class OutputParser:
    """把 ncm-cli 的 stdout 归一成 Song / Playlist / Album 列表或 JSON 值。"""

    def json(self, output: str) -> Any:
        """尽力解析 JSON：去掉 ANSI 颜色、容忍前后混入的非 JSON 文本。"""
        if not output:
            return None
        text = output.strip()
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        try:
            return json.loads(text)
        except Exception:
            pass
        for start_ch, end_ch in (('[', ']'), ('{', '}')):
            start = text.find(start_ch)
            end = text.rfind(end_ch)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except Exception:
                    continue
        return None

    def extract_list(self, data, keys, _depth: int = 0):
        """从嵌套结构里找出条目列表（ncm-cli 各命令的层级不一致）。"""
        if isinstance(data, list):
            return data
        if not isinstance(data, dict) or _depth > 5:
            return None
        for key in [*list(keys), "records", "tracks", "songs", "playlists", "albums"]:
            value = data.get(key)
            if isinstance(value, list):
                return value
        for key in [*list(keys), "data", "result"]:
            value = data.get(key)
            if isinstance(value, dict):
                found = self.extract_list(value, keys, _depth + 1)
                if found:
                    return found
        return None

    def songs(self, output: str) -> List[Song]:
        data = self.json(output)
        items = self.extract_list(data, ["songs", "data", "result"])
        songs: List[Song] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            original_id = str(item.get("originalId") or item.get("original_id") or item.get("id") or "")
            if not original_id:
                continue
            artists_raw = item.get("artists") or item.get("artist") or []
            if isinstance(artists_raw, str):
                artists = [artists_raw]
            elif isinstance(artists_raw, list):
                artists = [a.get("name") if isinstance(a, dict) else str(a) for a in artists_raw if a]
            else:
                artists = []
            album_raw = item.get("album") or item.get("albumName") or ""
            album = album_raw.get("name") or "" if isinstance(album_raw, dict) else str(album_raw or "")
            songs.append(Song(
                id=str(item.get("id") or original_id),
                original_id=original_id,
                name=str(item.get("name") or "未知"),
                artists=[str(a) for a in artists],
                album=album,
                duration=int(item.get("duration") or item.get("dt") or 0),
                visible=bool(item.get("visible", True)),
                tags=item.get("tags") or [],
                cover_url=item.get("coverImgUrl") or item.get("cover_url"),
            ))
        return songs

    def playlists(self, output: str) -> List[Playlist]:
        data = self.json(output)
        items = self.extract_list(data, ["playlists", "data", "result"])
        playlists: List[Playlist] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "")
            original_id = str(item.get("originalId") or item.get("original_id") or pid)
            if not pid:
                continue
            playlists.append(Playlist(
                id=pid,
                original_id=original_id,
                name=str(item.get("name") or "未知"),
                track_count=int(item.get("trackCount") or item.get("track_count") or 0),
                play_count=int(item.get("playCount") or item.get("play_count") or 0),
                cover_url=item.get("coverImgUrl") or item.get("cover_url"),
            ))
        return playlists

    def albums(self, output: str) -> List[Album]:
        data = self.json(output)
        items = self.extract_list(data, ["albums", "data", "result"])
        albums: List[Album] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            aid = str(item.get("id") or "")
            original_id = str(item.get("originalId") or item.get("original_id") or aid)
            if not aid:
                continue
            artist_raw = item.get("artist") or item.get("artists") or ""
            if isinstance(artist_raw, list):
                artist = ", ".join(str(a.get("name") if isinstance(a, dict) else a) for a in artist_raw if a)
            else:
                artist = str(artist_raw or "")
            albums.append(Album(
                id=aid,
                original_id=original_id,
                name=str(item.get("name") or "未知"),
                artist=artist,
                track_count=int(item.get("trackCount") or item.get("track_count") or 0),
            ))
        return albums

    def single_playlist(self, output: str) -> Optional[Playlist]:
        data = self.json(output)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return self.playlist_from_dict(data["data"])
        playlists = self.playlists(output)
        return playlists[0] if playlists else None

    def playlist_from_dict(self, item: dict) -> Optional[Playlist]:
        if not isinstance(item, dict):
            return None
        pid = str(item.get("id") or "")
        if not pid:
            return None
        original_id = str(item.get("originalId") or item.get("original_id") or pid)
        return Playlist(
            id=pid,
            original_id=original_id,
            name=str(item.get("name") or "未知"),
            track_count=int(item.get("trackCount") or item.get("track_count") or 0),
            play_count=int(item.get("playCount") or item.get("play_count") or 0),
            cover_url=item.get("coverImgUrl") or item.get("cover_url"),
        )

    def data_of(self, output: str) -> Any:
        """取 `data` 子树（命令返回 `{data: {...}}` 时用）；没有该子树时返回原文的 JSON 值。"""
        data = self.json(output)
        if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
            return data["data"]
        return data
