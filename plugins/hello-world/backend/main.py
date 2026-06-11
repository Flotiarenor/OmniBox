from shell.backend.plugin_base import PluginBase

class HelloPlugin(PluginBase):
    def register_api(self):
        return {
            'greet': lambda name: f"Hello, {name} from backend!"
        }