"""网易云音乐 Python API 封装
基于 ncm-cli 命令行工具，支持 Companion / media-player 复用。
"""
import subprocess
import json
import os
import re
import shlex
import tempfile
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime


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


class NeteaseMusicAPI:
    _resident_initialized = False

    def __init__(self, check_install: bool = True, url_cache_size: int = 100):
        self._config_dir = Path.home() / ".config" / "ncm"
        self._preference_file = self._config_dir / "ncm-preference.json"
        self._history_file = self._config_dir / "ncm-history.json"
        self._schedule_file = self._config_dir / "ncm-schedule.json"
        self._url_cache = {}
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
            print(f"[netease-music] 写入 ncm-cli player=mpv 配置失败: {e}")

    def _run_command(self, cmd: str, timeout: int = 30, env: dict = None) -> Dict[str, Any]:
        try:
            if os.name == 'nt':
                # Windows 下 npm 安装的 ncm-cli 通常是 .cmd/.ps1 shim，
                # 使用 shell=True 才能正确解析到命令。
                result = subprocess.run(
                    f"ncm-cli --output json {cmd}",
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=timeout,
                    env=env,
                )
            else:
                full_cmd = ["ncm-cli", "--output", "json"] + shlex.split(cmd)
                result = subprocess.run(
                    full_cmd,
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=timeout,
                    env=env,
                )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "stdout": "", "stderr": "命令执行超时", "returncode": -1}
        except Exception as e:
            return {"success": False, "stdout": "", "stderr": str(e), "returncode": -1}

    def _command_success(self, result: Dict[str, Any]) -> bool:
        if not result.get("success"):
            return False
        data = self._parse_json_output(result.get("stdout") or "")
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
        data = self._parse_json_output(result["stdout"])
        if isinstance(data, dict):
            return bool(data.get("success", result["success"]))
        return result["success"] and "未登录" not in result["stdout"]

    def login(self, background: bool = True) -> str:
        if background:
            result = self._run_command("login --background")
        else:
            result = self._run_command("login")
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 登录失败")
        data = self._parse_json_output(result["stdout"])
        if isinstance(data, dict):
            return (
                data.get("qrCodeUrl")
                or data.get("clickableUrl")
                or data.get("message")
                or result["stdout"]
            )
        return result["stdout"]

    def set_config(self, key: str, value: str) -> bool:
        result = self._run_command(f'config set {key} "{value}"')
        return result["success"]

    def get_config(self, key: str) -> Optional[str]:
        result = self._run_command(f"config get {key}")
        if result["success"]:
            return result["stdout"]
        return None

    def get_commands(self) -> str:
        result = self._run_command("commands")
        return result["stdout"]

    def run_cli_command(self, command: str) -> Dict[str, Any]:
        cmd = (command or "").strip()
        if cmd.startswith("ncm-cli"):
            cmd = cmd[len("ncm-cli"):].strip()
        if not cmd:
            return {"success": False, "stdout": "", "stderr": "请输入要执行的 ncm-cli 命令", "returncode": -1}
        return self._run_command(cmd)

    def search_song(self, keyword: str, user_input: str = None) -> List[Song]:
        cmd = f'search song --keyword "{keyword}"'
        result = self._run_command(cmd)
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 搜索歌曲失败")
        return self._parse_songs(result["stdout"])

    def search_playlist(self, keyword: str, user_input: str = None) -> List[Playlist]:
        cmd = f'search playlist --keyword "{keyword}"'
        result = self._run_command(cmd)
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 搜索歌单失败")
        return self._parse_playlists(result["stdout"])

    def search_album(self, keyword: str, user_input: str = None) -> List[Album]:
        cmd = f'search album --keyword "{keyword}"'
        result = self._run_command(cmd)
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 搜索专辑失败")
        return self._parse_albums(result["stdout"])

    def create_playlist(self, name: str) -> Optional[Playlist]:
        cmd = f'playlist create --playlistName "{name}"'
        result = self._run_command(cmd)
        if not result["success"]:
            return None
        return self._parse_single_playlist(result["stdout"])

    def add_to_playlist(self, playlist_id: str, song_ids: List[str]) -> bool:
        import json as _json
        ids_json = _json.dumps(list(song_ids), ensure_ascii=False)
        cmd = f"playlist add --playlistId {playlist_id} --songIdList '{ids_json}'"
        result = self._run_command(cmd)
        return self._command_success(result)

    def get_playlist_detail(self, playlist_id: str) -> Optional[Dict]:
        cmd = f'playlist get --playlistId {playlist_id}'
        result = self._run_command(cmd)
        if not result["success"]:
            return None
        data = self._parse_json_output(result["stdout"])
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data

    def get_playlist_tracks(self, playlist_id: str, limit: int = 30, offset: int = 0) -> List[Song]:
        cmd = f'playlist tracks --playlistId {playlist_id} --limit {limit} --offset {offset}'
        result = self._run_command(cmd)
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取歌单歌曲失败")
        return self._parse_songs(result["stdout"])

    def get_created_playlists(self, limit: int = 100) -> List[Playlist]:
        result = self._run_command(f"playlist created --limit {limit}")
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取创建的歌单失败")
        return self._parse_playlists(result["stdout"])

    def get_collected_playlists(self, limit: int = 100) -> List[Playlist]:
        result = self._run_command(f"playlist collected --limit {limit}")
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取收藏的歌单失败")
        return self._parse_playlists(result["stdout"])

    def get_my_playlists(self) -> List[Playlist]:
        result = self._run_command("playlist list")
        return self._parse_playlists(result["stdout"])

    def get_daily_recommend(self) -> List[Song]:
        result = self._run_command("recommend daily --limit 30")
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取每日推荐失败")
        return self._parse_songs(result["stdout"])

    def get_user_info(self) -> Optional[Dict]:
        result = self._run_command("user info")
        if not result["success"]:
            return None
        data = self._parse_json_output(result["stdout"])
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data

    def get_liked_songs(self, limit: int = 200) -> List[Song]:
        playlist_id = self._get_favorite_playlist_id()
        if not playlist_id:
            raise RuntimeError("无法获取“我的喜欢”歌单，请先登录后重试")
        cmd = f'playlist tracks --playlistId {playlist_id} --limit {limit}'
        result = self._run_command(cmd)
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取红心歌曲失败")
        return self._parse_songs(result["stdout"])

    def _get_favorite_playlist_id(self) -> Optional[str]:
        result = self._run_command("user favorite")
        if not result["success"]:
            return None
        data = self._parse_json_output(result["stdout"])
        if not isinstance(data, dict):
            return None
        playlist = data.get("data", {})
        if isinstance(playlist, dict):
            return playlist.get("id")
        return None

    def get_song_url(self, song_id: str, original_id: Optional[str] = None) -> Optional[str]:
        """获取可播放 URL。优先假 mpv 截获，失败回退公共外链。"""
        if original_id:
            return self.resolve_song_url(song_id, original_id)
        # 只有加密 ID 时无法直接拼公共外链；交由上层补传原始 ID。
        return None

    def resolve_song_url(self, song_id: str, original_id: str, timeout: int = 15) -> str:
        public_url = f"https://music.163.com/song/media/outer/url?id={original_id}.mp3"

        # 短期 URL 缓存，最多保留 self._url_cache_size 条
        if song_id in self._url_cache:
            return self._url_cache[song_id]

        tmp_dir = Path(tempfile.gettempdir())
        fake_bin = tmp_dir / "omnibox-ncm-fake-mpv"
        fake_mpv = self._ensure_fake_mpv(fake_bin)
        captured = tmp_dir / "ncm-captured-url.txt"
        log = tmp_dir / "ncm-fake-mpv.log"
        sock = Path.home() / ".config" / "ncm-cli" / "mpv.sock"

        # 首次初始化时清理一次旧 daemon / 真实 mpv，之后保持常驻
        if not NeteaseMusicAPI._resident_initialized:
            for pat in ("ncm-cli play --song", "ncm-cli play --playlist", str(fake_mpv), "mpv --no-video"):
                try:
                    subprocess.run(["pkill", "-f", pat], capture_output=True)
                except Exception:
                    pass
            for p in (captured, log, sock):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
            NeteaseMusicAPI._resident_initialized = True

        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

        try:
            self._run_command(
                f'play --song --encrypted-id {song_id} --original-id {original_id}',
                timeout=timeout,
                env=env,
            )
        except Exception:
            pass

        url = None
        deadline = time.time() + min(timeout, 10)
        while time.time() < deadline:
            if captured.exists():
                url = captured.read_text(encoding="utf-8").strip()
                if url:
                    break
            time.sleep(0.2)

        if not url:
            url = public_url

        # 写入短期缓存，超过上限时移除最旧一条
        if len(self._url_cache) >= self._url_cache_size:
            try:
                self._url_cache.pop(next(iter(self._url_cache)))
            except Exception:
                pass
        self._url_cache[song_id] = url
        return url

    def _ensure_fake_mpv(self, bin_dir: Path) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "mpv.py"
        script.write_text(
            r"""#!/usr/bin/env python3
import json, os, socket, sys, tempfile, time
from pathlib import Path
LOG = Path(tempfile.gettempdir()) / "ncm-fake-mpv.log"
CAP = Path(tempfile.gettempdir()) / "ncm-captured-url.txt"
def log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
def main():
    log("=== fake mpv called ===")
    log("ARGS=" + json.dumps(sys.argv[1:], ensure_ascii=False))
    if "--version" in sys.argv:
        print("mpv 0.37.0 Copyright © 2000-2023 mpv/MPlayer/mplayer2 projects")
        return 0
    sock_path = None
    for arg in sys.argv[1:]:
        if arg.startswith("--input-ipc-server="):
            sock_path = arg.split("=", 1)[1]
    if not sock_path:
        log("NO_IPC_SOCKET_ARG")
        time.sleep(30)
        return 0
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(5)
    log("IPC_LISTENING=" + sock_path)
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            break
        with conn:
            f = conn.makefile("r", encoding="utf-8")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                log("IPC_RECV=" + line)
                try:
                    req = json.loads(line)
                except Exception:
                    continue
                cmd = req.get("command") or []
                name = cmd[0] if cmd else ""
                if name == "loadfile" and len(cmd) >= 2:
                    url = cmd[1]
                    CAP.write_text(url + "\n", encoding="utf-8")
                    log("CAPTURED_URL=" + url)
                resp = {"error": "success", "request_id": req.get("request_id")}
                try:
                    f.write(json.dumps(resp) + "\n")
                    f.flush()
                except Exception:
                    pass
                if name == "quit":
                    server.close()
                    return 0
        time.sleep(0.01)
if __name__ == "__main__":
    sys.exit(main())
""",
            encoding="utf-8",
        )
        if os.name == 'nt':
            fake = bin_dir / "mpv.cmd"
            fake.write_text(
                '@echo off\r\npython "%~dp0mpv.py" %*\r\n',
                encoding="utf-8",
            )
        else:
            fake = bin_dir / "mpv"
            # Unix 直接软链/复制 Python 脚本为可执行的 mpv
            try:
                fake.symlink_to(script.name)
            except OSError:
                fake.write_bytes(script.read_bytes())
            fake.chmod(0o755)
        return fake

    def play(self, song_id: str = None, playlist_id: str = None) -> bool:
        if song_id and playlist_id:
            return False
        if song_id:
            cmd = f'play --song --encrypted-id {song_id}'
        elif playlist_id:
            cmd = f'play --playlist --encrypted-id {playlist_id}'
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
        volume = max(0, min(100, volume))
        return self._command_success(self._run_command(f"volume {volume}"))

    def get_playback_status(self) -> Optional[Dict]:
        result = self._run_command("state")
        if not result["success"]:
            return None
        return self._parse_json_output(result["stdout"])

    def get_lyric(self, song_id: str) -> Optional[Dict]:
        result = self._run_command(f"song lyric --songId {song_id}")
        if not result["success"]:
            raise RuntimeError(result["stderr"] or result["stdout"] or "ncm-cli 获取歌词失败")
        data = self._parse_json_output(result["stdout"])
        if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
            return data["data"]
        return data

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

    def _calculate_preference(self, songs: List[Song]) -> Dict:
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

    def _parse_songs(self, output: str) -> List[Song]:
        data = self._load_json(output)
        items = self._extract_list(data, ["songs", "data", "result"])
        songs = []
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
            if isinstance(album_raw, dict):
                album = album_raw.get("name") or ""
            else:
                album = str(album_raw or "")
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

    def _parse_playlists(self, output: str) -> List[Playlist]:
        data = self._load_json(output)
        items = self._extract_list(data, ["playlists", "data", "result"])
        playlists = []
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

    def _parse_albums(self, output: str) -> List[Album]:
        data = self._load_json(output)
        items = self._extract_list(data, ["albums", "data", "result"])
        albums = []
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

    def _parse_single_playlist(self, output: str) -> Optional[Playlist]:
        data = self._load_json(output)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return self._playlist_from_dict(data["data"])
        playlists = self._parse_playlists(output)
        return playlists[0] if playlists else None

    def _playlist_from_dict(self, item: dict) -> Optional[Playlist]:
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

    def _parse_json_output(self, output: str) -> Optional[Dict]:
        return self._load_json(output)

    def _load_json(self, output: str):
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

    def _extract_list(self, data, keys, _depth: int = 0):
        if isinstance(data, list):
            return data
        if not isinstance(data, dict) or _depth > 5:
            return None
        for key in list(keys) + ["records", "tracks", "songs", "playlists", "albums"]:
            value = data.get(key)
            if isinstance(value, list):
                return value
        for key in list(keys) + ["data", "result"]:
            value = data.get(key)
            if isinstance(value, dict):
                found = self._extract_list(value, keys, _depth + 1)
                if found:
                    return found
        return None
