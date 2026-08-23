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
import json
import importlib.util
from pathlib import Path
from collections import deque
from typing import Dict, List, Callable
from shell.backend.settings_store import SettingsStore
from shell.backend.paths import get_plugins_config_dir

# kind: "local-adapter" 与 docs/adapter-spec.md 描述的外部程序接入能力尚未实装，
# 该字段目前只在规范检查器中作为“规划中”提示，不会影响加载行为。



def _resolve_config_dir() -> Path:
    return get_plugins_config_dir()


class PluginManager:
    def __init__(self, plugins_dirs, config: dict):
        raw_dirs = list(plugins_dirs) if isinstance(plugins_dirs, (list, tuple)) else [plugins_dirs]
        self.plugins_dirs: List[Path] = []
        for raw_dir in raw_dirs:
            path = Path(raw_dir).resolve()
            if path not in self.plugins_dirs:
                self.plugins_dirs.append(path)
        if not self.plugins_dirs:
            raise ValueError('至少需要提供一个插件搜索目录')
        self.plugins_dir = self.plugins_dirs[0]
        self.config = config
        self._instances = {}
        self._api_methods = {}
        self._manifests = {}
        self._plugin_dirs: Dict[str, Path] = {}
        self._config_dir = _resolve_config_dir()
        self._settings_store = SettingsStore(str(self._config_dir))
        self._old_settings_dir = Path(config['directories']['data_root']).resolve() / '.settings'
        print(f"[PluginManager] 插件搜索目录: {', '.join(str(p) for p in self.plugins_dirs)}")
        print(f"[PluginManager] 插件设置目录: {self._config_dir}")

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
        """把旧设置迁入 SettingsStore 并删除旧文件。

        - 新设置文件已存在时也继续处理旧文件：合并后删除，避免插件目录里
          残留 settings.json（新值优先，旧值补缺）。
        - 支持插件目录 settings.json 和旧 data/.settings/<plugin>.json。
        """
        new_file = self._config_dir / f'{plugin_name}.json'
        current = self._settings_store.get(plugin_name)

        # 插件目录里的 settings.json 比 data/.settings 更接近当前版本，先处理。
        plugin_dir = self._plugin_dirs.get(plugin_name)
        legacy_plugin_file = (plugin_dir / 'settings.json') if plugin_dir else (self.plugins_dir / plugin_name / 'settings.json')
        old_paths = [
            legacy_plugin_file,
            self._old_settings_dir / f'{plugin_name}.json',
        ]
        for old_file in old_paths:
            if not old_file.exists():
                continue
            try:
                with open(old_file, 'r', encoding='utf-8') as f:
                    old_data = json.load(f)
                if not isinstance(old_data, dict):
                    print(f"[PluginManager] 忽略格式错误的旧设置: {old_file}")
                    continue
                # 旧值补缺，SettingsStore 中已有值优先。
                merged = {**old_data, **current}
                if old_data or merged != current:
                    self._settings_store.set(plugin_name, merged)
                    current = merged
                old_file.unlink()
                print(f"[PluginManager] 迁移设置: {old_file} → {new_file}")
            except Exception as e:
                print(f"[PluginManager] 迁移设置失败 {plugin_name}: {e}")

    # ---------- API ----------

    def get_api_methods(self) -> Dict[str, Callable]:
        return self._api_methods
    def get_plugin_instance(self, name: str):
        return self._instances.get(name)

    def get_plugin_dir(self, name: str):
        return self._plugin_dirs.get(name)

    def get_plugin_data_root(self, name: str) -> Path:
        instance = self._instances.get(name)
        if instance is not None:
            return instance.get_data_root()
        return Path(self.config['directories']['data_root']).resolve()

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
            if not m.get('hidden')
        ]

    def get_plugin_extensions(self, host: str = None, placement: str = None) -> List[dict]:
        """聚合所有插件注册的扩展入口，可按宿主和位置过滤。

        扩展数据结构由各插件的 get_extensions() 返回，Shell 会自动补上 plugin 字段。
        """
        extensions = []
        for name, instance in self._instances.items():
            getter = getattr(instance, 'get_extensions', None)
            if not callable(getter):
                continue
            try:
                items = getter() or []
            except Exception as e:
                print(f"[PluginManager] 读取插件 {name} 扩展失败: {e}")
                continue
            if not isinstance(items, list):
                continue
            for ext in items:
                if not isinstance(ext, dict):
                    continue
                if host is not None and ext.get('host') != host:
                    continue
                if placement is not None and ext.get('placement') != placement:
                    continue
                normalized = dict(ext)
                normalized.setdefault('plugin', name)
                extensions.append(normalized)
        return extensions

    def _discover(self) -> Dict[str, dict]:
        manifests: Dict[str, dict] = {}
        routes: Dict[str, str] = {}
        self._plugin_dirs = {}

        for root in self.plugins_dirs:
            if not root.exists():
                print(f"[PluginManager] 跳过不存在的插件目录: {root}")
                continue

            try:
                plugin_dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)
            except OSError as e:
                print(f"[PluginManager] 读取插件目录失败 {root}: {e}")
                continue

            for plugin_dir in plugin_dirs:
                folder_name = plugin_dir.name
                if folder_name.startswith('_'):
                    continue

                mf_path = plugin_dir / 'manifest.json'
                if not mf_path.exists():
                    print(f"[PluginManager] 跳过 {folder_name}: 缺少 manifest.json")
                    continue

                try:
                    with open(mf_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception as e:
                    # 任何损坏的 manifest（非法 JSON、非 UTF-8 编码、读取失败等）
                    # 只影响当前插件，不能中断整批插件加载。
                    print(f"[PluginManager] 跳过 {folder_name}: manifest 读取失败: {e}")
                    continue

                if not isinstance(data, dict):
                    print(f"[PluginManager] 跳过 {folder_name}: manifest 必须是 JSON 对象")
                    continue

                manifest_name = data.get('name')
                if not isinstance(manifest_name, str) or not manifest_name.strip():
                    print(f"[PluginManager] 跳过 {folder_name}: manifest.name 缺失或无效")
                    continue
                if manifest_name != folder_name:
                    print(
                        f"[PluginManager] 跳过 {folder_name}: "
                        f"manifest.name 必须与文件夹名一致 (当前为 {manifest_name!r})"
                    )
                    continue

                frontend = data.get('frontend')
                route = frontend.get('route') if isinstance(frontend, dict) else None
                if not isinstance(route, str) or not route.strip() or not route.startswith('/'):
                    print(f"[PluginManager] 跳过 {folder_name}: frontend.route 缺失或必须以 / 开头")
                    continue
                route = route.strip()
                if route in ('/', '/settings'):
                    print(f"[PluginManager] 跳过 {folder_name}: frontend.route 不能使用保留路由 {route}")
                    continue
                backend = data.get('backend')
                if (not isinstance(backend, dict)
                        or not isinstance(backend.get('entry'), str)
                        or not backend['entry'].strip()
                        or not isinstance(backend.get('class'), str)
                        or not backend['class'].strip()):
                    print(f"[PluginManager] 跳过 {folder_name}: backend.entry / backend.class 缺失或无效")
                    continue

                dependencies = data.get('dependencies', [])
                if (not isinstance(dependencies, list)
                        or not all(isinstance(dep, str) and dep.strip() for dep in dependencies)):
                    print(f"[PluginManager] 跳过 {folder_name}: dependencies 必须是字符串数组")
                    continue

                if manifest_name in manifests:
                    print(f"[PluginManager] 跳过重复插件 {manifest_name} (来源: {root})")
                    continue
                if route in routes:
                    print(
                        f"[PluginManager] 跳过 {folder_name}: "
                        f"frontend.route {route} 已被插件 {routes[route]} 占用"
                    )
                    continue

                manifests[manifest_name] = data
                routes[route] = manifest_name
                self._plugin_dirs[manifest_name] = plugin_dir
        return manifests

    def _resolve_dependencies(self, manifests: dict) -> List[str]:
        """拓扑排序依赖：被依赖的插件先加载。

        对缺失依赖给出告警但不阻断；存在循环依赖时先加载无环部分，
        再把剩余插件按名称排序兜底加载。
        """
        dependency_of: Dict[str, List[str]] = {}
        for name, m in manifests.items():
            deps = m.get('dependencies', [])
            resolved = []
            for dep in deps:
                if dep == name:
                    print(f"[PluginManager] 插件 {name} 依赖自身，已忽略该依赖")
                    continue
                if dep in manifests:
                    resolved.append(dep)
            # 去重，保持声明顺序
            dependency_of[name] = list(dict.fromkeys(resolved))

        dependents: Dict[str, set] = {name: set() for name in manifests}
        for name, deps in dependency_of.items():
            for dep in deps:
                dependents[dep].add(name)

        in_degree = {name: len(dependency_of[name]) for name in manifests}
        queue = deque(sorted(name for name, degree in in_degree.items() if degree == 0))
        order: List[str] = []
        while queue:
            current = queue.popleft()
            order.append(current)
            for dependent in sorted(dependents[current]):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(manifests):
            cyclic = sorted(set(manifests) - set(order))
            print(f"[PluginManager] 检测到循环依赖，无法完全排序: {', '.join(cyclic)}")
            order.extend(cyclic)

        missing = {
            name: [dep for dep in m.get('dependencies', []) if dep not in manifests]
            for name, m in manifests.items()
        }
        missing = {name: deps for name, deps in missing.items() if deps}
        for name, unknown in sorted(missing.items()):
            print(f"[PluginManager] 插件 {name} 声明了不存在的依赖: {', '.join(unknown)}")

        return order

    def _load_plugin(self, name: str, manifest: dict):
        backend_cfg = manifest.get('backend', {})
        entry_file = backend_cfg.get('entry', 'backend/main.py')
        class_name = backend_cfg.get('class', 'Plugin')

        plugin_dir = self._plugin_dirs.get(name) or (self.plugins_dir / name)
        try:
            module_path = (plugin_dir / entry_file).resolve()
            if not module_path.is_relative_to(plugin_dir):
                print(f"[PluginManager]  加载失败 {name}: 入口文件越界 {entry_file}")
                return
        except OSError as e:
            print(f"[PluginManager]  加载失败 {name}: 入口路径解析失败: {e}")
            return

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
            # 预加载已解析的设置与 PluginManager，注入到类上供 __init__ 读取
            cls._resolved_config = self._settings_store.get(name)
            cls._plugin_manager = self
            try:
                instance = cls(manifest=manifest, config=self.config)
            finally:
                del cls._resolved_config
                del cls._plugin_manager
            instance._settings_store = self._settings_store
            instance._plugin_manager = self

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
