"""浏览查询：搜索、最近、全部音视频、专辑聚合与统计。

无状态：条目来自 `index.items`，收藏 / 歌单计数来自 `state.data`，这里只做投影与聚合，
不持有缓存，也不改任何被读的数据。专辑聚合刻意**不把条目正文塞进响应**：前端只看
count / 时长 / 封面，大媒体库下每次专辑视图都传全库 JSON 会显著拖慢页面与桥接。
"""

from typing import Any, Dict, List

#: 「最近」接口的取数上限（前端只显示一屏，多余的传输没有意义）。
RECENT_MAX = 200


class MediaLibrary:
    """条目与专辑的只读投影。"""

    def __init__(self, index: Any, state: Any, models: Any) -> None:
        self._index = index
        self._state = state
        self._models = models

    # ===== 条目 =====

    def search(self, keyword: str = '') -> List[Dict]:
        """在标题 / 艺术家 / 专辑上做不区分大小写的包含匹配，按标题排序。"""
        kw = (keyword or '').strip().lower()
        items = list(self._index.items.values())
        if kw:
            items = [item for item in items
                     if kw in f'{item.title} {item.artist} {item.album}'.lower()]
        return [item.to_dict() for item in sorted(items, key=lambda item: item.title.lower())]

    def recent(self, limit: int = 50) -> List[Dict]:
        items = sorted(self._index.items.values(), key=lambda item: item.mtime, reverse=True)
        return [item.to_dict() for item in items[:max(1, min(RECENT_MAX, int(limit)))]]

    def all_audio(self) -> List[Dict]:
        return [item.to_dict() for item in self._index.items.values() if item.kind == 'audio']

    def all_video(self) -> List[Dict]:
        return [item.to_dict() for item in self._index.items.values() if item.kind == 'video']

    def get_item(self, item_id: str) -> Dict:
        item = self._index.get(item_id)
        return item.to_dict() if item is not None else {}

    # ===== 专辑 =====

    def audio_albums(self) -> List[Dict]:
        return self._albums('audio')

    def video_albums(self) -> List[Dict]:
        return self._albums('video')

    def _albums(self, kind: str) -> List[Dict]:
        albums: Dict[str, Any] = {}
        for item in self._index.items.values():
            if item.kind != kind:
                continue
            album = albums.get(item.album_key)
            if album is None:
                album = self._models.MediaAlbum(
                    key=item.album_key,
                    name=item.album,
                    kind=kind,
                    cover_path=item.cover_path,
                    artist=item.album_artist or item.artist,
                )
                albums[item.album_key] = album
            album.items.append(item)

        result = []
        for album in albums.values():
            # 音频按曲目号排序，视频按标题排序
            if kind == 'audio':
                album.items.sort(key=lambda item: (item.track or 0, item.title.lower()))
            else:
                album.items.sort(key=lambda item: item.title.lower())
            data = album.to_dict()
            # 封面懒生成：前端用首个有封面条目的 item id 请求 /thumbs/<id>
            data['cover_item_id'] = next(
                (item.id for item in album.items if item.has_cover), '')
            result.append(data)
        result.sort(key=lambda item: item['name'].lower())
        return result

    def album_items(self, album_key: str, kind: str = '') -> List[Dict]:
        items = [item for item in self._index.items.values() if item.album_key == album_key]
        if kind:
            items = [item for item in items if item.kind == kind]
        if '//album::' in album_key:
            items.sort(key=lambda item: (item.track or 0, item.title.lower()))
        else:
            items.sort(key=lambda item: item.title.lower())
        return [item.to_dict() for item in items]

    # ===== 统计 =====

    def stats(self) -> Dict:
        state = self._state.data
        audio = [item for item in self._index.items.values() if item.kind == 'audio']
        video = [item for item in self._index.items.values() if item.kind == 'video']
        return {
            'audio': len(audio),
            'video': len(video),
            'total': len(self._index.items),
            # 仅按 album_key 去重计数，避免为统计构建完整专辑结构（大库下明显更省）
            'audio_albums': len({item.album_key for item in audio}),
            'video_albums': len({item.album_key for item in video}),
            'favorites': len(state.get('favorites', [])),
            'playlists': len(state.get('playlists', [])),
        }
