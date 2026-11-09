"""插件入口：设置 schema、API 表、壳按名调用的钩子与装配。

本文件只做三件事：声明对壳暴露什么、把组件装配起来、把钩子一行委派给组件。
业务逻辑不写在这里 —— 门禁 `tools/check_plugin_layering.py` 会拦住两类退化：
用 Mixin 把类切成多个文件（R1），以及入口文件越过行数上限（R2）。目标形态与
迁移步骤见 `docs/plugin-backend-layout.md`。
"""

from typing import Any, ClassVar, Dict, List

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

# 组件模块由入口加载：插件后端不是包、目录不在 sys.path 上，只有入口能 load_sibling。
_greeting = load_sibling(__file__, 'greeting', '__NAMESPACE__')


class __CLASS__(PluginBase):
    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        {"key": "greeting", "label": "问候语", "type": "text",
         "default": "Hello, Cubby!"},
    ]

    def __init__(self, manifest: dict, config: dict) -> None:
        super().__init__(manifest, config)
        # 组件接收依赖，而不是互相 import：用例里传一个假的 settings 就能单独测组件。
        self.greeter = _greeting.Greeter(self)

    def register_api(self) -> dict:
        return {
            'greet': self.greeter.greet,
        }
