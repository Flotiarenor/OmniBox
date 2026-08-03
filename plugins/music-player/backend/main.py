import json
import time
import uuid
import importlib.util
from pathlib import Path
from typing import Dict, List

from shell.backend.plugin_base import PluginBase


def _load_sibling(name):
    path = Path(__file__).parent / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'music_player_{name}', str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_scanner_mod = _load_sibling('scanner')
_metadata_mod = _load_sibling('metadata')
MusicScanner = _scanner_mod.MusicScanner
MetadataReader = _metadata_mod.MetadataReader


class MusicPlayerPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "音乐库根目录", "type": "text",
         "placeholder": "默认: ./data",
         "help": "存放音乐文件的根目录"},
        {"key": "lyrics_enabled", "label": "启用歌词显示", "type": "checkbox",
         "default": True, "help": "关闭后不显示歌词入口"},
        {"key": "lyrics_font_size", "label": "歌词字号", "type": "range",
         "default": 16, "min": 12, "max": 40, "help": "未激活行的字号"},
        {"key": "lyrics_active_size", "label": "当前行字号", "type": "range",
         "default": 24, "min": 16, "max": 52, "help": "当前播放行的字号"},
        {"key": "lyrics_line_height", "label": "行高倍率", "type": "range",
         "default": 1.6, "min": 1.2, "max": 3.0, "step": 0.1,
         "help": "行间距倍率"},
        {"key": "lyrics_glow", "label": "文字发光效果", "type": "checkbox",
         "default": True, "help": "当前行文字发光"},
        {"key": "lyrics_align", "label": "歌词对齐", "type": "select",
         "options": [{"label": "居中", "value": "center"}, {"label": "左对齐", "value": "left"}],
         "default": "center", "help": "歌词文本对齐方式"},
        {"key": "lyrics_bg_color", "label": "歌词背景色", "type": "text",
         "placeholder": "如 #1a1a2e 留空为默认",
         "help": "纯色背景，支持 #RRGGBB 格式"},
        {"key": "lyrics_bg_image", "label": "歌词背景图路径", "type": "text",
         "placeholder": "如 /files/bg.jpg 留空使用纯色",
         "help": "图片路径，相对于音乐库根目录"},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self._migrate_old_settings()
        self._config_default_root = Path(super().get_data_root()).resolve()
        self._applied_root = None
        self._init_with_root(str(self._config_default_root))

    def _init_with_root(self, root_dir: str):
        self.music_dir = Path(root_dir).resolve()
        self._cache_dir = self.music_dir / '.cache'
        self._cover_dir = self._cache_dir / 'covers'
        self._state_file = self._cache_dir / 'music_state.json'
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.scanner = MusicScanner(self.music_dir, self._cache_dir, self._cover_dir)
        self._state = self._load_state()
        self._scanned = False
        self._applied_root = str(self.music_dir)

    def _ensure_ready(self):
        saved_root = self._resolve_root_dir()
        if saved_root != self._applied_root:
            print(f"[MusicPlayer] root_dir 变更: {self._applied_root} → {saved_root}")
            self._init_with_root(saved_root)

    def _resolve_root_dir(self) -> str:
        settings = super().get_settings()
        root = settings.get('root_dir')
        if root and Path(root).is_dir():
            return str(Path(root).resolve())
        return str(self._config_default_root)

    def _migrate_old_settings(self):
        old_file = Path(__file__).parent.parent / 'settings.json'
        if not old_file.exists():
            return
        try:
            with open(old_file, 'r', encoding='utf-8') as f:
                old = json.load(f)
            old_root = old.get('root_dir')
            if old_root and self._settings_store:
                current = self._settings_store.get(self.name) or {}
                if 'root_dir' not in current:
                    self._settings_store.set(self.name, {**current, 'root_dir': old_root})
            old_file.unlink()
        except Exception:
            pass

    def get_data_root(self) -> Path:
        self._ensure_ready()
        return self.music_dir

    def _ensure_scanned(self):
        self._ensure_ready()
        if not self._scanned:
            self.scanner.scan()
            self._scanned = True

    def _load_state(self) -> dict:
        if self._state_file.exists():
            try:
                with open(self._state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {"favorites": [], "recent": [], "playlists": []}

    def _save_state(self):
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[MusicPlayer] 保存状态失败: {e}")

    # ===== Settings =====

    def get_settings(self) -> Dict:
        settings = super().get_settings()
        return {k: v for k, v in settings.items() if v is not None}

    def save_settings(self, settings: Dict) -> Dict:
        result = super().save_settings(settings)
        if result.get('success') and settings.get('root_dir'):
            new_dir = Path(settings['root_dir']).resolve()
            if new_dir.is_dir() and str(new_dir) != self._applied_root:
                self._init_with_root(str(new_dir))
        return result

    # ===== API =====

    def register_api(self) -> dict:
        return {
            'music_scan': self.scan,
            'music_albums': self.list_albums,
            'music_album_songs': self.list_album_songs,
            'music_search': self.search,
            'music_get_song': self.get_song,
            'music_stats': self.stats,
            'music_debug_meta': self.debug_meta,
            'music_playlist_list': self.playlist_list,
            'music_playlist_get': self.playlist_get,
            'music_playlist_save': self.playlist_save,
            'music_playlist_delete': self.playlist_delete,
            'music_toggle_favorite': self.toggle_favorite,
            'music_update_recent': self.update_recent,
            'music_get_state': self.get_state,
            'music_get_lyrics': self.get_lyrics,
            'music_list_eq_presets': self.list_eq_presets,
            'music_save_eq_preset': self.save_eq_preset,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    def scan(self, force: bool = False) -> dict:
        self._ensure_ready()
        print(f"[MusicPlayer] 开始扫描, force={force}, root_dir={self.music_dir}")
        updated = self.scanner.scan(force=force)
        self._scanned = True
        print(f"[MusicPlayer] 扫描完成: updated={updated}, songs={self.scanner.song_count}, albums={self.scanner.album_count}")
        return {
            'success': True,
            'updated': updated,
            'total_songs': self.scanner.song_count,
            'total_albums': self.scanner.album_count,
        }

    def list_albums(self, sort_by: str = 'name') -> List[dict]:
        self._ensure_scanned()
        fav_set = set(self._state.get('favorites', []))
        albums = self.scanner.get_albums(sort_by)
        print(f"[MusicPlayer] list_albums: {len(albums)} albums")
        for album in albums:
            album['is_fav'] = any(s['id'] in fav_set for s in self.scanner.get_album_songs(album['key']))
        return albums

    def list_album_songs(self, album_key: str) -> List[dict]:
        self._ensure_scanned()
        fav_set = set(self._state.get('favorites', []))
        songs = self.scanner.get_album_songs(album_key)
        for s in songs:
            s['is_fav'] = s['id'] in fav_set
        return songs

    def search(self, keyword: str = '') -> List[dict]:
        self._ensure_scanned()
        fav_set = set(self._state.get('favorites', []))
        results = self.scanner.search(keyword)
        for s in results:
            s['is_fav'] = s['id'] in fav_set
        return results

    def get_song(self, song_id: str) -> dict:
        self._ensure_scanned()
        song = self.scanner.get_song(song_id)
        if song:
            song['is_fav'] = song['id'] in self._state.get('favorites', [])
        return song or {}

    def stats(self) -> dict:
        self._ensure_scanned()
        return {
            'total_songs': self.scanner.song_count,
            'total_albums': self.scanner.album_count,
        }

    # ===== Playlists =====

    def playlist_list(self) -> List[dict]:
        return self._state.get('playlists', [])

    def playlist_get(self, playlist_id: str) -> dict:
        for pl in self._state.get('playlists', []):
            if pl['id'] == playlist_id:
                return pl
        return {}

    def playlist_save(self, name: str = '', playlist_id: str = '', song_ids: List[str] = None) -> dict:
        if song_ids is None:
            song_ids = []
        playlists = self._state.get('playlists', [])
        now = time.strftime('%Y-%m-%d %H:%M:%S')

        if playlist_id:
            for pl in playlists:
                if pl['id'] == playlist_id:
                    pl['name'] = name or pl['name']
                    pl['song_ids'] = song_ids
                    pl['updated_at'] = now
                    self._save_state()
                    return {'success': True, 'playlist': pl}
            return {'success': False, 'error': '歌单不存在'}

        new_pl = {
            'id': uuid.uuid4().hex[:12],
            'name': name or '新建歌单',
            'song_ids': song_ids,
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

    # ===== Favorites & Recent =====

    def toggle_favorite(self, song_id: str) -> dict:
        favs = self._state.setdefault('favorites', [])
        if song_id in favs:
            favs.remove(song_id)
            is_fav = False
        else:
            favs.append(song_id)
            is_fav = True
        self._save_state()
        return {'is_fav': is_fav}

    def update_recent(self, song_id: str) -> dict:
        recent = self._state.setdefault('recent', [])
        recent = [r for r in recent if r.get('id') != song_id]
        recent.insert(0, {'id': song_id, 'played_at': time.strftime('%Y-%m-%d %H:%M:%S')})
        self._state['recent'] = recent[:50]
        self._save_state()
        return {'success': True}

    def get_state(self) -> dict:
        self._ensure_scanned()
        fav_ids = self._state.get('favorites', [])
        recent_entries = self._state.get('recent', [])[:20]

        favorites = []
        for sid in fav_ids:
            song = self.scanner.get_song(sid)
            if song:
                song['is_fav'] = True
                favorites.append(song)

        recent = []
        for entry in recent_entries:
            sid = entry.get('id', '')
            song = self.scanner.get_song(sid)
            if song:
                song['played_at'] = entry.get('played_at', '')
                recent.append(song)

        return {
            'favorites': favorites,
            'recent': recent,
        }

    # ===== Lyrics =====

    def get_lyrics(self, song_id: str) -> dict:
        self._ensure_scanned()
        song = self.scanner.get_song(song_id)
        if not song:
            return {'lyrics': '', 'source': 'none'}

        file_path = self.music_dir / song['file_path']

        lrc_path = file_path.with_suffix('.lrc')
        if lrc_path.exists():
            try:
                return {'lyrics': lrc_path.read_text(encoding='utf-8'), 'source': 'lrc'}
            except Exception:
                pass

        for enc in ['utf-8', 'gbk', 'gb2312']:
            try:
                if lrc_path.exists():
                    return {'lyrics': lrc_path.read_text(encoding=enc), 'source': 'lrc'}
            except (UnicodeDecodeError, UnicodeError):
                continue
            except Exception:
                break

        return {'lyrics': '', 'source': 'none'}

    def debug_meta(self, song_id: str = '') -> dict:
        self._ensure_scanned()
        if not song_id:
            return {'error': '请提供 song_id 参数'}
        song = self.scanner.get_song(song_id)
        if not song:
            return {'error': f'歌曲不存在: {song_id}'}
        file_path = self.music_dir / song['file_path']
        if not file_path.exists():
            return {'error': f'文件不存在: {file_path}'}
        result = MetadataReader.debug_meta(file_path)
        result['parsed'] = {
            'title': song.get('title'),
            'artist': song.get('artist'),
            'album': song.get('album'),
            'album_artist': song.get('album_artist'),
            'track': song.get('track'),
            'duration': song.get('duration'),
        }
        result['scanned'] = MetadataReader.read(file_path)
        return result

    # ===== EQ Presets =====

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
