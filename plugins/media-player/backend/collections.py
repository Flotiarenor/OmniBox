"""用户可见的集合操作：歌单、喜欢、最近播放、播放参数与队列。

无状态：数据由 `UserStateStore` 持有（`state.data`），这里只实现它们的业务规则并在
写完之后调 `state.save()`；条目解析走 `index`，落盘时机与原子性都归 store。

队列与播放参数为什么分开更新：`save_queue` 存 id 列表、`save_playback` 存播放参数，
一次换曲会先播后存队列。整体替换 `playback` 会让任何一次换曲都把队列字段清掉 ——
恢复播放时队列退化成单条，表现为重载后「下一首/上一首」不跟列表。
"""

import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

#: 「最近播放」在 get_state 里回传的条数（状态文件里仍存 `state.RECENT_LIMIT` 条）。
RECENT_RETURN = 20

#: 「校验确认存在」的条目键集合的提供方：返回 `set[str]`，没有权威结论时返回 `None`。
VerifiedKeysProvider = Callable[[], Optional[Set[str]]]


class MediaCollections:
    """歌单 / 喜欢 / 最近播放 / 播放状态。"""

    def __init__(self, state: Any, index: Any, queue_limit: int,
                 verified_keys_provider: Optional[VerifiedKeysProvider] = None) -> None:
        self._state = state
        self._index = index
        self._queue_limit = queue_limit
        #: 可选的「整趟校验已完成」查询。缺省 None = 永远没有权威结论，
        #: 于是「索引里没有」永远不足以清空用户数据（见 `_playback_item_alive`）。
        self._verified_keys_provider = verified_keys_provider

    @property
    def _data(self) -> Dict[str, Any]:
        return self._state.data

    # ===== 歌单（音视频混编） =====

    def playlist_list(self) -> List[dict]:
        return self._data.get('playlists', [])

    def playlist_get(self, playlist_id: str) -> dict:
        for playlist in self._data.get('playlists', []):
            if playlist['id'] == playlist_id:
                items = []
                for item_id in playlist.get('item_ids', []):
                    item = self._index.get(item_id)
                    if item is not None:
                        items.append(item.to_dict())
                return {**playlist, 'items': items}
        return {}

    def playlist_save(self, name: str = '', playlist_id: str = '',
                      item_ids: Optional[List[str]] = None) -> dict:
        playlists = self._data.get('playlists', [])
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        if playlist_id:
            for playlist in playlists:
                if playlist['id'] == playlist_id:
                    playlist['name'] = name or playlist['name']
                    # 未传 item_ids（如仅重命名）时保留原有曲目，避免误清空歌单
                    if item_ids is not None:
                        playlist['item_ids'] = item_ids
                    playlist['updated_at'] = now
                    self._state.save()
                    return {'success': True, 'playlist': playlist}
            return {'success': False, 'error': '歌单不存在'}
        if item_ids is None:
            item_ids = []
        new_playlist = {
            'id': uuid.uuid4().hex[:12],
            'name': name or '新建歌单',
            'item_ids': item_ids,
            'created_at': now,
            'updated_at': now,
        }
        playlists.append(new_playlist)
        self._data['playlists'] = playlists
        self._state.save()
        return {'success': True, 'playlist': new_playlist}

    def playlist_delete(self, playlist_id: str) -> dict:
        before = len(self._data.get('playlists', []))
        self._data['playlists'] = [playlist for playlist in self._data.get('playlists', [])
                                   if playlist['id'] != playlist_id]
        if len(self._data['playlists']) < before:
            self._state.save()
            return {'success': True}
        return {'success': False, 'error': '歌单不存在'}

    # ===== 喜欢 / 最近 =====

    def toggle_favorite(self, item_id: str) -> dict:
        favs = self._data.setdefault('favorites', [])
        if item_id in favs:
            favs.remove(item_id)
            is_fav = False
        else:
            favs.append(item_id)
            is_fav = True
        self._state.save()
        return {'is_fav': is_fav}

    def add_favorites(self, item_ids: Optional[List[str]] = None) -> dict:
        """批量加入喜欢（幂等）。网易云「喜欢」导入用它一次写盘。

        只接受索引中存在的 id：`get_state` 是按索引解析收藏的，写入索引外的 id
        会变成看不见也删不掉的死条目。已存在的 id 不计入 added（重复导入不重复计数）。
        """
        favs = self._data.setdefault('favorites', [])
        added = []
        for raw in item_ids or []:
            item_id = str(raw or '')
            if (item_id and item_id in self._index.items
                    and item_id not in favs and item_id not in added):
                added.append(item_id)
        if added:
            favs.extend(added)
            self._state.save()
        return {'success': True, 'added': len(added)}

    def update_recent(self, item_id: str) -> dict:
        recent = [entry for entry in self._data.setdefault('recent', [])
                  if entry.get('id') != item_id]
        recent.insert(0, {'id': item_id, 'played_at': time.strftime('%Y-%m-%d %H:%M:%S')})
        self._data['recent'] = recent[:self._state.recent_limit]
        self._state.save()
        return {'success': True}

    # ===== 播放参数与队列 =====

    def save_playback(self, item_id: str = '', loop_mode: str = 'none', shuffle: bool = False,
                      volume: float = 1.0, video_mode: str = 'video',
                      queue_index: Optional[int] = None) -> dict:
        # 就地更新而不是整体替换：队列 id 列表（save_queue）与播放参数分开更新，
        # 换曲时整条队列不应被覆盖掉。
        playback = self._data.setdefault('playback', {})
        playback.update({
            'item_id': item_id,
            'loop_mode': loop_mode,
            'shuffle': shuffle,
            'volume': volume,
            'video_mode': video_mode,
        })
        if queue_index is not None:
            playback['queue_index'] = max(0, int(queue_index))
        self._data['playback'] = playback
        self._state.save()
        return {'success': True}

    def save_queue(self, item_ids: Optional[List[str]] = None) -> dict:
        """保存当前播放队列的条目 id 列表（按播放顺序），供重启后恢复整条队列。

        只存 id 不存条目正文：状态文件因此与媒体库大小无关。超过 `queue_limit` 的队列
        只保留前若干条，并置 truncated 标记（恢复时退化为可恢复的部分）。
        """
        ids = [str(item_id) for item_id in (item_ids or []) if item_id]
        playback = self._data.setdefault('playback', {})
        playback['queue_ids'] = ids[:self._queue_limit]
        playback['queue_truncated'] = len(ids) > self._queue_limit
        playback['queue_index'] = min(max(0, int(playback.get('queue_index', 0))),
                                      max(0, len(playback['queue_ids']) - 1))
        self._data['playback'] = playback
        self._state.save()
        return {'success': True, 'count': len(playback['queue_ids']),
                'truncated': playback['queue_truncated']}

    def get_items(self, item_ids: Optional[List[str]] = None) -> List[Dict]:
        """按传入顺序批量取条目（队列恢复用），已不存在的 id 直接跳过。"""
        ids = [str(item_id) for item_id in (item_ids or []) if item_id][:self._queue_limit]
        items = []
        for item_id in ids:
            item = self._index.get(item_id)
            if item is not None:
                items.append(item.to_dict())
        return items

    def get_playback(self) -> dict:
        """播放参数；里面记的条目**确认**消失时才清空并落盘。

        判据不是「索引里有没有」：索引在启动早期可能还是空的、外接盘可能没挂载、
        首趟扫描可能只跑了一部分（预算用尽）。这些情形下条目不在索引里不代表文件
        没了，因此只有统一刷新基建给出**整趟校验成功**的结论（`verified_keys()` 非
        `None`）且条目不在其中时，才清空并落盘；拿不到结论时保留。
        """
        playback = self._data.get('playback', {})
        item_id = playback.get('item_id', '')
        if item_id and not self._playback_item_alive(item_id):
            self._data['playback'] = {}
            self._state.save()
            return {}
        return playback

    def _playback_item_alive(self, item_id: str) -> bool:
        """播放条目是否仍在：索引命中即活；索引未命中时只信校验结论。

        保守方向是「保留」：没有结论（provider 缺省、返回 None、抛异常）一律视为
        无法判定，不动用户数据。
        """
        if self._index.get(item_id) is not None:
            return True
        if self._verified_keys_provider is None:
            return True
        try:
            keys = self._verified_keys_provider()
        except Exception:
            return True
        if keys is None:
            return True
        return item_id in keys

    # ===== 收藏 / 最近的可解析投影 =====

    def get_state(self) -> dict:
        favorites = []
        for item_id in self._data.get('favorites', []):
            item = self._index.get(item_id)
            if item is not None:
                data = item.to_dict()
                data['is_fav'] = True
                favorites.append(data)

        recent = []
        for entry in self._data.get('recent', [])[:RECENT_RETURN]:
            item = self._index.get(entry.get('id', ''))
            if item is not None:
                data = item.to_dict()
                data['played_at'] = entry.get('played_at', '')
                recent.append(data)

        return {'favorites': favorites, 'recent': recent}
