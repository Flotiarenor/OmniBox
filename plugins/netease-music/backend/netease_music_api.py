"""网易云音乐数据接口：经 ncm-cli 取数，并缓存短期播放地址。

协作对象由入口注入：`NcmRunner`（执行命令）、`OutputParser`（解析输出）、
`FakeMpv`（截获播放地址）。组件之间不互相 import，依赖方向因此只有"入口 → 本组件"。

状态自持：播放地址缓存与 ncm-cli 的配置目录都在这里。地址是**短期**的（外链带时效），
因此缓存条目带写入时刻、有效期 `URL_CACHE_TTL_SECONDS`，过期后重新解析；
`clear_url_cache()` 仍然可以整体丢弃（统一刷新的「校验」用它）。
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

log = logging.getLogger(__name__)


class NeteaseMusicAPI:
    """ncm-cli 的薄封装：命令调用 + 输出解析 + 地址缓存。"""

    #: 播放地址缓存的有效期。外链带时效，缓存过久会把过期地址交回播放器。
    URL_CACHE_TTL_SECONDS: float = 600.0

    def __init__(self, runner: Any, parser: Any, fake_mpv: Any,
                 check_install: bool = True, url_cache_size: int = 100) -> None:
        self._runner = runner
        self._parser = parser
        self._fake_mpv = fake_mpv
        self._config_dir = Path.home() / ".config" / "ncm"
        self._preference_file = self._config_dir / "ncm-preference.json"
        self._history_file = self._config_dir / "ncm-history.json"
        self._schedule_file = self._config_dir / "ncm-schedule.json"
        # 键 -> (播放地址, 写入时刻)。时刻用于过期判定，不参与地址本身。
        self._url_cache: Dict[str, Tuple[str, float]] = {}
        self._url_cache_size = max(1, url_cache_size)
        # 我们的假 mpv 方案依赖 ncm-cli 使用 mpv 播放器；
        # 直接写入配置可以绕过 ncm-cli 对“真实 mpv 是否安装”的检查。
        self._ensure_player_mpv_config()
        if check_install:
            self._check_installation()

    def _ensure_player_mpv_config(self):
        try:
            cfg = Path.home() / ".config" / "ncm-cli" / "config.json"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if cfg.exists():
                try:
                    data = json.loads(cfg.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            if data.get("player") != "mpv":
                data["player"] = "mpv"
                cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log.error(f"[netease-music] 写入 ncm-cli player=mpv 配置失败: {e}")

    def _run_command(self, cmd, timeout: int = 30, env: dict | None = None) -> Dict[str, Any]:
        """执行 ncm-cli 命令（参数数组形式见 `NcmRunner.run`）。"""
        return cast(Dict[str, Any], self._runner.run(cmd, timeout=timeout, env=env))

    def _command_success(self, result: Dict[str, Any]) -> bool:
        if not result.get("success"):
            return False
        data = self._parser.json(result.get("stdout") or "")
        if isinstance(data, dict) and "success" in data:
            return bool(data.get("success"))
        return True

    def _check_installation(self) -> bool:
        result = self._run_command("--version")
        if not result["success"]:
            raise RuntimeError("ncm-cli未安装，请先执行: npm install -g @music163/ncm-cli")
        return True

    def check_login(self) -> bool:
        result = self._run_command("login --check")
        data = self._parser.json(result["stdout"])
        if isinstance(data, dict):
            return bool(data.get("success", result["success"]))
        return bool(result["success"] and "未登录" not in result["stdout"])

    def clear_url_cache(self) -> int:
        """丢弃播放地址缓存，返回丢弃条数。

        地址是**短期**的（网易云外链带时效，`resolve_song_url` 也只是"截获当下那次"），
        条目本身还有有效期上限；这里用于登录态变化或手动校验时整体丢弃，
        下次取地址重新解析。
        """
        count = len(self._url_cache)
        self._url_cache.clear()
        return count

    def login(self, background: bool = True) -> str:
        result = self._run_command("login --background") if background else self._run_command("login")
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 登录失败")
        data = self._parser.json(result["stdout"])
        if isinstance(data, dict):
            return (
                data.get("qrCodeUrl")
                or data.get("clickableUrl")
                or data.get("message")
                or result["stdout"]
            )
        return cast(str, result["stdout"])

    def set_config(self, key: str, value: str) -> bool:
        result = self._run_command(['config', 'set', str(key), str(value)])
        return bool(result["success"])

    def get_config(self, key: str) -> Optional[str]:
        result = self._run_command(['config', 'get', str(key)])
        if result["success"]:
            return cast(str, result["stdout"])
        return None

    def get_commands(self) -> str:
        result = self._run_command("commands")
        return cast(str, result["stdout"])

    def run_cli_command(self, command: str) -> Dict[str, Any]:
        cmd = (command or "").strip()
        if cmd.startswith("ncm-cli"):
            cmd = cmd[len("ncm-cli"):].strip()
        if not cmd:
            return {"success": False, "stdout": "", "stderr": "请输入要执行的 ncm-cli 命令", "returncode": -1}
        return self._run_command(cmd)

    # ===== 搜索与歌单 =====

    def search_song(self, keyword: str, user_input: str | None = None) -> List[Any]:
        result = self._run_command(['search', 'song', '--keyword', str(keyword)])
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 搜索歌曲失败")
        return cast(List[Any], self._parser.songs(result["stdout"]))

    def search_playlist(self, keyword: str, user_input: str | None = None) -> List[Any]:
        result = self._run_command(['search', 'playlist', '--keyword', str(keyword)])
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 搜索歌单失败")
        return cast(List[Any], self._parser.playlists(result["stdout"]))

    def search_album(self, keyword: str, user_input: str | None = None) -> List[Any]:
        result = self._run_command(['search', 'album', '--keyword', str(keyword)])
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 搜索专辑失败")
        return cast(List[Any], self._parser.albums(result["stdout"]))

    def create_playlist(self, name: str) -> Optional[Any]:
        result = self._run_command(['playlist', 'create', '--playlistName', str(name)])
        if not result["success"]:
            return None
        return self._parser.single_playlist(result["stdout"])

    def add_to_playlist(self, playlist_id: str, song_ids: List[str]) -> bool:
        ids_json = json.dumps(list(song_ids), ensure_ascii=False)
        result = self._run_command([
            'playlist', 'add',
            '--playlistId', str(playlist_id),
            '--songIdList', ids_json,
        ])
        return self._command_success(result)

    def get_playlist_detail(self, playlist_id: str) -> Any:
        result = self._run_command(['playlist', 'get', '--playlistId', str(playlist_id)])
        if not result["success"]:
            return None
        data = self._parser.json(result["stdout"])
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data

    def get_playlist_tracks(self, playlist_id: str, limit: int = 30, offset: int = 0) -> List[Any]:
        result = self._run_command([
            'playlist', 'tracks',
            '--playlistId', str(playlist_id),
            '--limit', str(limit),
            '--offset', str(offset),
        ])
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取歌单歌曲失败")
        return cast(List[Any], self._parser.songs(result["stdout"]))

    def get_created_playlists(self, limit: int = 100) -> List[Any]:
        result = self._run_command(['playlist', 'created', '--limit', str(limit)])
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取创建的歌单失败")
        return cast(List[Any], self._parser.playlists(result["stdout"]))

    def get_collected_playlists(self, limit: int = 100) -> List[Any]:
        result = self._run_command(['playlist', 'collected', '--limit', str(limit)])
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取收藏的歌单失败")
        return cast(List[Any], self._parser.playlists(result["stdout"]))

    def get_my_playlists(self) -> List[Any]:
        result = self._run_command("playlist list")
        return cast(List[Any], self._parser.playlists(result["stdout"]))

    def get_daily_recommend(self) -> List[Any]:
        result = self._run_command("recommend daily --limit 30")
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取每日推荐失败")
        return cast(List[Any], self._parser.songs(result["stdout"]))

    def get_user_info(self) -> Any:
        result = self._run_command("user info")
        if not result["success"]:
            return None
        data = self._parser.json(result["stdout"])
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data

    def get_liked_songs(self, limit: int = 200) -> List[Any]:
        playlist_id = self._get_favorite_playlist_id()
        if not playlist_id:
            raise RuntimeError("无法获取“我的喜欢”歌单，请先登录后重试")
        cmd = ['playlist', 'tracks', '--playlistId', str(playlist_id), '--limit', str(limit)]
        result = self._run_command(cmd)
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取红心歌曲失败")
        return cast(List[Any], self._parser.songs(result["stdout"]))

    def _get_favorite_playlist_id(self) -> Optional[str]:
        result = self._run_command("user favorite")
        if not result["success"]:
            return None
        data = self._parser.json(result["stdout"])
        if not isinstance(data, dict):
            return None
        playlist = data.get("data", {})
        if isinstance(playlist, dict):
            return cast(Optional[str], playlist.get("id"))
        return None

    # ===== 播放地址 =====

    def get_song_url(self, song_id: str, original_id: Optional[str] = None) -> Optional[str]:
        """获取可播放 URL。优先假 mpv 截获，失败回退公共外链。"""
        if original_id:
            return self.resolve_song_url(song_id, original_id)
        # 只有加密 ID 时无法直接拼公共外链；交由上层补传原始 ID。
        return None

    def resolve_song_url(self, song_id: str, original_id: str, timeout: int = 15) -> str:
        public_url = f"https://music.163.com/song/media/outer/url?id={original_id}.mp3"

        # 短期 URL 缓存，最多保留 self._url_cache_size 条；超过有效期一律重新解析。
        cached = self._url_cache.get(song_id)
        if cached is not None:
            cached_url, stored_at = cached
            if time.time() - stored_at < self.URL_CACHE_TTL_SECONDS:
                return cached_url
            self._url_cache.pop(song_id, None)

        self._fake_mpv.prepare()
        env = self._fake_mpv.environment()
        try:
            self._run_command(
                ['play', '--song', '--encrypted-id', str(song_id),
                 '--original-id', str(original_id)],
                timeout=timeout,
                env=env,
            )
        except Exception:
            pass

        url = self._fake_mpv.wait_url(timeout) or public_url

        # 写入短期缓存，超过上限时移除最旧一条
        if len(self._url_cache) >= self._url_cache_size:
            try:
                self._url_cache.pop(next(iter(self._url_cache)))
            except Exception:
                pass
        self._url_cache[song_id] = (url, time.time())
        return url

    # ===== 播放控制 =====

    def play(self, song_id: str | None = None, playlist_id: str | None = None) -> bool:
        if song_id and playlist_id:
            return False
        if song_id:
            cmd = ['play', '--song', '--encrypted-id', str(song_id)]
        elif playlist_id:
            cmd = ['play', '--playlist', '--encrypted-id', str(playlist_id)]
        else:
            return False
        result = self._run_command(cmd)
        return self._command_success(result)

    def pause(self) -> bool:
        return self._command_success(self._run_command("pause"))

    def resume(self) -> bool:
        return self._command_success(self._run_command("resume"))

    def next_track(self) -> bool:
        return self._command_success(self._run_command("next"))

    def previous_track(self) -> bool:
        return self._command_success(self._run_command("prev"))

    def set_volume(self, volume: int) -> bool:
        volume = max(0, min(100, int(volume)))
        return self._command_success(self._run_command(['volume', str(volume)]))

    def get_playback_status(self) -> Optional[Any]:
        result = self._run_command("state")
        if not result["success"]:
            return None
        return self._parser.json(result["stdout"])

    def get_lyric(self, song_id: str) -> Any:
        result = self._run_command(['song', 'lyric', '--songId', str(song_id)])
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取歌词失败")
        return self._parser.data_of(result["stdout"])

    # ===== 偏好分析 =====

    def analyze_preference(self, force_refresh: bool = False) -> Optional[Dict]:
        if not force_refresh and self._preference_file.exists():
            try:
                with open(self._preference_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    updated_at = datetime.fromisoformat(data.get("updatedAt", "2000-01-01"))
                    if (datetime.now() - updated_at).total_seconds() < 86400:
                        return data
            except Exception:
                pass
        liked_songs = self.get_liked_songs(200)
        if not liked_songs:
            return None
        preference = self._calculate_preference(liked_songs)
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with open(self._preference_file, "w", encoding="utf-8") as f:
            json.dump(preference, f, ensure_ascii=False, indent=2)
        return preference

    def _calculate_preference(self, songs: List[Any]) -> Dict:
        tag_counts = {}
        for song in songs:
            for tag in (song.tags or []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
        top_tags = [tag for tag, _ in sorted_tags[:8]]
        return {
            "overallProfile": f"偏好{', '.join(top_tags[:3])}等风格",
            "recentTrend": "暂无明显变化",
            "keywords": top_tags[:6] if top_tags else ["流行"],
            "contentTags": top_tags,
            "updatedAt": datetime.now().isoformat(),
        }
