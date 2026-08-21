"""媒体库增量扫描器：音频 + 视频统一索引。

- 音频专辑按 ID3/Flac/MP4 标签中的「专辑 + 专辑艺术家」聚合（与旧
  music-player 行为一致），支持内嵌封面抽取到 .cache/covers。
- 视频专辑按目录聚合，支持嵌套目录，封面优先使用目录内的
  cover/folder/poster 等图片。
- 文件 mtime/size 未变化时直接复用缓存，实现增量扫描。
"""

import hashlib
import os
import time
from pathlib import Path
from typing import Dict, Optional

from shell.backend.plugin_utils import load_sibling

_models = load_sibling(__file__, 'models', 'media_player')
_metadata = load_sibling(__file__, 'metadata', 'media_player')
_video_meta = load_sibling(__file__, 'video_meta', 'media_player')

MediaItem = _models.MediaItem
MetadataReader = _metadata.MetadataReader
probe_duration = _video_meta.probe_duration
extract_thumbnail = _video_meta.extract_thumbnail

INDEX_VERSION = 4

AUDIO_EXTS = _metadata.SUPPORTED_EXTS
VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv'}
COVER_NAMES = {
    'cover.jpg', 'cover.png', 'folder.jpg', 'folder.png',
    'poster.jpg', 'poster.png', 'fanart.jpg', 'fanart.png',
    'thumb.jpg', 'thumb.png', 'backdrop.jpg', 'backdrop.png',
}


def _item_id(path: str) -> str:
    return hashlib.md5(path.encode('utf-8')).hexdigest()[:16]


def _find_cover(directory: Path) -> Optional[Path]:
    try:
        for name in COVER_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    except OSError:
        pass
    return None


def _text(value: str, fallback: str = '') -> str:
    value = (value or '').strip()
    return value or fallback


def _audio_album_key(namespace: str, album: str, album_artist: str) -> str:
    """音频按标签聚合专辑；无标签时回退到目录。"""
    album = _text(album, '__untagged__')
    album_artist = _text(album_artist, 'unknown')
    return f'{namespace}//album::{album}||{album_artist}'


def _video_album_key(namespace: str, rel_dir: str) -> str:
    if rel_dir:
        return f'{namespace}/{rel_dir}'
    return namespace or '__root__'


def _video_thumb(file_path: Path, cover_dir: Path, item_id: str, duration: float) -> str:
    """为单个视频生成属于自己的封面帧（按 item_id 命名缓存）。"""
    dest = cover_dir / f'{item_id}.jpg'
    try:
        if dest.is_file() and dest.stat().st_size > 0:
            return str(dest.resolve())
        cover_dir.mkdir(parents=True, exist_ok=True)
        if duration and duration > 0:
            seek = min(max(duration * 0.1, 0.5), 120.0)
        else:
            seek = 10.0
        if extract_thumbnail(str(file_path), str(dest), at_seconds=seek):
            return str(dest.resolve())
    except Exception:
        pass
    return ''


def scan_media(root: Path, cache: Optional[Dict[str, MediaItem]] = None,
               namespace: str = '', cover_dir: Optional[Path] = None) -> Dict:
    """递归扫描单个媒体根目录，mtime/size 未变时复用缓存。"""
    root = Path(root).resolve()
    cache = cache or {}
    cover_dir = Path(cover_dir) if cover_dir else (root / '.cache' / 'covers')
    items: Dict[str, MediaItem] = {}
    dir_thumbs: Dict[str, str] = {}
    dir_thumb_done: set = set()

    for current_dir, dir_names, file_names in os.walk(root):
        current = Path(current_dir)
        if current.name.startswith('.') or current.name == '.cache':
            dir_names[:] = []
            continue
        dir_names[:] = [d for d in dir_names if not d.startswith('.') and d != '.cache']

        rel_dir = current.relative_to(root).as_posix() if current != root else ''
        folder_cover = _find_cover(current)
        folder_cover_abs = str(folder_cover.resolve()) if folder_cover else ''

        for file_name in sorted(file_names):
            suffix = Path(file_name).suffix.lower()
            if suffix in AUDIO_EXTS:
                kind = 'audio'
            elif suffix in VIDEO_EXTS:
                kind = 'video'
            else:
                continue

            full_path = current / file_name
            try:
                stat = full_path.stat()
            except OSError:
                continue

            rel_path = full_path.relative_to(root).as_posix()
            item_id = _item_id(str(full_path))
            cached = cache.get(item_id)

            if cached and cached.path == str(full_path) \
                    and cached.size == stat.st_size and cached.mtime == stat.st_mtime:
                item = cached
            else:
                cover_path = folder_cover_abs
                has_cover = bool(folder_cover_abs)

                if kind == 'audio':
                    meta = MetadataReader.read(full_path)
                    album = _text(meta.get('album'), current.name if current != root else (namespace or '未分类'))
                    album_artist = _text(meta.get('album_artist'), meta.get('artist') or '未知艺术家')
                    album_key = _audio_album_key(namespace, album, album_artist)
                    album_name = album
                    duration = float(meta.get('duration') or 0)
                    track = int(meta.get('track') or 0)

                    # 目录没有封面时，尝试抽取内嵌封面
                    if not has_cover:
                        try:
                            cover_name = MetadataReader.extract_cover(full_path, rel_path, cover_dir)
                            if cover_name:
                                cover_path = str((cover_dir / cover_name).resolve())
                                has_cover = True
                        except Exception:
                            pass

                    item = MediaItem(
                        id=item_id,
                        path=str(full_path),
                        kind=kind,
                        title=_text(meta.get('title'), full_path.stem),
                        artist=_text(meta.get('artist'), '未知艺术家'),
                        album=album_name,
                        album_key=album_key,
                        duration=duration,
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        cover_path=cover_path,
                        has_cover=has_cover,
                        album_artist=album_artist,
                        track=track,
                    )
                else:
                    album_key = _video_album_key(namespace, rel_dir)
                    album_name = current.name if current != root else (namespace or '未分类')
                    duration = probe_duration(str(full_path)) or 0.0

                    # 每个视频生成自己的封面帧（目录封面图优先）
                    if not has_cover:
                        generated = _video_thumb(full_path, cover_dir, item_id, duration)
                        if generated:
                            cover_path = generated
                            has_cover = True

                    item = MediaItem(
                        id=item_id,
                        path=str(full_path),
                        kind=kind,
                        title=full_path.stem,
                        artist=album_name,
                        album=album_name,
                        album_key=album_key,
                        duration=duration,
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        cover_path=cover_path,
                        has_cover=has_cover,
                        album_artist=album_name,
                        track=0,
                    )
            items[item_id] = item

    return {
        'items': [item.to_dict() for item in items.values()],
        'updated': time.time(),
        'version': INDEX_VERSION,
    }


def scan_directories(dirs, cache: Optional[Dict[str, MediaItem]] = None) -> Dict:
    """扫描多个媒体根目录并合并为一个统一索引。"""
    items: Dict[str, MediaItem] = {}
    for raw_dir in dirs:
        root = Path(raw_dir).resolve()
        namespace = root.name or str(root)
        result = scan_media(root, cache, namespace=namespace)
        for raw in result['items']:
            item = MediaItem.from_cache(raw)
            items[item.id] = item
    return {
        'items': [item.to_dict() for item in items.values()],
        'updated': time.time(),
        'version': INDEX_VERSION,
    }
