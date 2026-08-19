from pathlib import Path
from typing import List, Dict
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling



_media = load_sibling(__file__, 'media', 'video_player')
is_safe_path = _media.is_safe_path
list_directory = _media.list_directory
list_media_files = _media.list_media

class VideoPlayerPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "媒体库根目录", "type": "text",
         "placeholder": "默认: ./data",
         "help": "视频/音频文件所在根目录"},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir')
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
        return is_safe_path(self.media_dir, rel_path)


    def register_api(self) -> dict:
        return {
            'list_dir': self.list_dir,
            'list_media': self.list_media,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    def list_dir(self, rel_path: str = '') -> List[Dict]:
        return list_directory(self.media_dir, rel_path)


    def list_media(self, rel_path: str = '') -> Dict:
        return list_media_files(self.media_dir, rel_path)
