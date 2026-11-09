"""问候组件：业务逻辑在这里。

三条约束（门禁 `tools/check_plugin_layering.py` 会核对）：

- 不 import 兄弟模块、不调用 `load_sibling`：依赖由入口通过构造参数注入；
- 不 import `PluginBase`：只依赖 `shell.backend.ports` 里的 Protocol，因此可以在用例里
  单独构造（传一个 dict 支撑的假对象即可）；
- 状态自持：这个组件没有可变状态，需要缓存时就地声明在这里，不要挂回入口。
"""

from typing import Optional

from shell.backend.ports import SettingsPort


class Greeter:
    def __init__(self, settings: SettingsPort, default: str = 'Hello, OmniBox!') -> None:
        self._settings = settings
        self._default = default

    def greet(self, name: Optional[str] = None) -> str:
        greeting = self._settings.setting('greeting', self._default)
        who = name or 'OmniBox'
        return f'{greeting} （来自后端 {who} 的问候）'
