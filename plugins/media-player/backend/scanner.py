"""媒体条目构造：音频 / 视频 → `MediaItem`（目录遍历已交给统一刷新基建）。

- 音频专辑按 ID3/Flac/MP4 标签中的「专辑 + 专辑艺术家」聚合；
  `has_cover` 只做检测，封面字节由 ThumbCache 懒生成（见 cover_generator）。
- 视频专辑按目录聚合；`has_cover` 恒为 True（目录封面图优先，否则由前端
  canvas 抽帧后经 `media_put_thumb` 回写缓存）。
- **遍历与"哪些文件要重算"不在这里**：统一刷新基建（`shell/backend/freshness.py`）
  负责目录级短路、指纹比对、幽灵清理，只把新增/变化的文件交给本模块构造。
  上一版在这里做 `os.walk` + mtime/size 缓存，"删除的文件永久留在索引里"
  与"深度扫描要重读全库标签"都源自那份自己实现的判定。
"""

import hashlib
from pathlib import Path
from typing import Optional

from shell.backend.plugin_utils import load_sibling

_metadata = load_sibling(__file__, 'metadata', 'media_player')
_models = load_sibling(__file__, 'models', 'media_player')
_video_meta = load_sibling(__file__, 'video_meta', 'media_player')
_ffmpeg = load_sibling(__file__, 'video_ffmpeg', 'media_player')

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
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS


def cover_generator(src_path: Path) -> Optional[tuple]:
    """ThumbCache 自定义生成器：音频内嵌封面 / 视频目录封面图 / ffmpeg 抽帧。

    返回 (bytes, mime) 或 None（失败不缓存）。线程池会并发调用，无共享状态。
    视频优先级：同目录封面图（零成本）→ ffmpeg 后端抽帧（可选通道，路径来自
    设置 ffmpeg_path 或 PATH）→ 都没有则返回 None，由前端 canvas 抽帧兜底。
    """
    try:
        suffix = Path(src_path).suffix.lower()
        if suffix in AUDIO_EXTS:
            data = MetadataReader.extract_cover_bytes(Path(src_path))
            return (data, detect_image_mime(data)) if data else None
        if suffix in VIDEO_EXTS:
            # 1. 目录封面图（与扫描期 has_cover 检测同一组文件名）
            data = MetadataReader._find_folder_cover(Path(src_path).parent, COVER_NAMES)
            if data:
                return (data, detect_image_mime(data))
            # 2. ffmpeg 后端抽帧（未配置/失败则交给前端兜底）
            frame = _ffmpeg.extract_frame(str(src_path),
                                          duration=probe_duration(str(src_path)))
            return (frame, 'image/jpeg') if frame else None
    except Exception:
        pass
    return None


def item_id(path: str) -> str:
    """条目 id：绝对路径的 md5 前 16 位（索引、缩略图库、前端都以此为键）。"""
    return hashlib.md5(path.encode('utf-8')).hexdigest()[:16]


def find_cover(directory: Path) -> Optional[Path]:
    """目录封面图（与 `cover_generator` 同一组文件名）。"""
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


def build_item(full_path: Path, root: Path, namespace: str, rel_dir: str,
               stat, folder_cover: Optional[Path]) -> Optional[MediaItem]:
    """把一个媒体文件构造成 `MediaItem`；不是媒体文件时返回 None。

    这是本插件唯一"昂贵"的操作（音频读标签、视频探测时长），因此调用点只有
    统一刷新基建的 `derive` 钩子 —— 它只对新增/指纹变化的文件调用。
    """
    suffix = full_path.suffix.lower()
    if suffix in AUDIO_EXTS:
        kind = 'audio'
    elif suffix in VIDEO_EXTS:
        kind = 'video'
    else:
        return None

    iid = item_id(str(full_path))
    if kind == 'audio':
        meta = MetadataReader.read(full_path)
        directory = full_path.parent
        album = _text(meta.get('album'), directory.name if directory != root else (namespace or '未分类'))
        album_artist = _text(meta.get('album_artist'), meta.get('artist') or '未知艺术家')
        # 封面检测：目录封面图或内嵌封面（不落盘，字节懒生成）
        has_cover = bool(folder_cover)
        if not has_cover:
            try:
                has_cover = MetadataReader.has_embedded_cover(full_path)
            except Exception:
                has_cover = False
        return MediaItem(
            id=iid,
            path=str(full_path),
            kind=kind,
            title=_text(meta.get('title'), full_path.stem),
            artist=_text(meta.get('artist'), '未知艺术家'),
            album=album,
            album_key=_audio_album_key(namespace, album, album_artist),
            duration=float(meta.get('duration') or 0),
            size=stat.st_size,
            mtime=stat.st_mtime,
            cover_path='',
            has_cover=has_cover,
            album_artist=album_artist,
            track=int(meta.get('track') or 0),
        )

    album_name = full_path.parent.name if full_path.parent != root else (namespace or '未分类')
    return MediaItem(
        id=iid,
        path=str(full_path),
        kind=kind,
        title=full_path.stem,
        artist=album_name,
        album=album_name,
        album_key=_video_album_key(namespace, rel_dir),
        duration=probe_duration(str(full_path)) or 0.0,
        size=stat.st_size,
        mtime=stat.st_mtime,
        cover_path='',
        # 视频始终可生成封面帧（目录封面图优先，否则按需抽帧）
        has_cover=True,
        album_artist=album_name,
        track=0,
    )
