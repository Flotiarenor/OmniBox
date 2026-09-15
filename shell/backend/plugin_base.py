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

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from shell.backend.plugin_manager import PluginManager
    from shell.backend.settings_store import SettingsStore

# 设置项 schema 的字段类型：text / number / range / select / checkbox / textarea / directory
#   directory：目录列表（主要/额外 + 浏览…），由 Shell 共享组件 window.FolderPicker
#   渲染（shell/frontend/public/shell/folder-picker.js，与 image-viewer 同一份实现）；
#   值仍是字符串（multi 字段换行分隔），插件侧不用写代码，见 docs/plugin-guide.md §8.2
# 每个设置项示例：
#   {"key": "per_page", "label": "每页数量", "type": "number",
#    "default": 40, "min": 1, "max": 500, "help": "说明文字"}
#   {"key": "sort_by", "label": "排序方式", "type": "select",
#    "options": [{"label": "修改时间", "value": "mtime"}, {"label": "文件名", "value": "name"}]}
# 可选字段：
#   "central": False   — 不在集中设置面板显示（默认显示）
#   "help": "..."      — 设置面板悬浮提示

# 注意：docs/adapter-spec.md 中描述的 adapter_* 方法目前处于规划阶段，
# 尚未在本基类实现；adapter_process.py 也不应提前引入。


class PluginBase(ABC):
    # 子类覆盖：声明该插件的统一设置项。
    # ClassVar 是必要的：它是类级共享的常量声明，不是每个实例各自持有的可变状态
    # （不加 ClassVar 会被 RUF012 判为"把可变对象当类属性默认值"的隐患）。
    settings_schema: ClassVar[List[Dict[str, Any]]] = []

    def __init__(self, manifest: dict, config: dict) -> None:
        self.manifest = manifest
        self.config = config
        self.name = manifest.get('name', 'unknown')
        # 由 PluginManager 注入
        self._settings_store: SettingsStore | None = None
        self._plugin_manager: PluginManager | None = getattr(self.__class__, '_plugin_manager', None)
        # PluginManager 在构造前预加载的已解决设置
        self._resolved_config: Dict[str, Any] = getattr(self.__class__, '_resolved_config', {})
        # thumb_dir 的显式覆盖值：None 表示"跟随 get_data_root()"（见 thumb_dir property）。
        # 必须在 __init__ 里显式初始化：image-viewer 的 get_data_root() 读 self.root_dir，
        # 而 self.thumb_dir 的 getter 会回落到 get_data_root() —— 不在构造期先建好这个
        # 字段，插件 __init__ 里首次访问 thumb_dir 就可能读到尚不存在的属性。
        self._thumb_dir_override: Path | None = None

    @abstractmethod
    def register_api(self) -> Dict[str, Callable]:
        pass

    def get_data_root(self) -> Path:
        """返回该插件使用的数据根目录，默认使用全局配置"""
        return Path(self.config['directories']['data_root']).resolve()

    def get_file_roots(self) -> List[Path]:
        """返回文件服务允许访问的根目录列表。

        需要跨多个媒体目录提供文件的插件（如 media-player）可覆写本方法，
        返回所有已配置的根目录。`/files/` 路由会依次进行路径安全检查。
        """
        return [self.get_data_root()]

    # ===== 受保护路径契约（插件"申请"、Shell 执行）=====
    #
    # 定位：插件**自己申报**哪些路径是敏感内容，Shell 在文件路由上无条件拒绝把
    # 它们端出去。申报是可选能力 —— 不申报的插件行为完全不变。
    #
    # 为什么需要"申报"这一步：文件路由的放行依据是插件自己给出的根
    # （get_file_roots / thumb_dir），而根可以由插件设置改写。于是"根一旦覆盖了
    # 某个凭据文件，凭据就落在合法范围内"是结构性的，Shell 无法靠猜来避免。
    # 唯一可靠的信息来源就是插件自己说清楚 —— 但它只说"是什么"，不说"怎么防"，
    # 执行点始终在 Shell，插件侧没有任何绕过或关闭防护的接口。
    #
    # 与"限制插件怎么写"的区别：这**不**约束插件读写哪些文件（插件后端与 Shell
    # 同进程，任何声明都拦不住它直接读文件）。它只保证"插件自己不想要的泄露，
    # Shell 保证不会发生"。

    def get_protected_paths(self) -> List[Path]:
        """申请 Shell 文件防护：返回不得被 `/file`、`/files`、`/thumbs` 返回的路径。

        默认实现：`settings_schema` 里任何一项声明了 `"secret": True` 时，返回本
        插件的统一设置文件（凭据就存在那里）。因此**常见情况下一行都不用写** ——
        只需给对应的设置项加 `"secret": True`。

        插件若把敏感内容放在别处（自己的令牌文件、cookie jar、加密密钥等），覆写
        本方法返回那些路径即可，Shell 侧无需改动：

            def get_protected_paths(self):
                return [*super().get_protected_paths(), self.get_cache_dir() / 'token.json']

        约束与注意事项：

        - 只应返回**本插件自己的数据根目录或 `<config>/plugins` 之下**的路径。
          Shell 会校验这一边界，越界的申报被忽略并记 warning（防误用：一个笔误
          不该把整个文件服务钉死）。
        - 声明**目录**表示"该目录及其下全部内容"都受保护。
        - **不要**声明 `get_data_root()` 这样的宽目录：媒体的数据根往往就是媒体根，
          一起挡掉会让本插件自己的封面/缩略图也 404。只声明凭据文件。
        - 被拒绝的请求返回 `403`（与越界访问同一语义），所以插件前端不需要为它写
          特殊处理 —— 与已有的"403 就显示占位图"路径自然衔接。
        """
        if not any(isinstance(item, dict) and item.get('secret') for item in self.settings_schema):
            return []
        if self._settings_store is None:
            return []
        try:
            return [self._settings_store.path_for(self.name)]
        except (TypeError, ValueError) as e:
            log.warning(f"[{self.name}] 无法解析设置文件路径，凭据防护未生效: {e}")
            return []

    # ===== 缩略图契约（Shell 的 /thumbs 路由消费）=====
    #
    # 这三个成员曾经只由 file_server.py 以 getattr 探针隐式定义，既不在本基类、
    # 也没有测试固定形状：宿主侧把 thumb_dir 改成方法就能让 /thumbs 整体 500
    # （探针拿到 bound method 是真值 → 跳过回退 → Path(bound_method) 抛 TypeError），
    # 而 Companion 插件（image-cleaner）代理的正是这些未声明成员。
    # 现在它们是显式契约：形状、默认值与优先级都在这里定义，file_server 直接调用，
    # 不再做鸭子类型探测。

    def get_thumb_data(self, rel_path: str) -> Optional[Tuple[bytes, str]]:
        """返回缩略图字节，供 `/thumbs` 路由直接响应。默认返回 None（本插件不提供）。

        返回 `(data, mime)` 二元组；返回 None 时 `/thumbs` 回退到 `thumb_dir` 散文件布局。
        **优先级：本方法命中优先，未命中或返回 None 时才使用 thumb_dir。** 因此
        media-player 这类"只覆写 get_thumb_data、不定义 thumb_dir"的插件行为不变。
        """
        return None

    @property
    def thumb_dir(self) -> Path:
        """缩略图散文件目录，默认 `数据根目录/.cache/thumbs`。

        只读 property（契约要求二者择一，这里选 property）：默认值与 get_data_root()
        的派生关系固定，不允许各插件各拼一份路径。需要换目录的插件改 get_data_root()；
        也兼容旧写法 `self.thumb_dir = <path>` —— 赋值意味着"就用这个目录"，读取始终
        走本 property，宿主与插件不会看到两个不同的路径（见 setter）。
        """
        if self._thumb_dir_override is not None:
            return self._thumb_dir_override
        return self.get_data_root() / '.cache' / 'thumbs'

    @thumb_dir.setter
    def thumb_dir(self, value: Path | str | None) -> None:
        # 兼容旧插件的 `self.thumb_dir = <path>` 写法：image-viewer 在 __init__ 与
        # 切换 root_dir 时都会赋值。值按"完整目录路径"理解（赋什么就读到什么），
        # 传 None 表示恢复默认派生（跟随 get_data_root()）。
        self._thumb_dir_override = Path(value).resolve() if value is not None else None

    def ensure_thumb(self, rel_path: str) -> None:  # noqa: B027 - 有意的非抽象空钩子
        """按需生成缩略图（`/thumbs` 找不到文件时调用）。默认什么都不做。

        插件可覆写为"现场生成并落盘"；返回值被忽略，生成失败不应抛异常。
        """

    def get_dependency(self, name: str) -> PluginBase | None:
        """返回已加载依赖插件实例；未声明依赖或未加载时返回 None。"""
        dependencies = self.manifest.get('dependencies', []) or []
        if name not in dependencies:
            log.info(f"[{self.name}] 尝试访问未声明依赖的插件: {name}")
            return None
        if self._plugin_manager is None:
            return None
        return self._plugin_manager.get_plugin_instance(name)

    def get_extensions(self) -> List[dict]:
        """宿主前端可渲染的动作。默认空。"""
        return []

    def _default_settings(self) -> Dict[str, Any]:
        return {str(item["key"]): item.get("default") for item in self.settings_schema if item.get("key")}

    def get_settings(self) -> Dict[str, Any]:
        """从统一设置存储读取设置（合并默认值）。子类可覆盖，但必须调用 super() 以保证 on_settings_changed 检测正确"""
        stored = self._settings_store.get(self.name) if self._settings_store else {}
        return {**self._default_settings(), **stored}

    def save_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """校验、写入 SettingsStore、检测变更、调用 on_settings_changed。
        子类一般不需要覆盖此方法——覆盖 on_settings_changed() 即可响应设置变更。"""
        if not isinstance(settings, dict):
            return {"success": False, "error": "设置必须是字典"}

        old = self.get_settings()
        allowed = {item['key'] for item in self.settings_schema if item.get('key')}
        clean = {k: v for k, v in settings.items() if k in allowed} if allowed else settings

        if self._settings_store:
            try:
                # 必须"合并写入"而不是"整文件覆盖"：插件会用 update_setting() 把
                # 运行期状态（pixiv-sync 的 refresh_token、image-viewer 的 folders、
                # media-player 的 media_set_config 等）写进**同一个** JSON，这些键
                # 不在 settings_schema 里。整文件覆盖会让用户在设置面板点一次保存，
                # 就静默抹掉这些状态（不报错、无日志）——必须保留。
                merged = self._settings_store.update(self.name, clean)
            except Exception as e:
                return {"success": False, "error": f"保存失败: {e}"}
        else:
            merged = clean

        # 检测变更并通知插件：只上报本次提交涉及的键，但取值来自合并后的最终状态
        # （jsonify 之类的钩子可能补写额外键，旧实现同样会把它们算进 changed）。
        changed = set()
        for k in clean:
            new_val = merged.get(k)
            old_val = old.get(k)
            if isinstance(old_val, float) and isinstance(new_val, (int, float)):
                if abs(old_val - new_val) > 0.001:
                    changed.add(k)
            elif old_val != new_val:
                changed.add(k)
        for k in set(merged) - set(clean):
            if old.get(k) != merged[k]:
                changed.add(k)

        if changed:
            try:
                self.on_settings_changed(changed)
            except Exception as e:
                log.error(f"[{self.name}] on_settings_changed 异常: {e}")

        return {"success": True}

    def on_settings_changed(self, changed_keys: set) -> None:  # noqa: B027 - 可选钩子
        """设置变更时由 save_settings 自动调用。子类覆盖此方法以响应特定设置变更。

        示例:
            def on_settings_changed(self, changed_keys):
                if 'root_dir' in changed_keys:
                    self._reinit(self.setting('root_dir'))
        """

    def setting(self, key: str, default: Any = None) -> Any:
        """读取单个设置项。SettingsStore（运行时）→ _resolved_config（启动时预设）→ schema.default → 传入 default"""
        if self._settings_store:
            stored = self._settings_store.get(self.name)
            if key in stored:
                return stored[key]
        if key in self._resolved_config:
            return self._resolved_config[key]
        for item in self.settings_schema:
            if item.get('key') == key:
                return item.get('default')
        return default

    def update_setting(self, key: str, value: Any) -> bool:
        """更新单个设置项（保留其他设置不变），不校验 schema 以支持运行时状态持久化"""
        if self._settings_store:
            current = self._settings_store.get(self.name) or {}
            current[key] = value
            try:
                self._settings_store.set(self.name, current)
                return True
            except Exception:
                return False
        return False

    def clear_settings(self) -> Dict[str, Any]:
        """清空统一设置存储中的该插件设置"""
        if self._settings_store:
            self._settings_store.clear(self.name)
        return {"success": True}

    # on_load / on_unload 是**可选**钩子，因此刻意不加 @abstractmethod：
    # 插件只实现自己需要的那一个（只读插件没有收尾需求），强制实现反而会
    # 让最小插件多写两个空方法。
    def on_load(self) -> None:  # noqa: B027 - 有意的非抽象空钩子
        pass

    def on_unload(self) -> None:  # noqa: B027 - 有意的非抽象空钩子
        pass
