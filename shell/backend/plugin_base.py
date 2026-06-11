from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, Callable

class PluginBase(ABC):
    def __init__(self, manifest: dict, config: dict):
        self.manifest = manifest
        self.config = config
        self.name = manifest.get('name', 'unknown')

    @abstractmethod
    def register_api(self) -> Dict[str, Callable]:
        pass
    def get_data_root(self) -> Path:
        """返回该插件使用的数据根目录，默认使用全局配置"""
        return Path(self.config['directories']['data_root']).resolve()
    def on_load(self): pass
    def on_unload(self): pass