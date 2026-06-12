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
class PluginManager:
    def __init__(self, plugins_dir: str, config: dict):
        self.plugins_dir = Path(plugins_dir).resolve()
        self.config = config
        self._instances = {}
        self._api_methods = {}
        self._manifests = {}

    def load_all(self):
        manifests = self._discover()
        load_order = self._resolve_dependencies(manifests)
        
        for name in load_order:
            self._load_plugin(name, manifests[name])
        
        print(f"[PluginManager] 加载完成，顺序: {' → '.join(load_order)}")

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
                'entryUrl': f"/plugins/{m['name']}/frontend/index.html" # Flask 将 serve 这个路径
            }
            for m in self._manifests.values()
        ]

    def _discover(self) -> Dict[str, dict]:
        manifests = {}
        if not self.plugins_dir.exists(): return manifests
        
        for d in self.plugins_dir.iterdir():
            if not d.is_dir() or d.name.startswith('_'): continue
            mf_path = d / 'manifest.json'
            if not mf_path.exists(): continue
            
            with open(mf_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            manifests[data['name']] = data
        return manifests

    def _resolve_dependencies(self, manifests: dict) -> List[str]:
        # Kahn 拓扑排序 (同 v2 逻辑，此处简写)
        # 实际生产需加入循环依赖检测
        in_degree = {name: 0 for name in manifests}
        for name, m in manifests.items():
            for dep in m.get('dependencies', []):
                if dep in in_degree: in_degree[name] += 1 # 简化
        
        queue = deque([name for name, deg in in_degree.items() if deg == 0])
        order = []
        while queue:
            curr = queue.popleft()
            order.append(curr)
        # 补丁：如果存在未解析的依赖，直接追加（容错）
        for name in manifests:
            if name not in order: order.append(name)
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

        # 构造唯一模块名，避免不同插件间的冲突
        unique_module_name = f"{name}.backend.main"
        print(f"[PluginManager] 尝试加载插件: {name}.backend.main")

        try:
            # 从文件路径创建模块规格
            spec= importlib.util.spec_from_file_location(unique_module_name,str(module_path))
            if spec is None:
                print(f"[PluginManager]  加载失败 {name}: 模块规格创建失败")
                return
            mod = importlib.util.module_from_spec(spec)
            if spec.loader is None:
                print(f"[PluginManager]  加载失败 {name}: 模块加载器创建失败")
                return
            # 执行模块（将代码加载到 mod 的命名空间中）
            spec.loader.exec_module(mod)

            cls = getattr(mod, class_name)
            instance = cls(manifest=manifest, config=self.config)

            # 注册 API (带命名空间)
            for method_name, method_fn in instance.register_api().items():
                self._api_methods[f"{name}__{method_name}"] = method_fn

            instance.on_load()
            self._instances[name] = instance
            self._manifests[name] = manifest
            print(f"[PluginManager]  加载成功: {name}")
        except Exception as e:
            print(f"[PluginManager]  加载失败 {name}: {e}")