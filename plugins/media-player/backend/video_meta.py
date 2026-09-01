"""视频时长探测：mutagen 轻量解析（mp4/m4v 等）。

无法解析的格式（如 mkv）返回 None，索引时长记为 0；
播放时前端 <video> 的 loadedmetadata 提供真实时长（UI 进度条即用），
扫描流程不受影响。
"""

from typing import Optional


def probe_duration(file_path: str) -> Optional[float]:
    """返回视频时长（秒）；无法解析时返回 None（播放时前端补真实时长）。"""
    path = str(file_path)
    try:
        from mutagen import File as MutagenFile
        meta = MutagenFile(path)
        if meta is not None:
            info = getattr(meta, 'info', None)
            length = getattr(info, 'length', None) if info else None
            if length:
                return float(length)
    except Exception:
        pass
    return None
