"""音频标签读取与内嵌 / 目录封面字节提取。

mutagen 是**强制依赖**（`requirements.txt` 固定版本，`HIDDEN_IMPORTS` 已登记），因此
在模块顶层直接 import：可选依赖的 `try/except ImportError` 会让每个使用点都变成
"可能未绑定"，静态检查被迫放弃这十来个调用点的类型。`HAS_MUTAGEN` 保留为 True ——
它是 `MetadataReader.debug_meta()` 对外载荷里的字段名（诊断输出说什么库可用）。

分类读取的兜底策略：mutagen 能读就用它（内嵌标签与时长），读失败或没有标签时退回
文件名解析（`_read_by_filename`），保证索引里永远有条目。
"""

from pathlib import Path
from typing import Any, Optional, cast

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4

HAS_MUTAGEN = True

SUPPORTED_EXTS = {'.mp3', '.flac', '.wav', '.aac', '.ogg', '.wma', '.m4a', '.opus', '.ape', '.wv'}

COVER_NAMES = ['cover.jpg', 'cover.png', 'folder.jpg', 'folder.png',
               'album.jpg', 'album.png', 'front.jpg', 'front.png']


def detect_image_mime(data: bytes) -> str:
    """按魔数推断图片 MIME（内嵌封面无扩展名时使用）。"""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:4] == b'GIF8':
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/jpeg'


def _sanitize_key(text: str) -> str:
    return text.strip().lower() if text else 'unknown'


class MetadataReader:

    @staticmethod
    def read(file_path: Path) -> dict:
        if HAS_MUTAGEN:
            try:
                return MetadataReader._read_with_mutagen(file_path)
            except Exception:
                pass
        return MetadataReader._read_by_filename(file_path)

    @staticmethod
    def extract_cover_bytes(file_path: Path) -> Optional[bytes]:
        """返回封面图片字节（内嵌封面优先，其次同目录 cover/folder 图）；无则 None。

        供 ThumbCache 自定义生成器调用（线程池并发，需无共享可变状态）。
        """
        data = MetadataReader._extract_embedded_cover(file_path)
        if data:
            return data
        return MetadataReader._find_folder_cover(Path(file_path).parent)

    @staticmethod
    def has_embedded_cover(file_path: Path) -> bool:
        """是否存在内嵌封面（扫描期 has_cover 检测用，不落盘）。"""
        return MetadataReader._extract_embedded_cover(file_path) is not None

    @staticmethod
    def debug_meta(file_path: Path) -> dict:
        info = {'file': str(file_path), 'mutagen_available': HAS_MUTAGEN}
        if not HAS_MUTAGEN:
            return info
        try:
            info['exists'] = file_path.exists()
            info['size'] = file_path.stat().st_size if file_path.exists() else 0
            info['suffix'] = file_path.suffix.lower()

            audio_raw = MutagenFile(str(file_path))
            if audio_raw:
                info['type'] = type(audio_raw).__name__
                if hasattr(audio_raw, 'info') and audio_raw.info:
                    info['duration'] = audio_raw.info.length
                    info['bitrate'] = getattr(audio_raw.info, 'bitrate', 0)
                    info['sample_rate'] = getattr(audio_raw.info, 'sample_rate', 0)
                if hasattr(audio_raw, 'tags') and audio_raw.tags:
                    raw_tags = dict(audio_raw.tags)
                    info['raw_tags'] = {k: list(v) if not isinstance(v, (str, int, float)) else v
                                        for k, v in raw_tags.items()}

            audio_easy = MutagenFile(str(file_path), easy=True)
            if audio_easy and hasattr(audio_easy, 'tags') and audio_easy.tags:
                easy_tags = dict(audio_easy.tags)
                info['easy_tags'] = {k: list(v) if not isinstance(v, (str, int, float)) else v
                                     for k, v in easy_tags.items()}
                info['easy_title'] = MetadataReader._first_tag(easy_tags, ['title'])
                info['easy_artist'] = MetadataReader._first_tag(easy_tags, ['artist'])
                info['easy_album'] = MetadataReader._first_tag(easy_tags, ['album'])
                info['easy_albumartist'] = MetadataReader._first_tag(easy_tags, ['albumartist'])
                info['easy_tracknumber'] = MetadataReader._first_tag(easy_tags, ['tracknumber'])
        except Exception as e:
            info['error'] = str(e)
        return info

    @staticmethod
    def _read_with_mutagen(file_path: Path) -> dict:
        length = 0
        title = artist = album = album_artist = None
        track = 0

        audio_easy = MutagenFile(str(file_path), easy=True)
        if audio_easy is not None:
            if hasattr(audio_easy, 'info') and audio_easy.info:
                length = int(audio_easy.info.length)
            if hasattr(audio_easy, 'tags') and audio_easy.tags:
                t = audio_easy.tags
                title = MetadataReader._first_tag(t, ['title'])
                artist = MetadataReader._first_tag(t, ['artist'])
                album = MetadataReader._first_tag(t, ['album'])
                album_artist = MetadataReader._first_tag(t, ['albumartist']) or artist
                track_str = MetadataReader._first_tag(t, ['tracknumber']) or '0'
                track = MetadataReader._parse_track(track_str)

        if not title or not artist:
            suffix = file_path.suffix.lower()
            if suffix == '.flac':
                try:
                    audio_raw = FLAC(str(file_path))
                    if audio_raw is not None:
                        if not length and audio_raw.info:
                            length = int(audio_raw.info.length)
                        # FLAC.tags 的静态类型是 MetadataBlock | VCFLACDict 的联合
                        # （mutagen 用兼容层同时声明两种标签容器），运行期这里一定是
                        # dict-like 的 VCFLACDict；cast 到 Any 是为了让下面的迭代可写。
                        flac_tags = cast(Any, audio_raw.tags)
                        if flac_tags is not None:
                            raw = {key: flac_tags[key] for key in flac_tags}
                            title = title or MetadataReader._first_tag(raw, ['TITLE', 'title'])
                            artist = artist or MetadataReader._first_tag(raw, ['ARTIST', 'artist'])
                            album = album or MetadataReader._first_tag(raw, ['ALBUM', 'album'])
                            aa = MetadataReader._first_tag(raw, ['ALBUMARTIST', 'ALBUM ARTIST', 'albumartist'])
                            album_artist = album_artist or aa or artist
                            if not track:
                                track = MetadataReader._parse_track(
                                    MetadataReader._first_tag(raw, ['TRACKNUMBER', 'tracknumber']) or '0')
                except Exception:
                    pass
            elif suffix == '.mp3':
                try:
                    audio_raw = ID3(str(file_path))
                    if audio_raw is not None:
                        title = title or MetadataReader._id3_text(audio_raw, 'TIT2')
                        artist = artist or MetadataReader._id3_text(audio_raw, 'TPE1')
                        album = album or MetadataReader._id3_text(audio_raw, 'TALB')
                        aa = MetadataReader._id3_text(audio_raw, 'TPE2')
                        album_artist = album_artist or aa or artist
                        if not track:
                            track = MetadataReader._parse_track(
                                MetadataReader._id3_text(audio_raw, 'TRCK') or '0')
                except Exception:
                    pass
            elif suffix in ('.m4a', '.mp4', '.aac'):
                try:
                    audio_raw = MP4(str(file_path))
                    if audio_raw is not None:
                        if not length and audio_raw.info:
                            length = int(audio_raw.info.length)
                        if audio_raw.tags:
                            raw = dict(audio_raw.tags)
                            title = title or MetadataReader._first_tag(raw, ['\xa9nam', '©nam'])
                            artist = artist or MetadataReader._first_tag(raw, ['\xa9ART', '©ART'])
                            album = album or MetadataReader._first_tag(raw, ['\xa9alb', '©alb'])
                            aa = MetadataReader._first_tag(raw, ['aART'])
                            album_artist = album_artist or aa or artist
                            if not track:
                                trkn = raw.get('trkn')
                                if trkn:
                                    track = trkn[0][0] if isinstance(trkn, list) and trkn else 0
                except Exception:
                    pass

        return {
            'title': str(title) if title else file_path.stem,
            'artist': str(artist) if artist else '未知艺术家',
            'album': str(album) if album else file_path.parent.name,
            'album_artist': str(album_artist) if album_artist else (str(artist) if artist else '未知艺术家'),
            'track': track,
            'duration': length,
        }

    @staticmethod
    def _read_by_filename(file_path: Path) -> dict:
        stem = file_path.stem
        parts = stem.split(' - ', 2)
        if len(parts) >= 2:
            track_part = parts[0].strip()
            title_part = parts[-1].strip()
            artist_part = parts[1].strip() if len(parts) == 3 else '未知艺术家'
        else:
            title_part = stem
            artist_part = '未知艺术家'
            track_part = ''

        track = MetadataReader._parse_track(track_part) if track_part else 0
        title = title_part if not track else parts[-1].strip() if len(parts) >= 2 else title_part

        return {
            'title': title,
            'artist': artist_part,
            'album': file_path.parent.name,
            'album_artist': artist_part,
            'track': track,
            'duration': 0,
        }

    @staticmethod
    def _extract_embedded_cover(file_path: Path) -> Optional[bytes]:
        suffix = file_path.suffix.lower()
        try:
            if suffix == '.mp3':
                return MetadataReader._cover_from_id3(file_path)
            if suffix == '.flac':
                return MetadataReader._cover_from_flac(file_path)
            if suffix in ('.m4a', '.mp4', '.aac'):
                return MetadataReader._cover_from_mp4(file_path)
        except Exception:
            pass
        return None

    @staticmethod
    def _cover_from_id3(file_path: Path) -> Optional[bytes]:
        try:
            tags = ID3(str(file_path))
            for tag in tags.values():
                if isinstance(tag, APIC):
                    # APIC 的 mime / data / desc 由 _framespec 在运行期生成，静态类型里
                    # 看不到这些属性（mutagen 的帧字段是声明式描述的），故按契约 cast。
                    return cast(Any, tag).data
        except Exception:
            pass
        return None

    @staticmethod
    def _cover_from_flac(file_path: Path) -> Optional[bytes]:
        try:
            audio = FLAC(str(file_path))
            if audio.pictures:
                return audio.pictures[0].data
        except Exception:
            pass
        return None

    @staticmethod
    def _cover_from_mp4(file_path: Path) -> Optional[bytes]:
        try:
            audio = MP4(str(file_path))
            # MP4.tags 是 Optional（无标签的文件返回 None），直接 .get 会漏掉这一点
            tags = audio.tags
            covers = tags.get('covr', []) if tags is not None else []
            if covers:
                return bytes(covers[0])
        except Exception:
            pass
        return None

    @staticmethod
    def _find_folder_cover(dir_path: Path, names: list | None = None) -> Optional[bytes]:
        for name in (names or COVER_NAMES):
            cover_file = dir_path / name
            if cover_file.exists():
                try:
                    return cover_file.read_bytes()
                except Exception:
                    pass
        return None

    @staticmethod
    def _first_tag(tags: dict, keys: list) -> Optional[str]:
        if not tags:
            return None
        for key in keys:
            val = tags.get(key)
            if val:
                if isinstance(val, list):
                    return str(val[0])
                return str(val)
        return None

    @staticmethod
    def _parse_track(track_str: str) -> int:
        if not track_str:
            return 0
        track_str = str(track_str).split('/')[0].strip()
        try:
            return int(track_str)
        except ValueError:
            return 0

    @staticmethod
    def _id3_text(tags, frame_id: str) -> Optional[str]:
        if not tags or not HAS_MUTAGEN:
            return None
        try:
            frame = tags.get(frame_id)
            if frame is None:
                return None
            if hasattr(frame, 'text') and frame.text:
                return str(frame.text[0]) if isinstance(frame.text, (list, tuple)) else str(frame.text)
            return str(frame)
        except Exception:
            return None
