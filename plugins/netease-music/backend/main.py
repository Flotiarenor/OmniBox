"""网易云音乐 Companion 插件后端。"""

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

_api = load_sibling(__file__, 'netease_music_api', 'netease_music')
NeteaseMusicAPI = _api.NeteaseMusicAPI


class NeteaseMusicPlugin(PluginBase):
    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self._api = None

    def _get_api(self) -> NeteaseMusicAPI:
        if self._api is None:
            self._api = NeteaseMusicAPI(check_install=False)
        return self._api

    def get_extensions(self) -> list:
        return [
            {'host': 'media-player', 'id': 'netease-daily', 'label': '每日推荐', 'icon': '🎵',
             'view': 'ncm-daily', 'placement': 'sidebar', 'scope': 'all'},
            {'host': 'media-player', 'id': 'netease-playlists', 'label': '推荐歌单', 'icon': '📋',
             'view': 'ncm-playlists', 'placement': 'sidebar', 'scope': 'all'},
            {'host': 'media-player', 'id': 'netease-liked', 'label': '我的喜欢', 'icon': '❤️',
             'view': 'ncm-liked', 'placement': 'sidebar', 'scope': 'all'},
            {'host': 'media-player', 'id': 'netease-my-playlists', 'label': '我的歌单', 'icon': '📚',
             'view': 'ncm-my-playlists', 'placement': 'sidebar', 'scope': 'all'},
            {'host': 'media-player', 'id': 'netease-login', 'label': '登录', 'icon': '👤',
             'view': 'ncm-login', 'placement': 'sidebar', 'scope': 'all'},
        ]

    def register_api(self) -> dict:
        return {
            'search_song': self.search_song,
            'search_playlist': self.search_playlist,
            'get_daily_recommend': self.get_daily_recommend,
            'get_liked_songs': self.get_liked_songs,
            'get_created_playlists': self.get_created_playlists,
            'get_collected_playlists': self.get_collected_playlists,
            'get_playlist_tracks': self.get_playlist_tracks,
            'get_song_url': self.get_song_url,
            'get_lyric': self.get_lyric,
            'check_login': self.check_login,
            'login': self.login,
            'get_status': self.get_status,
        }

    def get_status(self) -> dict:
        try:
            result = self._get_api()._run_command('--version')
            available = bool(result.get('success'))
            return {
                'plugin': self.name,
                'ncm_cli_available': available,
                'ncm_cli_version': (result.get('stdout') or '').strip() if available else '',
                'error': result.get('stderr') or '' if not available else '',
            }
        except Exception as e:
            return {'plugin': self.name, 'ncm_cli_available': False, 'error': str(e)}

    def search_song(self, keyword: str) -> dict:
        try:
            songs = self._get_api().search_song(keyword)
            return {'success': True, 'results': [self._song(s) for s in songs]}
        except Exception as e:
            return {'success': False, 'error': str(e), 'results': []}

    def search_playlist(self, keyword: str) -> dict:
        try:
            items = self._get_api().search_playlist(keyword)
            return {'success': True, 'results': [self._playlist(p) for p in items]}
        except Exception as e:
            return {'success': False, 'error': str(e), 'results': []}

    def get_daily_recommend(self) -> dict:
        try:
            songs = self._get_api().get_daily_recommend()
            return {'success': True, 'results': [self._song(s) for s in songs]}
        except Exception as e:
            return {'success': False, 'error': str(e), 'results': []}

    def get_liked_songs(self, limit: int = 100) -> dict:
        try:
            songs = self._get_api().get_liked_songs(limit)
            return {'success': True, 'results': [self._song(s) for s in songs]}
        except Exception as e:
            return {'success': False, 'error': str(e), 'results': []}

    def get_created_playlists(self, limit: int = 100) -> dict:
        try:
            items = self._get_api().get_created_playlists(limit)
            return {'success': True, 'results': [self._playlist(p) for p in items]}
        except Exception as e:
            return {'success': False, 'error': str(e), 'results': []}

    def get_collected_playlists(self, limit: int = 100) -> dict:
        try:
            items = self._get_api().get_collected_playlists(limit)
            return {'success': True, 'results': [self._playlist(p) for p in items]}
        except Exception as e:
            return {'success': False, 'error': str(e), 'results': []}

    def get_playlist_tracks(self, playlist_id: str, limit: int = 100, offset: int = 0) -> dict:
        try:
            songs = self._get_api().get_playlist_tracks(playlist_id, limit=limit, offset=offset)
            return {'success': True, 'results': [self._song(s) for s in songs]}
        except Exception as e:
            return {'success': False, 'error': str(e), 'results': []}

    def get_song_url(self, song_id: str, original_id: str = None) -> dict:
        try:
            url = self._get_api().get_song_url(song_id, original_id)
            return {'success': bool(url), 'url': url or ''}
        except Exception as e:
            return {'success': False, 'error': str(e), 'url': ''}

    def get_lyric(self, song_id: str) -> dict:
        try:
            data = self._get_api().get_lyric(song_id)
            return {'success': bool(data), 'data': data}
        except Exception as e:
            return {'success': False, 'error': str(e), 'data': None}

    def check_login(self) -> dict:
        try:
            return {'success': self._get_api().check_login()}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def login(self, background: bool = True) -> dict:
        try:
            return {'success': True, 'message': self._get_api().login(background=background)}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @staticmethod
    def _song(song) -> dict:
        return {
            'id': song.id,
            'original_id': song.original_id,
            'name': song.name,
            'artists': song.artists,
            'album': song.album,
            'duration': song.duration,
            'url': song.url,
            'cover_url': getattr(song, 'cover_url', ''),
        }

    @staticmethod
    def _playlist(pl) -> dict:
        return {
            'id': pl.id,
            'original_id': pl.original_id,
            'name': pl.name,
            'track_count': pl.track_count,
            'play_count': pl.play_count,
            'cover_url': pl.cover_url,
        }
