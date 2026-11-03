"""媒体条目构造：音频 / 视频 → `MediaItem`（目录遍历已交给统一刷新基建）。

- 音频专辑按 ID3/Flac/MP4 标签中的「专辑 + 专辑艺术家」聚合；
  `has_cover` 只做检测，封面字节由 ThumbCache 懒生成（见 `cover_generator`）。
- 视频专辑按目录聚合；`has_cover` 恒为 True（目录封面图优先，否则由前端
  canvas 抽帧后经 `media_put_thumb` 回写缓存）。
- **遍历与"哪些文件要重算"不在这里**：统一刷新基建（`shell/backend/freshness.py`）
  负责目录级短路、指纹比对、幽灵清理，只把新增/变化的文件交给本模块构造。

依赖全部由入口注入（`models` / `metadata` / `video_meta` / `ffmpeg` 四个兄弟模块）：
插件后端不是包、目录不在 `sys.path` 上，组件之间不互相 import。
"""

import hashlib
from pathlib import Path
from typing import Any, FrozenSet, Optional

#: 索引结构与解析规则的版本。升级它 = 让统一刷新基建整体作废派生数据后重建。
INDEX_VERSION = 4

#: 视频扩展名（音频扩展名以 `metadata.SUPPORTED_EXTS` 为准，构造时取一次）。
VIDEO_EXTS: FrozenSet[str] = frozenset(
    {'.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv'})

#: 目录封面图文件名。与 `metadata.COVER_NAMES` 是两个口径：这里还认海报 / 缩略图类
#: 命名（视频目录常见），并且要参与指纹（`freshness.include_names`）。
COVER_NAMES: FrozenSet[str] = frozenset({
    'cover.jpg', 'cover.png', 'folder.jpg', 'folder.png',
    'poster.jpg', 'poster.png', 'fanart.jpg', 'fanart.png',
    'thumb.jpg', 'thumb.png', 'backdrop.jpg', 'backdrop.png',
})


def item_id(path: str) -> str:
    """条目 id：绝对路径的 md5 前 16 位（索引、缩略图库、前端都以此为键）。"""
    return hashlib.md5(path.encode('utf-8')).hexdigest()[:16]


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


class MediaScanner:
    """把一个媒体文件构造成 `MediaItem`；也提供封面字节生成器。"""

    def __init__(self, models: Any, metadata: Any, video_meta: Any,
                 ffmpeg: Any) -> None:
        self._models = models
        self._metadata = metadata
        self._video_meta = video_meta
        self._ffmpeg = ffmpeg
        self.audio_exts: FrozenSet[str] = frozenset(metadata.SUPPORTED_EXTS)
        self.video_exts: FrozenSet[str] = VIDEO_EXTS
        self.cover_names: FrozenSet[str] = COVER_NAMES
        self.media_exts: FrozenSet[str] = self.audio_exts | self.video_exts
        self.index_version: int = INDEX_VERSION

    # ===== 条目键与封面 =====

    @staticmethod
    def item_id(path: str) -> str:
        return item_id(path)

    def find_cover(self, directory: Path) -> Optional[Path]:
        """目录封面图（与 `cover_generator` 同一组文件名）。"""
        try:
            for name in self.cover_names:
                candidate = directory / name
                if candidate.is_file():
                    return candidate
        except OSError:
            pass
        return None

    def cover_generator(self, src_path: Path) -> Optional[tuple]:
        """ThumbCache 自定义生成器：音频内嵌封面 / 视频目录封面图 / ffmpeg 抽帧。

        返回 (bytes, mime) 或 None（失败不缓存）。线程池会并发调用，无共享可变状态。
        视频优先级：同目录封面图（零成本）→ ffmpeg 后端抽帧（可选通道，路径来自
        设置 ffmpeg_path 或 PATH）→ 都没有则返回 None，由前端 canvas 抽帧兜底。
        """
        try:
            suffix = Path(src_path).suffix.lower()
            if suffix in self.audio_exts:
                data = self._metadata.MetadataReader.extract_cover_bytes(Path(src_path))
                return (data, self._metadata.detect_image_mime(data)) if data else None
            if suffix in self.video_exts:
                # 1. 目录封面图（与扫描期 has_cover 检测同一组文件名）
                data = self._metadata.MetadataReader._find_folder_cover(
                    Path(src_path).parent, self.cover_names)
                if data:
                    return (data, self._metadata.detect_image_mime(data))
                # 2. ffmpeg 后端抽帧（未配置/失败则交给前端兜底）
                frame = self._ffmpeg.extract_frame(
                    str(src_path), duration=self._video_meta.probe_duration(str(src_path)))
                return (frame, 'image/jpeg') if frame else None
        except Exception:
            pass
        return None

    def has_embedded_cover(self, file_path: Path) -> bool:
        """该文件是否有内嵌封面（扫描期与封面失效重算共用，不落盘）。"""
        return bool(self._metadata.MetadataReader.has_embedded_cover(file_path))

    # ===== 构造条目 =====

    def build_item(self, full_path: Path, root: Path, namespace: str, rel_dir: str,
                   stat: Any, folder_cover: Optional[Path]) -> Optional[Any]:
        """把一个媒体文件构造成 `MediaItem`；不是媒体文件时返回 None。

        这是本插件唯一"昂贵"的操作（音频读标签、视频探测时长），因此调用点只有
        统一刷新基建的 `derive` 钩子 —— 它只对新增/指纹变化的文件调用。
        """
        suffix = full_path.suffix.lower()
        if suffix in self.audio_exts:
            kind = 'audio'
        elif suffix in self.video_exts:
            kind = 'video'
        else:
            return None

        iid = item_id(str(full_path))
        if kind == 'audio':
            meta = self._metadata.MetadataReader.read(full_path)
            directory = full_path.parent
            album = _text(meta.get('album'),
                          directory.name if directory != root else (namespace or '未分类'))
            album_artist = _text(meta.get('album_artist'), meta.get('artist') or '未知艺术家')
            # 封面检测：目录封面图或内嵌封面（不落盘，字节懒生成）
            has_cover = bool(folder_cover)
            if not has_cover:
                try:
                    has_cover = self._metadata.MetadataReader.has_embedded_cover(full_path)
                except Exception:
                    has_cover = False
            return self._models.MediaItem(
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
        return self._models.MediaItem(
            id=iid,
            path=str(full_path),
            kind=kind,
            title=full_path.stem,
            artist=album_name,
            album=album_name,
            album_key=_video_album_key(namespace, rel_dir),
            duration=self._video_meta.probe_duration(str(full_path)) or 0.0,
            size=stat.st_size,
            mtime=stat.st_mtime,
            cover_path='',
            # 视频始终可生成封面帧（目录封面图优先，否则按需抽帧）
            has_cover=True,
            album_artist=album_name,
            track=0,
        )
