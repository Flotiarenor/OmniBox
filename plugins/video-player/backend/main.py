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
        self._migrate_old_settings()

    def _migrate_old_settings(self):
        old_file = Path(__file__).parent.parent / 'settings.json'
        if not old_file.exists():
            return
        try:
            with open(old_file, 'r', encoding='utf-8') as f:
                old = json.load(f)
            old_root = old.get('root_dir')
            if old_root and self._settings_store:
                current = self._settings_store.get(self.name) or {}
                if 'root_dir' not in current:
                    self._settings_store.set(self.name, {**current, 'root_dir': old_root})
            old_file.unlink()
        except Exception:
            pass

    def get_data_root(self) -> Path:
        settings = self.get_settings()
        root_dir = settings.get('root_dir')
        if root_dir:
            return Path(root_dir).resolve()
        return Path(self.config['directories']['data_root']).resolve()

    @property
    def media_dir(self) -> Path:
        return self.get_data_root()

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

    def get_settings(self) -> Dict:
        settings = super().get_settings()
        return {k: v for k, v in settings.items() if v is not None}

    def save_settings(self, settings: Dict) -> Dict:
        result = super().save_settings(settings)
        return result