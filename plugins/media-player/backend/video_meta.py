"""视频元数据探测：PyAV → mutagen → ffprobe 逐级回退，
并提供 ffmpeg / PyAV 两种视频封面帧抽取能力。

任何一级失败都静默降级，保证扫描流程不会因单个文件的探测问题中断。
"""

import subprocess
import shutil
from typing import Optional


def _ffmpeg_exe() -> Optional[str]:
    return shutil.which('ffmpeg')


def probe_duration(file_path: str) -> Optional[float]:
    """返回视频时长（秒）；无法解析时返回 None。"""
    path = str(file_path)

    # 1. PyAV（最可靠，但体积大 / 偶发 DLL 占用）
    av = None
    try:
        import av
    except Exception:
        av = None
    if av is not None:
        try:
            with av.open(path) as container:
                duration = container.duration
                if duration:
                    return float(duration) / 1_000_000.0
        except Exception:
            pass

    # 2. mutagen（mp4/m4v 等格式轻量解析）
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

    # 3. ffprobe（mkv 等特殊封装）
    try:
        completed = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, timeout=20)
        value = (completed.stdout or '').strip()
        if value:
            return float(value)
    except Exception:
        pass

    return None


def extract_thumbnail(file_path: str, out_path: str, at_seconds: float = 10.0,
                      width: int = 640) -> bool:
    """从视频中截取一帧保存为 JPEG 封面。

    优先使用 ffmpeg（速度快、格式支持全），ffmpeg 不可用时回退到 PyAV。
    返回是否成功生成目标图片。
    """
    path = str(file_path)
    target = str(out_path)
    seek = max(0.0, float(at_seconds or 0))

    # 1. ffmpeg：只解到目标时间点的一帧，不采集音频
    exe = _ffmpeg_exe()
    if exe:
        try:
            subprocess.run(
                [exe, '-y', '-loglevel', 'error',
                 '-ss', f'{seek:.2f}',
                 '-i', path,
                 '-frames:v', '1',
                 '-vf', f'scale={int(width)}:-2',
                 '-q:v', '3',
                 target],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60, check=True)
            import os
            if os.path.isfile(target) and os.path.getsize(target) > 0:
                return True
        except Exception:
            pass

    # 2. PyAV 回退：解码到目标时间点附近的第一帧
    try:
        import av
        with av.open(path) as container:
            stream = next((s for s in container.streams if s.type == 'video'), None)
            if stream is None:
                return False
            target_ts = int(seek / float(stream.time_base or 1))
            for frame in container.decode(stream):
                if frame.pts is not None and frame.pts >= target_ts:
                    frame.to_image().save(target)
                    return True
                if frame.pts is not None and frame.pts > target_ts + 15 * 60 * int(1 / float(stream.time_base or 1)):
                    break
    except Exception:
        pass

    return False
