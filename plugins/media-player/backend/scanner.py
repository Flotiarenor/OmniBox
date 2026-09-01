"""媒体库增量扫描器：音频 + 视频统一索引。

- 音频专辑按 ID3/Flac/MP4 标签中的「专辑 + 专辑艺术家」聚合（与旧
  music-player 行为一致）；has_cover 只做检测，封面字节由 ThumbCache
  懒生成（见 cover_generator），不再在扫描期抽取落盘。
- 视频专辑按目录聚合，支持嵌套目录；has_cover 恒为 True（目录封面图
  优先，否则由前端 canvas 抽帧后经 media_put_thumb 回写缓存）。
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
detect_image_mime = _metadata.detect_image_mime
probe_duration = _video_meta.probe_duration

INDEX_VERSION = 4

AUDIO_EXTS = _metadata.SUPPORTED_EXTS
VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv'}
COVER_NAMES = {
    'cover.jpg', 'cover.png', 'folder.jpg', 'folder.png',
    'poster.jpg', 'poster.png', 'fanart.jpg', 'fanart.png',
    'thumb.jpg', 'thumb.png', 'backdrop.jpg', 'backdrop.png',
}


def cover_generator(src_path: Path) -> Optional[tuple]:
    """ThumbCache 自定义生成器：音频内嵌封面或视频目录封面图。

    返回 (bytes, mime) 或 None（失败不缓存）。线程池会并发调用，无共享状态。
    视频优先用同目录封面图（cover/folder/poster 等，与扫描期 has_cover 检测
    同一组文件名），没有则由前端 canvas 抽帧后经 media_put_thumb 回写。
    """
    try:
        suffix = Path(src_path).suffix.lower()
        if suffix in AUDIO_EXTS:
            data = MetadataReader.extract_cover_bytes(Path(src_path))
            return (data, detect_image_mime(data)) if data else None
        if suffix in VIDEO_EXTS:
            data = MetadataReader._find_folder_cover(Path(src_path).parent, COVER_NAMES)
            return (data, detect_image_mime(data)) if data else None
    except Exception:
        pass
    return None


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


def scan_media(root: Path, cache: Optional[Dict[str, MediaItem]] = None,
               namespace: str = '') -> Dict:
    """递归扫描单个媒体根目录，mtime/size 未变时复用缓存。

    封面不再在扫描期生成/落盘：仅记录 has_cover，实际字节由
    ThumbCache + cover_generator 按需生成（/thumbs 路由触发）。
    """
    root = Path(root).resolve()
    cache = cache or {}
    items: Dict[str, MediaItem] = {}

    for current_dir, dir_names, file_names in os.walk(root):
        current = Path(current_dir)
        if current.name.startswith('.') or current.name == '.cache':
            dir_names[:] = []
            continue
        dir_names[:] = [d for d in dir_names if not d.startswith('.') and d != '.cache']

        rel_dir = current.relative_to(root).as_posix() if current != root else ''
        folder_cover = _find_cover(current)

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

            item_id = _item_id(str(full_path))
            cached = cache.get(item_id)

            # 目录封面图比媒体文件新（后添加/更新）时强制重建条目，刷新 has_cover
            try:
                cover_fresh = folder_cover is None or (
                    cached is not None and cached.mtime >= folder_cover.stat().st_mtime)
            except OSError:
                cover_fresh = True
            if cached and cached.path == str(full_path) \
                    and cached.size == stat.st_size and cached.mtime == stat.st_mtime \
                    and cover_fresh:
                item = cached
            else:
                if kind == 'audio':
                    meta = MetadataReader.read(full_path)
                    album = _text(meta.get('album'), current.name if current != root else (namespace or '未分类'))
                    album_artist = _text(meta.get('album_artist'), meta.get('artist') or '未知艺术家')
                    album_key = _audio_album_key(namespace, album, album_artist)
                    album_name = album
                    duration = float(meta.get('duration') or 0)
                    track = int(meta.get('track') or 0)

                    # 封面检测：目录封面图或内嵌封面（不落盘，字节懒生成）
                    has_cover = bool(folder_cover)
                    if not has_cover:
                        try:
                            has_cover = MetadataReader.has_embedded_cover(full_path)
                        except Exception:
                            has_cover = False

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
                        cover_path='',
                        has_cover=has_cover,
                        album_artist=album_artist,
                        track=track,
                    )
                else:
                    album_key = _video_album_key(namespace, rel_dir)
                    album_name = current.name if current != root else (namespace or '未分类')
                    duration = probe_duration(str(full_path)) or 0.0

                    # 视频始终可生成封面帧（目录封面图优先，否则按需抽帧）
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
                        cover_path='',
                        has_cover=True,
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
