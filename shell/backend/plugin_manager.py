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

import importlib.util
import json
import logging
import sys
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List

from shell.backend.paths import get_plugins_config_dir
from shell.backend.plugin_base import PluginBase, mask_secrets
from shell.backend.protected_paths import normalize as normalize_path
from shell.backend.settings_store import SettingsStore

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from shell.backend.plugin_base import PluginBase

# kind: "local-adapter" 与 docs/adapter-spec.md 描述的外部程序接入能力尚未实装，
# 该字段目前只在规范检查器中作为“规划中”提示，不会影响加载行为。



def _resolve_config_dir() -> Path:
    return get_plugins_config_dir()


def _is_protectable(instance: PluginBase, path: Path, config_dir: Path) -> bool:
    r"""该路径是否落在插件有权申报的范围内（自身数据根或 <config>/plugins）。

    这是**防误用**而不是安全边界：插件后端与 Shell 同进程，它不需要"申报"就能
    直接读任何文件。边界的作用是让一个写错的申报（例如声明了盘符根）被忽略并
    留痕，而不是把整个文件服务钉死。

    两侧都用 protected_paths.normalize()：它会剥掉 `\\?\` 扩展前缀再 resolve()。
    只 resolve() 是不够的 —— 扩展前缀形态与普通形态既不等也不互相包含，
    于是"插件用扩展形式申报、Shell 用普通形式比较"会得出"越界"的错误结论，
    保护静默失效（反之亦然，见该模块的说明）。
    """
    roots: List[Path] = []
    try:
        roots.append(normalize_path(config_dir))
    except (OSError, TypeError, ValueError):
        pass
    try:
        roots.append(normalize_path(instance.get_data_root()))
    except Exception:
        # get_data_root() 由插件实现（image-viewer 读 self.root_dir）：它抛异常时
        # 不能连累整条聚合，只是少一个允许范围。
        pass
    return any(path.is_relative_to(root) for root in roots)


def collect_protected_paths(instances: Dict[str, PluginBase], config_dir: Path) -> List[Path]:
    """汇总插件申报的受保护路径（`PluginBase.get_protected_paths`）。

    每次调用现算、不做缓存：插件支持运行期卸载与重新加载，快照会让新加载插件
    申报的路径静默失效 —— 而"静默失效的防护"比没有防护更危险。插件数量是个位
    数量级，相对文件本身的 I/O 可以忽略。
    """
    protected: List[Path] = []
    # 快照后再遍历：unload_all()/加载都可能与请求线程并发改动实例表
    for name, instance in list(instances.items()):
        try:
            declared = instance.get_protected_paths()
        except Exception as e:
            log.warning(f"[PluginManager] {name} 申报受保护路径失败: {e}")
            continue
        if not isinstance(declared, (list, tuple)):
            log.warning(f"[PluginManager] {name}.get_protected_paths() 必须返回列表，已忽略")
            continue
        for raw in declared:
            try:
                path = normalize_path(raw)
            except (OSError, TypeError, ValueError):
                log.warning(f"[PluginManager] {name} 申报的受保护路径无效，已忽略: {raw!r}")
                continue
            if not _is_protectable(instance, path, config_dir):
                log.warning(f"[PluginManager] {name} 申报的受保护路径越界，已忽略: {path}")
                continue
            protected.append(path)
    return protected


class PluginManager:
    def __init__(self, plugins_dirs: str | Path | Iterable[str | Path], config: dict) -> None:
        raw_dirs = (
            list(plugins_dirs)
            if isinstance(plugins_dirs, Iterable) and not isinstance(plugins_dirs, (str, bytes, Path))
            else [plugins_dirs]
        )
        self.plugins_dirs: List[Path] = []
        for raw_dir in raw_dirs:
            path = Path(raw_dir).resolve()
            if path not in self.plugins_dirs:
                self.plugins_dirs.append(path)
        if not self.plugins_dirs:
            raise ValueError('至少需要提供一个插件搜索目录')
        self.plugins_dir = self.plugins_dirs[0]
        self.config = config
        self._instances: Dict[str, PluginBase] = {}
        self._api_methods: Dict[str, Callable] = {}
        self._manifests: Dict[str, dict] = {}
        self._plugin_dirs: Dict[str, Path] = {}
        # 加载失败的插件：{名字: 原因}。以前只 print 一行，界面拿到的
        # get_frontend_manifests 只含成功的插件 —— 用户只会觉得"插件没了"，
        # 现在由 /status 展示（docs/code-review.md §4.2）。
        self._load_failures: Dict[str, str] = {}
        self._config_dir = _resolve_config_dir()
        self._settings_store = SettingsStore(str(self._config_dir))
        self._old_settings_dir = Path(config['directories']['data_root']).resolve() / '.settings'
        log.info(f"[PluginManager] 插件搜索目录: {', '.join(str(p) for p in self.plugins_dirs)}")
        log.info(f"[PluginManager] 插件设置目录: {self._config_dir}")

    def load_all(self) -> None:
        manifests = self._discover()
        load_order = self._resolve_dependencies(manifests)

        for name in load_order:
            manifest = manifests[name]
            self._migrate_settings(name)
            self._load_plugin(name, manifest)

        log.info(f"[PluginManager] 加载完成，顺序: {' → '.join(load_order)}")

    def unload_all(self) -> None:
        """卸载全部插件（进程退出前调用），按加载的逆序回调 on_unload。

        on_unload 是插件唯一的收尾钩子（关 SQLite/WAL、停后台线程、落盘最后一次
        状态）。它写在开发指南里却长期没有任何调用点，于是这些资源一直没人收尾。
        单个插件抛错不影响其余插件，也不阻断退出；重复调用是安全的（幂等）。
        """
        for name in reversed(list(self._instances.keys())):
            instance = self._instances.pop(name, None)
            if instance is None:
                continue
            self._manifests.pop(name, None)
            prefix = f"{name}__"
            for method_key in [k for k in self._api_methods if k.startswith(prefix)]:
                self._api_methods.pop(method_key, None)
            try:
                instance.on_unload()
                log.info(f"[PluginManager] 已卸载: {name}")
            except Exception as e:
                log.error(f"[PluginManager] 卸载失败 {name}: {e}")

    # ---------- 设置迁移 ----------

    def _migrate_settings(self, plugin_name: str) -> None:
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
                    log.error(f"[PluginManager] 忽略格式错误的旧设置: {old_file}")
                    continue
                # 旧值补缺，SettingsStore 中已有值优先。
                merged = {**old_data, **current}
                if old_data or merged != current:
                    self._settings_store.set(plugin_name, merged)
                    current = merged
                old_file.unlink()
                log.info(f"[PluginManager] 迁移设置: {old_file} → {new_file}")
            except Exception as e:
                log.error(f"[PluginManager] 迁移设置失败 {plugin_name}: {e}")

    # ---------- API ----------

    def get_api_methods(self) -> Dict[str, Callable]:
        """返回方法表的**副本**。

        以前直接返回 self._api_methods，而 file_server 每个请求都会 `dict(...)`
        或用它建表；Flask 是 threaded=True，卸载/加载插件（unload_all 会 pop）
        与请求线程同时操作同一个 dict 会抛
        "dictionary changed size during iteration"（关闭/重载瞬间的 500）。
        返回副本让调用方拿到一致快照，写入仍全部发生在管理器内部。
        """
        return dict(self._api_methods)
    def get_plugin_instance(self, name: str) -> PluginBase | None:
        return self._instances.get(name)

    def get_protected_paths(self) -> List[Path]:
        """汇总所有插件申请的受保护路径（PluginBase.get_protected_paths）。

        实现放在模块级 `collect_protected_paths()`：它只依赖"实例表 + 配置目录"，
        因此测试里的桩管理器能用**同一份**实现，不会出现"桩放松了边界校验、
        用例却仍然全绿"。
        """
        return collect_protected_paths(self._instances, self._config_dir)

    def get_plugin_dir(self, name: str) -> Path | None:
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
                'icon': m.get('icon', 'icon:package'),
                'route': m['frontend']['route'],
                'entryUrl': f"/plugins/{m['name']}/frontend/index.html",
                'keepAlive': bool(m.get('keepAlive', False))
            }
            for m in self._manifests.values()
            if not m.get('hidden')
        ]

    def get_plugin_status(self) -> dict:
        """插件加载状态：供 /status 回答"某个插件为什么不见了"。

        以前加载失败只有一行日志，而界面拿到的 get_frontend_manifests 只含
        加载成功的插件 —— 用户看到的现象就是"插件消失了"，没有任何线索
        （docs/code-review.md §4.2）。
        """
        return {
            'loaded': sorted(self._instances.keys()),
            'failures': [
                {'name': name, 'reason': reason}
                for name, reason in sorted(self._load_failures.items())
            ],
        }

    def get_plugin_extensions(self, host: str|None = None, placement: str|None = None) -> List[dict]:
        """聚合所有插件注册的扩展入口，可按宿主和位置过滤。

        扩展数据结构由各插件的 get_extensions() 返回，Shell 会自动补上 plugin 字段。
        """
        extensions = []
        # 快照后再遍历：unload_all()/加载都可能与请求线程并发改动 _instances
        for name, instance in list(self._instances.items()):
            getter = getattr(instance, 'get_extensions', None)
            if not callable(getter):
                continue
            try:
                items = getter() or []
            except Exception as e:
                log.error(f"[PluginManager] 读取插件 {name} 扩展失败: {e}")
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
                log.warning(f"[PluginManager] 跳过不存在的插件目录: {root}")
                continue

            try:
                plugin_dirs = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)
            except OSError as e:
                log.error(f"[PluginManager] 读取插件目录失败 {root}: {e}")
                continue

            for plugin_dir in plugin_dirs:
                folder_name = plugin_dir.name
                if folder_name.startswith('_'):
                    continue

                mf_path = plugin_dir / 'manifest.json'
                if not mf_path.exists():
                    log.warning(f"[PluginManager] 跳过 {folder_name}: 缺少 manifest.json")
                    continue

                try:
                    with open(mf_path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception as e:
                    # 任何损坏的 manifest（非法 JSON、非 UTF-8 编码、读取失败等）
                    # 只影响当前插件，不能中断整批插件加载。
                    log.error(f"[PluginManager] 跳过 {folder_name}: manifest 读取失败: {e}")
                    continue

                if not isinstance(data, dict):
                    log.warning(f"[PluginManager] 跳过 {folder_name}: manifest 必须是 JSON 对象")
                    continue

                manifest_name = data.get('name')
                if isinstance(manifest_name, str) and manifest_name.strip():
                    if manifest_name != folder_name:
                        log.warning(f"[PluginManager] 警告 {folder_name}: manifest.name={manifest_name!r} 与文件夹名不一致，已使用文件夹名")
                else:
                    log.info(f"[PluginManager] 提示 {folder_name}: 未声明 manifest.name，已使用文件夹名")
                name = folder_name
                data['name'] = name

                frontend = data.get('frontend')
                if not isinstance(frontend, dict):
                    frontend = {}
                    data['frontend'] = frontend
                route = frontend.get('route')
                if not isinstance(route, str) or not route.strip() or not route.startswith('/'):
                    route = f'/{name}'
                    frontend['route'] = route
                    log.info(f"[PluginManager] 提示 {name}: 未声明 frontend.route，已默认 {route}")
                else:
                    route = route.strip()
                if route in ('/', '/settings'):
                    log.warning(f"[PluginManager] 跳过 {folder_name}: frontend.route 不能使用保留路由 {route}")
                    continue

                backend = data.get('backend')
                if not isinstance(backend, dict):
                    backend = {}
                    data['backend'] = backend
                if not isinstance(backend.get('entry'), str) or not backend['entry'].strip():
                    backend['entry'] = 'backend/main.py'
                    log.info(f"[PluginManager] 提示 {name}: 未声明 backend.entry，已默认 backend/main.py")
                if not isinstance(backend.get('class'), str) or not backend['class'].strip():
                    backend['class'] = 'Plugin'
                    log.info(f"[PluginManager] 提示 {name}: 未声明 backend.class，已默认 Plugin")

                dependencies = data.get('dependencies', [])
                if (not isinstance(dependencies, list)
                        or not all(isinstance(dep, str) and dep.strip() for dep in dependencies)):
                    log.warning(f"[PluginManager] 跳过 {folder_name}: dependencies 必须是字符串数组")
                    continue

                if name in manifests:
                    log.warning(f"[PluginManager] 跳过重复插件 {name} (来源: {root})")
                    continue
                if route in routes:
                    log.info(
                        f"[PluginManager] 跳过 {folder_name}: "
                        f"frontend.route {route} 已被插件 {routes[route]} 占用"
                    )
                    continue

                manifests[name] = data
                routes[route] = name
                self._plugin_dirs[name] = plugin_dir
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
                    log.warning(f"[PluginManager] 插件 {name} 依赖自身，已忽略该依赖")
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
            log.error(f"[PluginManager] 检测到循环依赖，无法完全排序: {', '.join(cyclic)}")
            order.extend(cyclic)

        missing = {
            name: [dep for dep in m.get('dependencies', []) if dep not in manifests]
            for name, m in manifests.items()
        }
        missing = {name: deps for name, deps in missing.items() if deps}
        for name, unknown in sorted(missing.items()):
            log.info(f"[PluginManager] 插件 {name} 声明了不存在的依赖: {', '.join(unknown)}")

        return order

    def _plugin_lib_dirs(self, plugin_dir: Path, manifest: dict) -> List[Path]:
        """返回插件本地附加库目录。

        默认使用 <plugin>/backend/libs；也可在 manifest 中用 libs 字段自定义：
            "libs": ["backend/libs", "vendor"]
        这些目录会在加载插件后端前加入 sys.path，便于携带纯 Python 依赖。
        """
        raw = manifest.get('libs', ['backend/libs'])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        dirs = []
        for rel in raw:
            if not isinstance(rel, str) or not rel.strip():
                continue
            try:
                path = (plugin_dir / rel).resolve()
                if path.is_relative_to(plugin_dir.resolve()) and path.is_dir():
                    dirs.append(path)
            except OSError:
                continue
        return dirs

    def _load_plugin(self, name: str, manifest: dict):
        backend_cfg = manifest.get('backend', {})
        entry_file = backend_cfg.get('entry', 'backend/main.py')
        class_name = backend_cfg.get('class', 'Plugin')

        plugin_dir = self._plugin_dirs.get(name) or (self.plugins_dir / name)

        # 插件本地附加库：在加载后端前加入 sys.path，支持纯 Python 依赖随插件分发。
        # 加载失败的插件必须把这些路径收回去，否则往后的插件会误用别人的私有库。
        inserted_paths: List[str] = []

        def rollback_lib_paths():
            for lib_path in inserted_paths:
                try:
                    sys.path.remove(lib_path)
                except ValueError:
                    pass
            inserted_paths.clear()

        def fail(reason: str):
            """加载失败的统一出口：收回 sys.path、登记失败原因且不留下任何状态。"""
            rollback_lib_paths()
            self._load_failures[name] = reason
            log.error(f"[PluginManager]  加载失败 {name}: {reason}")

        for lib_dir in self._plugin_lib_dirs(plugin_dir, manifest):
            lib_path = str(lib_dir)
            sys.path.insert(0, lib_path)
            inserted_paths.append(lib_path)

        try:
            module_path = (plugin_dir / entry_file).resolve()
            if not module_path.is_relative_to(plugin_dir):
                fail(f"入口文件越界 {entry_file}")
                return
        except OSError as e:
            fail(f"入口路径解析失败: {e}")
            return

        if not module_path.exists():
            fail(f"入口文件不存在 {module_path}")
            return

        unique_module_name = f"{name}.backend.main"
        log.info(f"[PluginManager] 尝试加载插件: {name}.backend.main")

        try:
            spec = importlib.util.spec_from_file_location(unique_module_name, str(module_path))
            if spec is None:
                fail("模块规格创建失败")
                return
            mod = importlib.util.module_from_spec(spec)
            if spec.loader is None:
                fail("模块加载器创建失败")
                return
            spec.loader.exec_module(mod)

            cls = getattr(mod, class_name)
            # 预加载已解析的设置与 PluginManager，注入到类上供 __init__ 读取。
            # 这里必须"保存-还原"而不是无条件 del：旧写法 `finally: del cls._resolved_config`
            # 会在类上原本没有该属性时抛 AttributeError，把插件真正的加载失败原因
            # 顶掉（/status 显示的是 "type object 'X' has no attribute ..."）；
            # 也会静默删除子类自己定义的同名类属性。
            _MISSING = object()
            prev_resolved = getattr(cls, '_resolved_config', _MISSING)
            prev_manager = getattr(cls, '_plugin_manager', _MISSING)
            cls._resolved_config = self._settings_store.get(name)
            cls._plugin_manager = self
            try:
                instance = cls(manifest=manifest, config=self.config)
            finally:
                for attr, prev in (('_resolved_config', prev_resolved), ('_plugin_manager', prev_manager)):
                    if prev is _MISSING:
                        # cls.__dict__ 是只读的 mappingproxy，只能用 delattr
                        try:
                            delattr(cls, attr)
                        except AttributeError:
                            pass
                    else:
                        setattr(cls, attr, prev)
            instance._settings_store = self._settings_store
            instance._plugin_manager = self

            # 方法表先在本地组装，on_load 成功后才一次性登记：on_load 抛错时
            # 若已经把方法写进 _api_methods，就会出现"幽灵 API"——/api/<插件>__<方法>
            # 能打到从未进入 _instances 的半初始化实例（docs/code-review.md §4.1-3）。
            pending_methods = {
                f"{name}__{method_name}": self._exposed_method(instance, method_name, method_fn)
                for method_name, method_fn in instance.register_api().items()
            }
            pending_methods[f"{name}__get_settings_schema"] = (
                lambda inst=instance: getattr(inst, 'settings_schema', [])
            )

            instance.on_load()
            self._api_methods.update(pending_methods)
            self._instances[name] = instance
            self._manifests[name] = manifest
            self._load_failures.pop(name, None)
            log.info(f"[PluginManager]  加载成功: {name}")
        except Exception as e:
            # 回滚：任何一步失败都不留下半初始化的实例 / 方法 / 清单
            self._instances.pop(name, None)
            self._manifests.pop(name, None)
            prefix = f"{name}__"
            for method_key in [k for k in self._api_methods if k.startswith(prefix)]:
                self._api_methods.pop(method_key, None)
            fail(str(e))

    # ---------- 插件方法出口约束 ----------

    @staticmethod
    def _exposed_method(instance: PluginBase, method_name: str, method_fn: Callable) -> Callable:
        """包一层出口处理：插件注册的方法在返回前要过的 Shell 侧约束。

        目前只有一条 —— `get_settings` 的凭据脱敏。**必须由 Shell 做**：插件可以
        覆写 `get_settings()`（image-viewer / manga-library 都覆写了），基类里的
        掩码于是整个不执行；而该方法经 register_api() 直接变成
        `POST /api/<插件>__get_settings`，与壳同源 —— 覆写一下就能把长期凭据交给
        任何一段同源脚本。返回非字典（插件自定义形状）时原样放行。
        """
        if method_name != 'get_settings':
            return method_fn

        def masked(*args, **kwargs):
            return mask_secrets(getattr(instance, 'settings_schema', None) or [],
                                method_fn(*args, **kwargs))

        return masked
