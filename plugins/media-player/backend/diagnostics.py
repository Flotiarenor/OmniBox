"""标签解析诊断与 ffmpeg 可用性探测。

无状态：条目来自 `index`，ffmpeg 通道是模块级可选设施，这里只读它的探测结果。

`metadata` 组件按**调用时机**延迟获取：`metadata_provider` 是入口给的闭包，只在
`debug_meta()` 被真正调用时才执行。这条时机语义是有意的 —— 诊断属于低频调试路径，
不该让它的模块依赖在插件装配期就成立（装配期拿不到它时插件仍然完整可用，只是这条
诊断入口回报"不可用"）。
"""

import logging
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class AudioDiagnostics:
    """媒体条目与标签解析的诊断出口。"""

    def __init__(self, index: Any, settings: Any, ffmpeg: Any,
                 metadata_provider: Callable[[], Any]) -> None:
        self._index = index
        self._settings = settings
        self._ffmpeg = ffmpeg
        self._metadata_provider = metadata_provider

    def _metadata(self) -> Optional[Any]:
        """按调用时机获取 `metadata` 模块；拿不到时返回 None（诊断入口报不可用）。"""
        try:
            return self._metadata_provider()
        except Exception as error:
            log.warning(f'[MediaPlayer] metadata 模块不可用: {error}')
            return None

    def debug_meta(self, item_id: str = '') -> dict:
        """报一条媒体的实际标签解析结果（`media_debug_meta`）。"""
        if not item_id:
            return {'error': '请提供 item_id 参数'}
        item = self._index.get(item_id)
        if item is None:
            return {'error': f'媒体不存在: {item_id}'}
        file_path = Path(item.path)
        if not file_path.exists():
            return {'error': f'文件不存在: {file_path}'}
        metadata = self._metadata()
        if metadata is None:
            return {'error': 'metadata 模块不可用'}
        result = metadata.MetadataReader.debug_meta(file_path)
        result['parsed'] = item.to_dict()
        return result

    def ffmpeg_status(self) -> dict:
        """ffmpeg 可用性探测（force 重新检测，供设置/调试确认）。"""
        path = self._ffmpeg.find_ffmpeg(force=True)
        return {
            'available': bool(path),
            'path': path or '',
            'configured': self._settings.setting('ffmpeg_path') or '',
        }
