"""视频封面 ffmpeg 后端抽取器（可选通道）。

插件设置 ffmpeg_path（或 PATH 中检出 ffmpeg）时，后端可直接抽帧写入
ThumbCache——`/thumbs` 一次命中，前端 canvas 抽帧自动降级为兜底；
找不到 ffmpeg 时本模块返回 None，走原有前端链路，行为与之前完全一致。

设计要点：
- `-ss` 置于 `-i` 之前：关键帧快速 seek（缩略图足够，成本远低于整段解码）；
- 线程安全：`configure()` 仅在插件启动/设置变更时写入，worker 只读；
- 探测结果缓存：成功/失败均缓存，`configure()` 或 `find_ffmpeg(force=True)`
  时重新探测（供设置页「重新检测」类场景）。
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

_configured_path = ''       # 用户设置注入的路径（可空）
_found: Optional[str] = None
_found_valid = False        # 缓存是否有效（configure 后失效）

SEEK_DEFAULT = 10.0         # 时长未知时取 10s（与前端抽帧一致）
SEEK_MIN = 0.5
SEEK_MAX = 120.0
FRAME_TIMEOUT = 30          # 单帧抽取超时（秒）


def configure(path: str) -> None:
    """设置变更时注入用户填写的 ffmpeg 路径（可为空字符串）。"""
    global _configured_path, _found, _found_valid
    _configured_path = (path or '').strip()
    _found = None
    _found_valid = False


def find_ffmpeg(force: bool = False) -> Optional[str]:
    """定位 ffmpeg。语义：配置了 ffmpeg_path 就用配置（无效则视为不可用，
    不静默回落 PATH，避免设置项形同虚设）；留空才检测 PATH。

    force=True 时绕过缓存重新探测（配置探测 / 调试确认用）。
    """
    global _found, _found_valid
    if _found_valid and not force:
        return _found
    p = _configured_path
    if p:
        cand = Path(p).expanduser()
        found = None
        if cand.is_file():
            found = str(cand)
        elif sys.platform == 'win32':
            if cand.is_dir():
                exe = cand / 'ffmpeg.exe'
                if exe.is_file():
                    found = str(exe)
            elif not cand.suffix:
                exe = cand.with_suffix('.exe')
                if exe.is_file():
                    found = str(exe)
        _found = found
        _found_valid = True
        return found
    exe = shutil.which('ffmpeg')
    _found = exe
    _found_valid = True
    return exe


def extract_frame(video_path: str, duration: Optional[float] = None) -> Optional[bytes]:
    """抽取一帧 JPEG 字节；ffmpeg 不可用或失败时返回 None（调用方走兜底）。

    duration 已知时取 10%（钳 [0.5, 120]）；未知时依次尝试 10s/3s/1s
    （覆盖短视频，行为优于前端隐藏 video 的固定 10s）。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    if duration and duration > 0:
        seeks = [max(SEEK_MIN, min(duration * 0.1, SEEK_MAX))]
    else:
        seeks = [SEEK_DEFAULT, 3.0, 1.0]
    for seek in seeks:
        data = _run(ffmpeg, video_path, seek)
        if data:
            return data
    return None


def _run(ffmpeg: str, video_path: str, seek: float) -> Optional[bytes]:
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
           '-ss', f'{seek:.3f}', '-i', str(video_path),
           '-frames:v', '1', '-q:v', '3', '-f', 'image2pipe', '-']
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout
