'''
Copyright 2026 flotiarenor

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

import json
from pathlib import Path
from typing import Dict


class SettingsStore:
    """统一的插件设置存储，每个插件一个 JSON 文件，位于 <config_dir>/<plugin>.json"""

    def __init__(self, settings_dir: str):
        self.settings_dir = Path(settings_dir)
        self.settings_dir.mkdir(parents=True, exist_ok=True)

    def _file(self, plugin_name: str) -> Path:
        return self.settings_dir / f"{plugin_name}.json"

    def get(self, plugin_name: str) -> Dict:
        file = self._file(plugin_name)
        if not file.exists():
            return {}
        try:
            with open(file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def set(self, plugin_name: str, values: Dict):
        if not isinstance(values, dict):
            raise ValueError("设置必须是字典")
        file = self._file(plugin_name)
        with open(file, 'w', encoding='utf-8') as f:
            json.dump(values, f, indent=2, ensure_ascii=False)

    def clear(self, plugin_name: str):
        file = self._file(plugin_name)
        if file.exists():
            file.unlink()

    def update(self, plugin_name: str, values: Dict):
        current = self.get(plugin_name)
        current.update(values)
        self.set(plugin_name, current)
