import os
import json
from pathlib import Path
from typing import List, Dict
from shell.backend.plugin_base import PluginBase

ALLOWED_EXTENSIONS = {
    '.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv',
    '.mp3', '.flac', '.wav', '.aac', '.ogg', '.wma'
}


class VideoPlayerPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "媒体库根目录", "type": "text",
         "placeholder": "默认: ./data",
         "help": "视频/音频文件所在根目录"},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self._resolved_config.get('root_dir')
        if root and Path(root).is_dir():
            self._root_dir = Path(root).resolve()
        else:
            self._root_dir = Path(self.config['directories']['data_root']).resolve()

    def get_data_root(self) -> Path:
        return self._root_dir

    @property
    def media_dir(self) -> Path:
        return self._root_dir

    def on_settings_changed(self, changed_keys):
        if 'root_dir' in changed_keys:
            new_dir = self.setting('root_dir')
            if new_dir and Path(new_dir).is_dir():
                self._root_dir = Path(new_dir).resolve()

    def _is_safe(self, rel_path: str) -> bool:
        try:
            target = (self.media_dir / rel_path).resolve()
            return str(target).startswith(str(self.media_dir))
        except Exception:
            return False

    def register_api(self) -> dict:
        return {
            'list_dir': self.list_dir,
            'list_media': self.list_media,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    def list_dir(self, rel_path: str = '') -> List[Dict]:
        if not self._is_safe(rel_path):
            return []
        target = self.media_dir / rel_path
        if not target.exists() or not target.is_dir():
            return []
        children = []
        try:
            for entry in os.scandir(target):
                if entry.is_dir() and not entry.name.startswith('.') and entry.name != '.cache':
                    child_path = (Path(rel_path) / entry.name).as_posix()
                    children.append({"name": entry.name, "path": child_path})
        except PermissionError:
            pass
        children.sort(key=lambda x: x['name'].lower())
        return children

    def list_media(self, rel_path: str = '') -> Dict:
        if not self._is_safe(rel_path):
            return {"dirs": [], "files": [], "path": rel_path}
        target = self.media_dir / rel_path
        if not target.exists() or not target.is_dir():
            return {"dirs": [], "files": [], "path": rel_path}
        dirs = []
        files = []
        try:
            for entry in os.scandir(target):
                if entry.name.startswith('.') or entry.name == '.cache':
                    continue
                if entry.is_dir():
                    child_path = (Path(rel_path) / entry.name).as_posix()
                    dirs.append({"name": entry.name, "path": child_path})
                elif entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                    stat = entry.stat()
                    files.append({
                        "name": entry.name,
                        "path": (Path(rel_path) / entry.name).as_posix(),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime
                    })
        except PermissionError:
            pass
        dirs.sort(key=lambda x: x['name'].lower())
        files.sort(key=lambda x: x['name'].lower())
        return {"dirs": dirs, "files": files, "path": rel_path}
