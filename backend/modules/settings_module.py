import json
import threading
from pathlib import Path
from typing import Dict


class SettingsModule:
    def __init__(self, config_path: str = 'config.json'):
        self.config_path = Path(config_path)
        self._lock = threading.Lock()

    def _load(self) -> Dict:
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "global": {"row_height": 200, "per_page": 40, "sort_by": "mtime", "sort_order": "desc"},
            "folders": {}
        }

    def _save(self, config: Dict):
        with self._lock:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)

    def get(self, folder_path: str = '') -> Dict:
        config = self._load()
        global_cfg = config.get('global', {})
        folder_cfg = config.get('folders', {}).get(folder_path, {})
        return {**global_cfg, **folder_cfg}

    def save(self, folder_path: str, settings: Dict):
        config = self._load()
        if folder_path:
            if 'folders' not in config:
                config['folders'] = {}
            config['folders'][folder_path] = settings
        else:
            config['global'] = settings
        self._save(config)
        return {"status": "ok"}