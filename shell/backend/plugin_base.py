from abc import ABC, abstractmethod
from typing import Dict, Any

class PluginBase(ABC):
    def __init__(self, manifest: dict, config: dict):
        self.manifest = manifest
        self.config = config
        self.name = manifest.get('name', 'unknown')

    @abstractmethod
    def register_api(self) -> Dict[str, callable]:
        pass

    def on_load(self): pass
    def on_unload(self): pass