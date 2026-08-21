"""媒体播放器插件主类：统一音频/视频媒体库。"""

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

_scanner = load_sibling(__file__, 'scanner', 'media_player')
_models = load_sibling(__file__, 'models', 'media_player')

scan_media = _scanner.scan_media
scan_directories = _scanner.scan_directories
scan_media = _scanner.scan_media
scan_directories = _scanner.scan_directories
INDEX_VERSION = _scanner.INDEX_VERSION
MediaItem = _models.MediaItem
MediaAlbum = _models.MediaAlbum


class MediaPlayerPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "媒体库根目录", "type": "text",
         "placeholder": "默认: ./data", "central": True,
         "help": "主媒体库根目录"},
        {"key": "media_dirs", "label": "额外媒体目录", "type": "textarea",
         "placeholder": "每行一个目录", "central": True,
         "help": "每行填写一个媒体目录，与主目录一起扫描"},
        {"key": "lyrics_enabled", "label": "启用歌词显示", "type": "checkbox",
         "default": True, "central": False, "help": "关闭后不显示歌词入口"},
        {"key": "lyrics_font_size", "label": "歌词字号", "type": "range",
         "default": 16, "min": 12, "max": 40, "central": False, "help": "未激活行的字号"},
        {"key": "lyrics_active_size", "label": "当前行字号", "type": "range",
         "default": 24, "min": 16, "max": 52, "central": False, "help": "当前播放行的字号"},
        {"key": "lyrics_line_height", "label": "行高倍率", "type": "range",
         "default": 1.6, "min": 1.2, "max": 3.0, "step": 0.1, "central": False,
         "help": "行间距倍率"},
        {"key": "lyrics_glow", "label": "文字发光效果", "type": "checkbox",
         "default": True, "central": False, "help": "当前行文字发光"},
        {"key": "lyrics_align", "label": "歌词对齐", "type": "select",
         "options": [{"label": "居中", "value": "center"}, {"label": "左对齐", "value": "left"}],
         "default": "center", "central": False, "help": "歌词文本对齐方式"},
        {"key": "lyrics_bg_color", "label": "歌词背景色", "type": "text",
         "placeholder": "如 #1a1a2e 留空为默认", "central": False,
         "help": "纯色背景，支持 #RRGGBB 格式"},
        {"key": "lyrics_bg_image", "label": "歌词背景图路径", "type": "text",
          "placeholder": "如 /files/bg.jpg 留空使用纯色", "central": False,
          "help": "图片路径，相对于媒体库根目录"},
        {"key": "lyrics_font_color", "label": "歌词字体颜色", "type": "text",
          "default": "#ffffff", "placeholder": "#ffffff", "central": False,
          "help": "歌词文字颜色，支持 #RRGGBB 格式"},
        {"key": "lyrics_bg_blur", "label": "背景模糊度", "type": "range",
          "default": 8, "min": 0, "max": 50, "central": False, "help": "背景模糊效果强度"},
        {"key": "lyrics_bg_brightness", "label": "背景明亮度", "type": "range",
          "default": 0.25, "min": 0.05, "max": 1.0, "step": 0.05, "central": False,
          "help": "背景亮度调节"},
        {"key": "auto_hide_enabled", "label": "全屏自动隐藏控件", "type": "checkbox",
         "default": True, "central": False, "help": "进入全屏后鼠标静止自动隐藏控件"},
        {"key": "auto_hide_delay", "label": "自动隐藏延迟（秒）", "type": "range",
         "default": 3, "min": 0, "max": 10, "step": 1, "central": False},
        {"key": "default_video_mode", "label": "视频默认播放模式", "type": "select",
         "options": [{"label": "画面模式", "value": "video"}, {"label": "仅声音", "value": "audio"}],
         "default": "video", "central": False, "help": "视频默认以画面或仅声音播放"},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir') or str(super().get_data_root())
        self.root_dir = Path(root).resolve()
        self._cache_dir = self.root_dir / '.cache'
        self._cache_file = self._cache_dir / 'media_index.json'
        self._state_file = self._cache_dir / 'media_state.json'
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._items: Dict[str, MediaItem] = {}
        self._state = self._load_state()
        self._load_index()
        self._legacy_migrated = False
        self._migrate_legacy_state()

    def get_data_root(self) -> Path:
        return self.root_dir

    def get_file_roots(self) -> List[Path]:
        """跨主目录与额外媒体目录提供文件访问。"""
        roots = []
        for raw in self._media_dirs():
            try:
                roots.append(Path(raw).resolve())
            except Exception:
                pass
        return roots or [self.root_dir]

    def _media_dirs(self) -> List[str]:
        dirs = [str(self.root_dir)]
        extra = self.setting('media_dirs') or ''
        for line in str(extra).splitlines():
            line = line.strip()
            if line:
                try:
                    path = Path(line).expanduser().resolve()
                    dirs.append(str(path))
                except Exception:
                    pass
        return dirs

    def _reload_index(self):
        self._load_index()
        self._state = self._load_state()

    # ---------- 索引缓存 ----------

    def _load_index(self):
        if self._cache_file.exists():
            try:
                with open(self._cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('version') != INDEX_VERSION:
                    # 索引结构升级（如新增曲目号/专辑艺术家字段）时自动重建
                    self._items = {}
                    return
                for raw in data.get('items', []):
                    item = MediaItem.from_cache(raw)
                    self._items[item.id] = item
            except Exception:
                self._items = {}

    def _save_index(self, result: Dict):
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {'version': INDEX_VERSION, **result}
        with open(self._cache_file, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _load_state(self) -> dict:
        defaults = {"favorites": [], "recent": [], "playlists": [], "playback": {}}
        if self._state_file.exists():
            try:
                with open(self._state_file, 'r', encoding='utf-8') as f:
                    return {**defaults, **json.load(f)}
            except Exception:
                pass
        return defaults

    def _save_state(self):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[MediaPlayer] 保存状态失败: {e}")

    def _migrate_legacy_state(self):
        """把旧 music-player 的收藏 / 最近播放 / 歌单 / 播放状态迁入统一状态文件。

        旧插件使用「相对路径」作为歌曲 id，新插件使用绝对路径的 md5 摘要，
        这里在索引可用时做一次映射，成功后把旧文件改名保留。
        """
        if self._legacy_migrated:
            return
        self._legacy_migrated = True
        legacy_file = self._cache_dir / 'music_state.json'
        if not legacy_file.exists():
            return
        try:
            legacy = json.loads(legacy_file.read_text(encoding='utf-8'))
        except Exception:
            return
        if not isinstance(legacy, dict):
            return

        if not self._items:
            # 索引尚未建立：保留旧文件，等待首次扫描后再迁移
            self._legacy_migrated = False
            return

        mapping = {}
        raw_ids = set()
        raw_ids.update(str(x) for x in legacy.get('favorites', []) if x)
        raw_ids.update(str(x.get('id', '')) for x in legacy.get('recent', []) if x.get('id'))
        raw_ids.update(str(x) for pl in legacy.get('playlists', []) for x in pl.get('song_ids', []))
        if legacy.get('playback', {}).get('song_id'):
            raw_ids.add(str(legacy['playback']['song_id']))

        for raw_id in raw_ids:
            try:
                legacy_path = Path(raw_id)
                if not legacy_path.is_absolute():
                    legacy_path = self._cache_dir.parent / legacy_path
                new_id = hashlib.md5(str(legacy_path.resolve()).encode('utf-8')).hexdigest()[:16]
                if new_id in self._items:
                    mapping[raw_id] = new_id
            except Exception:
                pass

        favorites = [mapping[x] for x in legacy.get('favorites', []) if str(x) in mapping]
        playlists = []
        for pl in legacy.get('playlists', []):
            item_ids = [mapping[x] for x in pl.get('song_ids', []) if str(x) in mapping]
            playlists.append({
                'id': str(pl.get('id') or uuid.uuid4().hex[:12]),
                'name': pl.get('name') or '迁移歌单',
                'item_ids': item_ids,
                'created_at': pl.get('created_at', ''),
                'updated_at': pl.get('updated_at', ''),
            })
        playback = {}
        old_pb = legacy.get('playback', {})
        old_song = old_pb.get('song_id')
        if old_song and str(old_song) in mapping:
            playback = {
                'item_id': mapping[str(old_song)],
                'loop_mode': old_pb.get('loop_mode', 'none'),
                'shuffle': bool(old_pb.get('shuffle', False)),
                'volume': old_pb.get('volume', 1.0),
            }

        if favorites:
            self._state['favorites'] = list(dict.fromkeys(self._state.get('favorites', []) + favorites))

        legacy_recent = [
            {'id': mapping[entry.get('id')], 'played_at': entry.get('played_at', '')}
            for entry in legacy.get('recent', []) if str(entry.get('id', '')) in mapping
        ]
        if legacy_recent:
            merged_recent = {entry.get('id'): entry for entry in legacy_recent}
            for entry in self._state.get('recent', []):
                merged_recent.setdefault(entry.get('id'), entry)
            self._state['recent'] = sorted(
                merged_recent.values(),
                key=lambda x: x.get('played_at', ''),
                reverse=True,
            )[:50]

        if playlists and not self._state.get('playlists'):
            self._state['playlists'] = playlists
        if playback and not self._state.get('playback'):
            self._state['playback'] = playback
        self._save_state()

        try:
            legacy_file.replace(legacy_file.with_name('music_state.json.migrated'))
            print(f"[MediaPlayer] 已迁移旧 music-player 状态 → {self._state_file.name}")
        except Exception:
            pass

    def _get_item(self, item_id: str) -> Optional[MediaItem]:
        return self._items.get(item_id)

    # ---------- 扫描与浏览 ----------

    def scan(self, force: bool = False) -> Dict:
        cache = {} if force else self._items
        result = scan_directories(self._media_dirs(), cache)
        self._items = {item.id: item for item in (MediaItem.from_cache(x) for x in result['items'])}
        self._save_index(result)
        self._migrate_legacy_state()
        audio = sum(1 for item in self._items.values() if item.kind == 'audio')
        video = sum(1 for item in self._items.values() if item.kind == 'video')
        return {'success': True, 'audio': audio, 'video': video, 'total': len(self._items)}

    def search(self, keyword: str = '') -> List[Dict]:
        kw = (keyword or '').strip().lower()
        items = self._items.values()
        if kw:
            items = [i for i in items if kw in f'{i.title} {i.artist} {i.album}'.lower()]
        return [i.to_dict() for i in sorted(items, key=lambda x: x.title.lower())]

    def recent(self, limit: int = 50) -> List[Dict]:
        items = sorted(self._items.values(), key=lambda x: x.mtime, reverse=True)
        return [i.to_dict() for i in items[:max(1, min(200, int(limit)))]]

    def all_audio(self) -> List[Dict]:
        return [i.to_dict() for i in self._items.values() if i.kind == 'audio']

    def all_video(self) -> List[Dict]:
        return [i.to_dict() for i in self._items.values() if i.kind == 'video']

    def _albums(self, kind: str) -> List[Dict]:
        albums: Dict[str, MediaAlbum] = {}
        for item in self._items.values():
            if item.kind != kind:
                continue
            album = albums.get(item.album_key)
            if album is None:
                album = MediaAlbum(
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
                album.items.sort(key=lambda x: (x.track or 0, x.title.lower()))
            else:
                album.items.sort(key=lambda x: x.title.lower())
            if not album.cover_path and album.items:
                album.cover_path = next((i.cover_path for i in album.items if i.cover_path), '')
            data = album.to_dict()
            data['items'] = [i.to_dict() for i in album.items]
            result.append(data)
        result.sort(key=lambda x: x['name'].lower())
        return result

    def audio_albums(self) -> List[Dict]:
        return self._albums('audio')

    def video_albums(self) -> List[Dict]:
        return self._albums('video')

    def stats(self) -> Dict:
        fav_count = len(self._state.get('favorites', []))
        audio = [i for i in self._items.values() if i.kind == 'audio']
        video = [i for i in self._items.values() if i.kind == 'video']
        return {
            'audio': len(audio),
            'video': len(video),
            'total': len(self._items),
            'audio_albums': len(self._albums('audio')),
            'video_albums': len(self._albums('video')),
            'favorites': fav_count,
            'playlists': len(self._state.get('playlists', [])),
        }

    def album_items(self, album_key: str, kind: str = '') -> List[Dict]:
        items = [i for i in self._items.values() if i.album_key == album_key]
        if kind:
            items = [i for i in items if i.kind == kind]
        if '//album::' in album_key:
            items.sort(key=lambda x: (x.track or 0, x.title.lower()))
        else:
            items.sort(key=lambda x: x.title.lower())
        return [i.to_dict() for i in items]

    def get_item(self, item_id: str) -> Dict:
        item = self._get_item(item_id)
        return item.to_dict() if item else {}

    # ---------- 歌单（音视频混编） ----------

    def playlist_list(self) -> List[dict]:
        return self._state.get('playlists', [])

    def playlist_get(self, playlist_id: str) -> dict:
        for pl in self._state.get('playlists', []):
            if pl['id'] == playlist_id:
                items = [self._get_item(sid).to_dict() for sid in pl.get('item_ids', []) if self._get_item(sid)]
                return {**pl, 'items': items}
        return {}

    def playlist_save(self, name: str = '', playlist_id: str = '', item_ids: List[str] = None) -> dict:
        if item_ids is None:
            item_ids = []
        playlists = self._state.get('playlists', [])
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        if playlist_id:
            for pl in playlists:
                if pl['id'] == playlist_id:
                    pl['name'] = name or pl['name']
                    pl['item_ids'] = item_ids
                    pl['updated_at'] = now
                    self._save_state()
                    return {'success': True, 'playlist': pl}
            return {'success': False, 'error': '歌单不存在'}
        new_pl = {
            'id': uuid.uuid4().hex[:12],
            'name': name or '新建歌单',
            'item_ids': item_ids,
            'created_at': now,
            'updated_at': now,
        }
        playlists.append(new_pl)
        self._state['playlists'] = playlists
        self._save_state()
        return {'success': True, 'playlist': new_pl}

    def playlist_delete(self, playlist_id: str) -> dict:
        before = len(self._state.get('playlists', []))
        self._state['playlists'] = [pl for pl in self._state.get('playlists', []) if pl['id'] != playlist_id]
        if len(self._state['playlists']) < before:
            self._save_state()
            return {'success': True}
        return {'success': False, 'error': '歌单不存在'}

    # ---------- 喜欢 / 最近 / 播放状态 ----------

    def toggle_favorite(self, item_id: str) -> dict:
        favs = self._state.setdefault('favorites', [])
        if item_id in favs:
            favs.remove(item_id)
            is_fav = False
        else:
            favs.append(item_id)
            is_fav = True
        self._save_state()
        return {'is_fav': is_fav}

    def update_recent(self, item_id: str) -> dict:
        recent = self._state.setdefault('recent', [])
        recent = [r for r in recent if r.get('id') != item_id]
        recent.insert(0, {'id': item_id, 'played_at': time.strftime('%Y-%m-%d %H:%M:%S')})
        self._state['recent'] = recent[:50]
        self._save_state()
        return {'success': True}

    def save_playback(self, item_id: str = '', loop_mode: str = 'none', shuffle: bool = False, volume: float = 1.0) -> dict:
        self._state['playback'] = {
            'item_id': item_id,
            'loop_mode': loop_mode,
            'shuffle': shuffle,
            'volume': volume,
        }
        self._save_state()
        return {'success': True}

    def get_playback(self) -> dict:
        pb = self._state.get('playback', {})
        item_id = pb.get('item_id', '')
        if item_id and self._get_item(item_id) is None:
            self._state['playback'] = {}
            self._save_state()
            return {}
        return pb

    def get_state(self) -> dict:
        fav_ids = self._state.get('favorites', [])
        recent_entries = self._state.get('recent', [])[:20]

        favorites = []
        for sid in fav_ids:
            item = self._get_item(sid)
            if item:
                data = item.to_dict()
                data['is_fav'] = True
                favorites.append(data)

        recent = []
        for entry in recent_entries:
            item = self._get_item(entry.get('id', ''))
            if item:
                data = item.to_dict()
                data['played_at'] = entry.get('played_at', '')
                recent.append(data)

        return {'favorites': favorites, 'recent': recent}

    # ---------- 设置 ----------

    def on_settings_changed(self, changed_keys):
        if 'root_dir' in changed_keys or 'media_dirs' in changed_keys:
            new_dir = self.setting('root_dir')
            if new_dir and Path(new_dir).is_dir():
                self.root_dir = Path(new_dir).resolve()
                self._cache_dir = self.root_dir / '.cache'
                self._cache_file = self._cache_dir / 'media_index.json'
                self._state_file = self._cache_dir / 'media_state.json'
                self._cache_dir.mkdir(parents=True, exist_ok=True)
                self._items = {}
                self._reload_index()

    # ---------- 歌词 / 调试 / EQ ----------

    def get_lyrics(self, item_id: str) -> dict:
        item = self._get_item(item_id)
        if not item:
            return {'lyrics': '', 'source': 'none'}
        file_path = Path(item.path)
        lrc_path = file_path.with_suffix('.lrc')
        if not lrc_path.exists():
            return {'lyrics': '', 'source': 'none'}
        for enc in ('utf-8', 'gbk', 'gb2312'):
            try:
                return {'lyrics': lrc_path.read_text(encoding=enc), 'source': 'lrc'}
            except (UnicodeDecodeError, UnicodeError):
                continue
            except Exception:
                break
        return {'lyrics': '', 'source': 'none'}

    def debug_meta(self, item_id: str = '') -> dict:
        if not item_id:
            return {'error': '请提供 item_id 参数'}
        item = self._get_item(item_id)
        if not item:
            return {'error': f'媒体不存在: {item_id}'}
        file_path = Path(item.path)
        if not file_path.exists():
            return {'error': f'文件不存在: {file_path}'}
        try:
            from shell.backend.plugin_utils import load_sibling
            _metadata = load_sibling(__file__, 'metadata', 'media_player')
            MetadataReader = _metadata.MetadataReader
        except Exception:
            return {'error': 'metadata 模块不可用'}
        result = MetadataReader.debug_meta(file_path)
        result['parsed'] = item.to_dict()
        return result

    def _eq_presets_dir(self) -> Path:
        return Path(__file__).parent.parent / 'eq-presets'

    def list_eq_presets(self) -> list:
        presets_dir = self._eq_presets_dir()
        if not presets_dir.exists():
            return []
        presets = []
        for f in sorted(presets_dir.glob('*.json')):
            try:
                data = json.loads(f.read_text(encoding='utf-8'))
                data['id'] = f.stem
                presets.append(data)
            except Exception:
                pass
        return presets

    def save_eq_preset(self, name: str = '', bands: list = None) -> dict:
        if not name or not bands:
            return {'success': False, 'error': '缺少参数'}
        presets_dir = self._eq_presets_dir()
        presets_dir.mkdir(parents=True, exist_ok=True)
        safe_name = ''.join(c for c in name if c.isalnum() or c in '_- ')[:40].strip().replace(' ', '_')
        if not safe_name:
            safe_name = 'custom'
        file_path = presets_dir / f'{safe_name}.json'
        file_path.write_text(json.dumps({'name': name, 'bands': bands}, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'success': True, 'id': safe_name, 'name': name, 'bands': bands}

    def register_api(self) -> dict:
        return {
            'media_scan': self.scan,
            'media_search': self.search,
            'media_recent': self.recent,
            'media_stats': self.stats,
            'media_audio_albums': self.audio_albums,
            'media_all_audio': self.all_audio,
            'media_video_albums': self.video_albums,
            'media_all_video': self.all_video,
            'media_album_items': self.album_items,
            'media_get_item': self.get_item,
            'media_playlist_list': self.playlist_list,
            'media_playlist_get': self.playlist_get,
            'media_playlist_save': self.playlist_save,
            'media_playlist_delete': self.playlist_delete,
            'media_toggle_favorite': self.toggle_favorite,
            'media_update_recent': self.update_recent,
            'media_get_state': self.get_state,
            'media_save_playback': self.save_playback,
            'media_get_playback': self.get_playback,
            'media_get_lyrics': self.get_lyrics,
            'media_debug_meta': self.debug_meta,
            'media_list_eq_presets': self.list_eq_presets,
            'media_save_eq_preset': self.save_eq_preset,
            'media_get_config': lambda key, default=None: self.setting(key, default),
            'media_set_config': lambda key, value: {'success': self.update_setting(key, value)},
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }
