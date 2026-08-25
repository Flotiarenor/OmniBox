from shell.backend.plugin_base import PluginBase


class __CLASS__(PluginBase):
    settings_schema = [
        {"key": "greeting", "label": "问候语", "type": "text",
         "default": "Hello, OmniBox!"},
    ]

    def register_api(self):
        return {
            'greet': self.greet,
        }

    def greet(self, name=None):
        greeting = self.setting('greeting', 'Hello, OmniBox!')
        who = name or 'OmniBox'
        return f"{greeting} （来自后端 {who} 的问候）"
