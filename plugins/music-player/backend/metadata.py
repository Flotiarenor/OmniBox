import json
import hashlib
from pathlib import Path
from typing import Optional, Tuple

try:
    from mutagen import File as MutagenFile
    from mutagen.id3 import ID3, APIC
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

SUPPORTED_EXTS = {'.mp3', '.flac', '.wav', '.aac', '.ogg', '.wma', '.m4a', '.opus', '.ape', '.wv'}

COVER_NAMES = ['cover.jpg', 'cover.png', 'folder.jpg', 'folder.png',
               'album.jpg', 'album.png', 'front.jpg', 'front.png']


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
    def extract_cover(file_path: Path, rel_path: str, cover_dir: Path) -> Optional[str]:
        hashed = hashlib.md5(rel_path.encode()).hexdigest()[:12]
        cover_path = cover_dir / f'{hashed}.jpg'

        if cover_path.exists():
            return cover_path.name

        cover_data = None
        if HAS_MUTAGEN:
            cover_data = MetadataReader._extract_embedded_cover(file_path)

        if not cover_data:
            cover_data = MetadataReader._find_folder_cover(file_path.parent)

        if cover_data:
            cover_dir.mkdir(parents=True, exist_ok=True)
            cover_path.write_bytes(cover_data)
            return cover_path.name
        return None

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
                        if audio_raw.tags:
                            raw = dict(audio_raw.tags)
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
            elif suffix == '.flac':
                return MetadataReader._cover_from_flac(file_path)
            elif suffix in ('.m4a', '.mp4', '.aac'):
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
                    return tag.data
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
            covers = audio.tags.get('covr', [])
            if covers:
                return bytes(covers[0])
        except Exception:
            pass
        return None

    @staticmethod
    def _find_folder_cover(dir_path: Path) -> Optional[bytes]:
        for name in COVER_NAMES:
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
            from mutagen.id3 import TextFrame
            frame = tags.get(frame_id)
            if frame is None:
                return None
            if hasattr(frame, 'text') and frame.text:
                return str(frame.text[0]) if isinstance(frame.text, (list, tuple)) else str(frame.text)
            return str(frame)
        except Exception:
            return None
