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
import os
import sys
import json
import importlib
from pathlib import Path
from collections import deque
from typing import Dict, List, Callable
import importlib.util
import importlib.machinery
from shell.backend.settings_store import SettingsStore


def _resolve_config_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / '.config' / 'plugins'
    return Path('.config') / 'plugins'


class PluginManager:
    def __init__(self, plugins_dir: str, config: dict):
        self.plugins_dir = Path(plugins_dir).resolve()
        self.config = config
        self._instances = {}
        self._api_methods = {}
        self._manifests = {}
        self._config_dir = _resolve_config_dir()
        self._settings_store = SettingsStore(str(self._config_dir))
        self._old_settings_dir = Path(config['directories']['data_root']).resolve() / '.settings'

    def load_all(self):
        manifests = self._discover()
        load_order = self._resolve_dependencies(manifests)

        for name in load_order:
            manifest = manifests[name]
            self._migrate_settings(name)
            self._load_plugin(name, manifest)

        print(f"[PluginManager] 加载完成，顺序: {' → '.join(load_order)}")

    # ---------- 设置迁移 ----------

    def _migrate_settings(self, plugin_name: str):
        new_file = self._config_dir / f'{plugin_name}.json'
        if new_file.exists():
            return

        # 查找旧设置文件
        old_paths = [
            self._old_settings_dir / f'{plugin_name}.json',
            self.plugins_dir / plugin_name / 'settings.json',
        ]
        for old_file in old_paths:
            if not old_file.exists():
                continue
            try:
                with open(old_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    new_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(new_file, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    old_file.unlink()
                    print(f"[PluginManager] 迁移设置: {old_file} → {new_file}")
                elif isinstance(data, dict):
                    old_file.unlink()
            except Exception as e:
                print(f"[PluginManager] 迁移设置失败 {plugin_name}: {e}")
            break

    # ---------- API ----------

    def get_api_methods(self) -> Dict[str, Callable]:
        return self._api_methods

    def get_frontend_manifests(self) -> List[dict]:
        """返回给前端路由使用的清单"""
        return [
            {
                'name': m['name'],
                'displayName': m.get('displayName', m['name']),
                'icon': m.get('icon', '📦'),
                'route': m['frontend']['route'],
                'entryUrl': f"/plugins/{m['name']}/frontend/index.html",
                'destroyOnLeave': m.get('destroyOnLeave', False)
            }
            for m in self._manifests.values()
        ]

    def _discover(self) -> Dict[str, dict]:
        manifests = {}
        if not self.plugins_dir.exists():
            return manifests

        for d in self.plugins_dir.iterdir():
            if not d.is_dir() or d.name.startswith('_'):
                continue
            mf_path = d / 'manifest.json'
            if not mf_path.exists():
                continue

            with open(mf_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            manifests[data['name']] = data
        return manifests

    def _resolve_dependencies(self, manifests: dict) -> List[str]:
        in_degree = {name: 0 for name in manifests}
        for name, m in manifests.items():
            for dep in m.get('dependencies', []):
                if dep in in_degree:
                    in_degree[name] += 1

        queue = deque([name for name, deg in in_degree.items() if deg == 0])
        order = []
        while queue:
            curr = queue.popleft()
            order.append(curr)
        for name in manifests:
            if name not in order:
                order.append(name)
        return order

    def _load_plugin(self, name: str, manifest: dict):
        backend_cfg = manifest.get('backend', {})
        entry_file = backend_cfg.get('entry', 'backend/main.py')
        class_name = backend_cfg.get('class', 'Plugin')

        plugin_dir = self.plugins_dir / name
        module_path = plugin_dir / entry_file

        if not module_path.exists():
            print(f"[PluginManager]  加载失败 {name}: 入口文件不存在 {module_path}")
            return

        unique_module_name = f"{name}.backend.main"
        print(f"[PluginManager] 尝试加载插件: {name}.backend.main")

        try:
            spec = importlib.util.spec_from_file_location(unique_module_name, str(module_path))
            if spec is None:
                print(f"[PluginManager]  加载失败 {name}: 模块规格创建失败")
                return
            mod = importlib.util.module_from_spec(spec)
            if spec.loader is None:
                print(f"[PluginManager]  加载失败 {name}: 模块加载器创建失败")
                return
            spec.loader.exec_module(mod)

            cls = getattr(mod, class_name)
            # 预加载已解析的设置，注入到类上供 __init__ 读取
            cls._resolved_config = self._settings_store.get(name)
            instance = cls(manifest=manifest, config=self.config)
            del cls._resolved_config
            instance._settings_store = self._settings_store

            for method_name, method_fn in instance.register_api().items():
                self._api_methods[f"{name}__{method_name}"] = method_fn
            self._api_methods[f"{name}__get_settings_schema"] = (
                lambda inst=instance: getattr(inst, 'settings_schema', [])
            )

            instance.on_load()
            self._instances[name] = instance
            self._manifests[name] = manifest
            print(f"[PluginManager]  加载成功: {name}")
        except Exception as e:
            print(f"[PluginManager]  加载失败 {name}: {e}")

    # ---------- 集中设置面板 ----------

    def get_settings_panels(self) -> List[dict]:
        """返回声明了 settings_schema 的插件的设置面板数据。
        只显示 root_dir 或声明 central:true 的字段，其余在插件内部设置。"""
        panels = []
        for name, inst in self._instances.items():
            schema = getattr(inst, 'settings_schema', None) or []
            if not schema:
                continue
            # 过滤：若 schema 中有 central 标记，仅显示 root_dir 或 central:true 的字段
            # 若无标记（旧版兼容），显示全部字段
            has_central = any('central' in f for f in schema)
            if has_central:
                central_schema = [f for f in schema if f.get('key') == 'root_dir' or f.get('central') is True]
            else:
                central_schema = schema
            if not central_schema:
                continue
            try:
                values = inst.get_settings()
            except Exception as e:
                values = {}
                print(f"[PluginManager] 读取设置失败 {name}: {e}")
            manifest = self._manifests.get(name, {})
            panels.append({
                'name': name,
                'displayName': manifest.get('displayName', name),
                'icon': manifest.get('icon', '📦'),
                'schema': central_schema,
                'values': values if isinstance(values, dict) else {},
            })
        return panels

    def save_settings_panel(self, plugin_name: str, values: dict) -> dict:
        """保存指定插件的设置（走插件自身的 save_settings）"""
        inst = self._instances.get(plugin_name)
        if inst is None:
            return {"success": False, "error": f"插件不存在: {plugin_name}"}
        try:
            result = inst.save_settings(values)
            if isinstance(result, dict):
                return result
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
