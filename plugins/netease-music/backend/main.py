"""网易云音乐 Companion 插件后端。

入口只做装配、API 表与壳钩子：ncm-cli 命令执行、输出解析、假 mpv 截获分别在
`ncm_cli.py` / `parsing.py` / `fake_mpv.py`，音乐数据接口在 `netease_music_api.py`，
远端缓存失效与新鲜期投影在 `remote_cache.py`。
组件之间不互相 import，依赖一律由这里按构造参数注入。
"""

from typing import ClassVar

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

_ncm = load_sibling(__file__, 'ncm_cli', 'netease_music')
_parsing = load_sibling(__file__, 'parsing', 'netease_music')
_fake_mpv = load_sibling(__file__, 'fake_mpv', 'netease_music')
_api = load_sibling(__file__, 'netease_music_api', 'netease_music')
_remote = load_sibling(__file__, 'remote_cache', 'netease_music')
NeteaseMusicAPI = _api.NeteaseMusicAPI


class NeteaseMusicPlugin(PluginBase):
    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self._api = None
        # 装配顺序就是依赖顺序：三个无状态协作者按构造参数注入数据接口（组件之间不 import）。
        self._runner = _ncm.NcmRunner()
        self._parser = _parsing.OutputParser()
        self._fake_mpv = _fake_mpv.FakeMpv()
        self.remote_cache = _remote.RemoteCache(
            self, self._invalidate_caches, self.NETEASE_TTL_SECONDS)

    def _get_api(self) -> NeteaseMusicAPI:
        if self._api is None:
            self._api = NeteaseMusicAPI(
                self._runner, self._parser, self._fake_mpv, check_install=False)
        return self._api

    # ---------- 统一刷新基建（远端来源） ----------
    #
    # 本插件没有本地文件树：数据全部来自 ncm-cli 请求，本地只有一份**播放地址缓存**
    # （短期地址 + 有效期上限）。因此它是 `mode: 'remote'` 的来源 —— 判据只能是时间，
    # 「同步」= TTL 到期才丢弃本地缓存，「校验」= 立刻丢弃（忽略 TTL）。
    # 真正取数仍由各视图按需发起，invalidate 只负责让那份取数不再命中旧缓存。

    # 远端数据的**新鲜期**（投影给 `get_status.freshness`，界面暂未消费）：半小时。
    # 与播放地址缓存的有效期不是同一个数：地址是更短期的外链（`URL_CACHE_TTL_SECONDS`，
    # 十分钟），过期后单条重新解析；这里的 TTL 决定整份远端数据何时该重新取。
    NETEASE_TTL_SECONDS: ClassVar[float] = 1800.0

    def freshness_spec(self) -> dict:
        return {
            'mode': 'remote',
            'ttl_seconds': self.NETEASE_TTL_SECONDS,
            'invalidate': self._invalidate_caches,
            'unit': '首',
        }

    def _invalidate_caches(self, reason: str = '') -> dict:
        """丢弃本地派生缓存（播放地址），返回丢弃条数。

        `reason` 由刷新引擎给出（`ttl` / `verify` / `identity`），引擎把它记在报告的
        `invalidate_reason` 里；这里只接受它，判定仍只有"整体丢弃"一种。
        """
        try:
            dropped = self._get_api().clear_url_cache()
        except Exception as e:
            return {'url_cache': 0, 'error': str(e)}
        return {'url_cache': dropped}

    def get_extensions(self) -> list:
        return [
            {'host': 'media-player', 'id': 'netease-daily', 'label': '每日推荐', 'icon': 'icon:music',
             'view': 'ncm-daily', 'placement': 'sidebar', 'scope': 'all'},
            {'host': 'media-player', 'id': 'netease-playlists', 'label': '推荐歌单', 'icon': 'icon:list-music',
             'view': 'ncm-playlists', 'placement': 'sidebar', 'scope': 'all'},
            {'host': 'media-player', 'id': 'netease-liked', 'label': '我的喜欢', 'icon': 'icon:heart',
             'view': 'ncm-liked', 'placement': 'sidebar', 'scope': 'all'},
            {'host': 'media-player', 'id': 'netease-my-playlists', 'label': '我的歌单', 'icon': 'icon:library',
             'view': 'ncm-my-playlists', 'placement': 'sidebar', 'scope': 'all'},
            {'host': 'media-player', 'id': 'netease-login', 'label': '登录', 'icon': 'icon:user',
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
        # 远端新鲜期随状态一起给出。宿主读 `get_status` 时只消费 `ncm_cli_available`
        # （决定是否提示安装 ncm-cli），`freshness` 目前仅供状态接口与测试读取，
        # 界面暂未按它显示任何内容。
        freshness = self.remote_cache.state()
        try:
            result = self._get_api()._run_command('--version')
            available = bool(result.get('success'))
            return {
                'plugin': self.name,
                'ncm_cli_available': available,
                'ncm_cli_version': (result.get('stdout') or '').strip() if available else '',
                'error': result.get('stderr') or '' if not available else '',
                'freshness': freshness,
            }
        except Exception as e:
            return {'plugin': self.name, 'ncm_cli_available': False, 'error': str(e),
                    'freshness': freshness}

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

    def get_song_url(self, song_id: str, original_id: str | None = None) -> dict:
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
        """登录态查询：判定为已登录时作废远端缓存（换过账号，旧列表与地址都不可信）。"""
        try:
            logged_in = bool(self._get_api().check_login())
        except Exception as e:
            return {'success': False, 'error': str(e)}
        self.remote_cache.note_login_state(logged_in)
        return {'success': logged_in}

    def login(self, background: bool = True) -> dict:
        """启动登录流程；流程一开始旧账号的数据就不再可信，因此先作废远端缓存。"""
        try:
            message = self._get_api().login(background=background)
        except Exception as e:
            return {'success': False, 'error': str(e)}
        self.remote_cache.mark_login_started()
        return {'success': True, 'message': message}

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
