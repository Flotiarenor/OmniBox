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

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, Callable, List

# 设置项 schema 的字段类型：text / number / range / select / checkbox / textarea / folder
# 每个设置项示例：
#   {"key": "per_page", "label": "每页数量", "type": "number",
#    "default": 40, "min": 1, "max": 500, "help": "说明文字"}
#   {"key": "sort_by", "label": "排序方式", "type": "select",
#    "options": [{"label": "修改时间", "value": "mtime"}, {"label": "文件名", "value": "name"}]}

class PluginBase(ABC):
    # 子类覆盖：声明该插件的统一设置项
    settings_schema: List[Dict] = []

    def __init__(self, manifest: dict, config: dict):
        self.manifest = manifest
        self.config = config
        self.name = manifest.get('name', 'unknown')
        # 由 PluginManager 注入
        self._settings_store = None

    @abstractmethod
    def register_api(self) -> Dict[str, Callable]:
        pass
    def get_data_root(self) -> Path:
        """返回该插件使用的数据根目录，默认使用全局配置"""
        return Path(self.config['directories']['data_root']).resolve()

    def _default_settings(self) -> Dict:
        return {item.get('key'): item.get('default') for item in self.settings_schema if item.get('key')}

    def get_settings(self) -> Dict:
        """从统一设置存储读取设置（合并默认值），子类可覆盖实现自定义逻辑"""
        stored = self._settings_store.get(self.name) if self._settings_store else {}
        return {**self._default_settings(), **stored}

    def save_settings(self, settings: Dict) -> Dict:
        """校验并写入统一设置存储，子类可覆盖实现自定义逻辑"""
        if not isinstance(settings, dict):
            return {"success": False, "error": "设置必须是字典"}
        allowed = {item['key'] for item in self.settings_schema if item.get('key')}
        clean = {k: v for k, v in settings.items() if k in allowed} if allowed else settings
        if self._settings_store:
            try:
                self._settings_store.set(self.name, clean)
            except Exception as e:
                return {"success": False, "error": f"保存失败: {e}"}
        return {"success": True}

    def clear_settings(self) -> Dict:
        """清空统一设置存储中的该插件设置"""
        if self._settings_store:
            self._settings_store.clear(self.name)
        return {"success": True}

    def on_load(self): pass
    def on_unload(self): pass