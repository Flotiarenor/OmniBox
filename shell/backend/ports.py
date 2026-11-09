"""插件组件与宿主之间最小的能力契约。

为什么需要
----------
插件后端组件化之后，组件需要"读一个设置项""写一个设置项"，但它不应该 import
`PluginBase`：那会把组件绑死在插件基类上，用例里就必须先造一个完整插件才能测一个
纯逻辑组件（现在正是这样：要测一个路径函数，得先喂 manifest 与 config）。

这里只声明组件真正用到的两三个方法，`PluginBase` 结构化地满足它们（有同名同签名的方法
就算满足，不需要显式继承）。于是：

* 组件的构造参数写 `settings: SettingsPort`，静态检查可以核对调用点传进来的东西；
* 用例里传一个 `{'root_dir': '...'}` 支撑的假对象即可，不需要启动插件；
* 端口定义放在共享包里而不是每个插件各写一份 —— 这是**所有插件都相同**的契约，
  写 8 遍只会得到 8 个漂移的版本。

为什么不把端口放进插件目录：插件后端不是包、目录不在 `sys.path` 上，组件之间不能
互相 import（门禁 `tools/check_plugin_layering.py` 的 R3 会拦）。共享端口在
`shell.backend` 下才有真正的 import 路径，也才被 `HIDDEN_IMPORTS` 罩住。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class SettingsPort(Protocol):
    """读单个设置项。由 `PluginBase.setting` 满足。

    `setting` 的取值顺序是"设置存储 → 启动时预设 → schema 默认值 → 传入默认值"，
    组件不需要知道这些层级，只按"拿不到就用 default"使用。
    """

    def setting(self, key: str, default: Any = None) -> Any:
        ...


@runtime_checkable
class PersistPort(Protocol):
    """写设置。由 `PluginBase.update_setting` / `PluginBase.save_settings` 满足。

    两个方法语义不同，都要：`update_setting` 写运行期状态（不进 schema、不校验），
    `save_settings` 走 schema 校验与字段级错误返回。组件按需要选，不要自己拼写盘。
    """

    def update_setting(self, key: str, value: Any) -> bool:
        ...

    def save_settings(self, values: Mapping[str, Any]) -> dict:
        ...
