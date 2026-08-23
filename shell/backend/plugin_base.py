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
# 可选字段：
#   "central": False   — 不在集中设置面板显示（默认显示）
#   "help": "..."      — 设置面板悬浮提示

# 注意：docs/adapter-spec.md 中描述的 adapter_* 方法目前处于规划阶段，
# 尚未在本基类实现；adapter_process.py 也不应提前引入。


class PluginBase(ABC):
    # 子类覆盖：声明该插件的统一设置项
    settings_schema: List[Dict] = []

    def __init__(self, manifest: dict, config: dict):
        self.manifest = manifest
        self.config = config
        self.name = manifest.get('name', 'unknown')
        # 由 PluginManager 注入
        self._settings_store = None
        self._plugin_manager = getattr(self.__class__, '_plugin_manager', None)
        # PluginManager 在构造前预加载的已解决设置
        self._resolved_config = getattr(self.__class__, '_resolved_config', {})

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

    def get_dependency(self, name: str):
        """返回已加载依赖插件实例；未声明依赖或未加载时返回 None。"""
        dependencies = self.manifest.get('dependencies', []) or []
        if name not in dependencies:
            print(f"[{self.name}] 尝试访问未声明依赖的插件: {name}")
            return None
        if self._plugin_manager is None:
            return None
        return self._plugin_manager.get_plugin_instance(name)

    def get_extensions(self) -> List[dict]:
        """宿主前端可渲染的动作。默认空。"""
        return []

    def _default_settings(self) -> Dict:
        return {item.get('key'): item.get('default') for item in self.settings_schema if item.get('key')}

    def get_settings(self) -> Dict:
        """从统一设置存储读取设置（合并默认值）。子类可覆盖，但必须调用 super() 以保证 on_settings_changed 检测正确"""
        stored = self._settings_store.get(self.name) if self._settings_store else {}
        return {**self._default_settings(), **stored}

    def save_settings(self, settings: Dict) -> Dict:
        """校验、写入 SettingsStore、检测变更、调用 on_settings_changed。
        子类一般不需要覆盖此方法——覆盖 on_settings_changed() 即可响应设置变更。"""
        if not isinstance(settings, dict):
            return {"success": False, "error": "设置必须是字典"}

        old = self.get_settings()
        allowed = {item['key'] for item in self.settings_schema if item.get('key')}
        clean = {k: v for k, v in settings.items() if k in allowed} if allowed else settings

        if self._settings_store:
            try:
                self._settings_store.set(self.name, clean)
            except Exception as e:
                return {"success": False, "error": f"保存失败: {e}"}

        # 检测变更并通知插件
        changed = set()
        for k, v in clean.items():
            old_val = old.get(k)
            new_val = v
            if isinstance(old_val, float) and isinstance(new_val, (int, float)):
                if abs(old_val - new_val) > 0.001:
                    changed.add(k)
            elif old_val != new_val:
                changed.add(k)

        if changed:
            try:
                self.on_settings_changed(changed)
            except Exception as e:
                print(f"[{self.name}] on_settings_changed 异常: {e}")

        return {"success": True}

    def on_settings_changed(self, changed_keys: set):
        """设置变更时由 save_settings 自动调用。子类覆盖此方法以响应特定设置变更。

        示例:
            def on_settings_changed(self, changed_keys):
                if 'root_dir' in changed_keys:
                    self._reinit(self.setting('root_dir'))
        """
        pass

    def setting(self, key, default=None):
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

    def update_setting(self, key, value):
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

    def clear_settings(self) -> Dict:
        """清空统一设置存储中的该插件设置"""
        if self._settings_store:
            self._settings_store.clear(self.name)
        return {"success": True}

    def on_load(self): pass
    def on_unload(self): pass
