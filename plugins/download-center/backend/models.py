"""下载任务数据模型与序列化。"""

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class DownloadTask:
    id: str
    album_id: str
    title: str = ''
    thumb_url: str = ''
    status: str = 'queued'  # queued/downloading/paused/completed/failed
    total_images: int = 0
    completed_images: int = 0
    speed: float = 0.0  # bytes/s
    eta: int = 0  # seconds
    priority: str = 'normal'  # high/normal/low
    download_dir: str = ''
    error: str = ''
    start_time: str = ''
    complete_time: str = ''
    concurrency: int = 3
    chapters: List[Dict] = field(default_factory=list)
    _thread: Optional[threading.Thread] = None
    _stop_event: threading.Event = field(default_factory=threading.Event)

    def to_api_dict(self, detail: bool = False) -> Dict:
        result = {
            'id': self.id,
            'albumId': self.album_id,
            'title': self.title,
            'thumbUrl': self.thumb_url,
            'status': self.status,
            'totalImages': self.total_images,
            'completedImages': self.completed_images,
            'speed': self.speed,
            'eta': self.eta,
            'priority': self.priority,
            'downloadDir': self.download_dir,
            'error': self.error,
            'startTime': self.start_time,
            'completeTime': self.complete_time,
        }
        if detail:
            result['chapters'] = self.chapters
            result['concurrency'] = self.concurrency
        return result

    def to_state_dict(self) -> Dict:
        return {
            'id': self.id,
            'album_id': self.album_id,
            'title': self.title,
            'thumb_url': self.thumb_url,
            'status': self.status,
            'total_images': self.total_images,
            'completed_images': self.completed_images,
            'priority': self.priority,
            'download_dir': self.download_dir,
            'error': self.error,
            'start_time': self.start_time,
            'complete_time': self.complete_time,
            'concurrency': self.concurrency,
        }

    @classmethod
    def from_state_dict(cls, data: Dict) -> 'DownloadTask':
        task = cls(
            id=data['id'],
            album_id=data['album_id'],
            title=data.get('title', ''),
            thumb_url=data.get('thumb_url', ''),
            status=data.get('status', 'paused'),
            total_images=data.get('total_images', 0),
            completed_images=data.get('completed_images', 0),
            priority=data.get('priority', 'normal'),
            download_dir=data.get('download_dir', ''),
            error=data.get('error', ''),
            start_time=data.get('start_time', ''),
            complete_time=data.get('complete_time', ''),
            concurrency=data.get('concurrency', 3),
        )
        # 程序退出前正在下载的任务，重启后统一按暂停处理。
        if task.status == 'downloading':
            task.status = 'paused'
        return task
