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
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Dict, List

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from shell.backend.plugin_manager import PluginManager
    from shell.backend.settings_store import SettingsStore

# 设置项 schema 的字段类型：text / number / range / select / checkbox / textarea / folder
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
