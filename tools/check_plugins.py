#!/usr/bin/env python3
"""OmniBox 插件规范检查器。

检查项：
- manifest.json 可解析、name 与目录名一致、frontend/backend 声明完整
- frontend/backend 入口文件存在且不越界
- route 合法、保留路由、跨插件不重复
- dependencies 是字符串数组、引用存在且不依赖自身
- minShellVersion 不超过当前 shell 版本
- 插件目录内不得残留 settings.json 或旧版 _save_settings_to_file 代码
- 后端类可导入、继承 PluginBase、settings_schema 结构合法
- 已规划但未实装的 kind: "local-adapter" 只告警不阻断

用法：
    python tools/check_plugins.py
    python tools/check_plugins.py --no-load
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_PLUGINS_DIR = PROJECT_ROOT / 'plugins'
RESERVED_ROUTES = {'/', '/settings'}
ALLOWED_SCHEMA_TYPES = {'text', 'number', 'range', 'select', 'checkbox', 'textarea', 'folder'}
PLUGIN_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-_]*$')
LEGACY_SETTINGS_MARKERS = ('settings_file', '_save_settings_to_file')
LOCAL_SIBLING_LOADER_MARKER = 'def _load_sibling'


def get_shell_version() -> str:
    try:
        package_json = PROJECT_ROOT / 'shell' / 'frontend' / 'package.json'
        return json.loads(package_json.read_text(encoding='utf-8')).get('version', '0.0.0')
    except Exception:
        return '0.0.0'


def parse_version(value: str) -> Tuple[int, ...]:
    parts = re.findall(r'\d+', value or '')[:3]
    return tuple(int(part) for part in parts) or (0, 0, 0)


def _read_manifest(plugin_dir: Path) -> Tuple[Path, dict | None, str | None]:
    manifest_path = plugin_dir / 'manifest.json'
    try:
        data = json.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception as exc:
        return manifest_path, None, f'manifest.json 无法读取或不是合法 UTF-8 JSON: {exc}'
    if not isinstance(data, dict):
        return manifest_path, None, 'manifest 必须是 JSON 对象'
    return manifest_path, data, None


def _entry_is_safe(plugin_dir: Path, entry: str) -> bool:
    try:
        full = (plugin_dir / entry).resolve()
        return full.is_relative_to(plugin_dir.resolve()) and full.is_file()
    except Exception:
        return False


def _check_frontend_assets(plugin_dir: Path, frontend_entry: str) -> List[str]:
    """校验 index.html 引用的本地 script/css 资源都存在且不越界。"""
    errors: List[str] = []
    index_path = plugin_dir / frontend_entry
    try:
        html = index_path.read_text(encoding='utf-8')
    except Exception as exc:
        return [f'frontend.entry 无法读取: {exc}']
    base_dir = index_path.parent
    for match in re.findall(r'(?:src|href)="([^"]+)"', html):
        if match.startswith(('/', 'http://', 'https://', '//', 'data:', '#')):
            continue
        try:
            asset = (base_dir / match.split('?', 1)[0].split('#', 1)[0]).resolve()
            if not asset.is_relative_to(plugin_dir.resolve()) or not asset.is_file():
                errors.append(f'frontend 资源缺失或越界: {match}')
        except Exception:
            errors.append(f'frontend 资源路径无效: {match}')
    return errors



def _check_schema(cls) -> List[str]:
    """校验后端类的 settings_schema，返回错误文本列表。"""
    errors: List[str] = []
    schema = getattr(cls, 'settings_schema', None)
    if schema is None:
        return errors
    if not isinstance(schema, list):
        return ['settings_schema 必须是 list']
    keys: set = set()
    for index, field in enumerate(schema):
        where = f'settings_schema[{index}]'
        if not isinstance(field, dict):
            errors.append(f'{where} 必须是对象')
            continue
        key = field.get('key')
        if not isinstance(key, str) or not key.strip():
            errors.append(f'{where}.key 缺失或无效')
            continue
        if key in keys:
            errors.append(f'{where}.key={key!r} 重复')
        keys.add(key)

        field_type = field.get('type', 'text')
        if field_type not in ALLOWED_SCHEMA_TYPES:
            errors.append(f'{where}.type={field_type!r} 不在支持列表 {sorted(ALLOWED_SCHEMA_TYPES)}')

        if 'default' in field:
            default = field['default']
            if field_type == 'number' and not isinstance(default, (int, float)):
                errors.append(f'{where}.default 应为 number')
            if field_type == 'checkbox' and not isinstance(default, bool):
                errors.append(f'{where}.default 应为 bool')

        if field_type in {'number', 'range'}:
            bounds = [field.get('min'), field.get('max')]
            for bound_name in ('min', 'max'):
                value = field.get(bound_name)
                if value is not None and not isinstance(value, (int, float)):
                    errors.append(f'{where}.{bound_name} 应为 number')
            if all(isinstance(value, (int, float)) for value in bounds if value is not None):
                if field.get('min') is not None and field.get('max') is not None and field['min'] > field['max']:
                    errors.append(f'{where}.min 不能大于 max')

        if field_type == 'select':
            options = field.get('options')
            if not isinstance(options, list) or not options:
                errors.append(f'{where}.options 必须是非空数组')
            else:
                values = []
                for option in options:
                    value = option.get('value') if isinstance(option, dict) else option
                    if not isinstance(value, str):
                        errors.append(f'{where}.options 中存在无效 value')
                    else:
                        values.append(value)
                if len(set(values)) != len(values):
                    errors.append(f'{where}.options 的 value 重复')
                if 'default' in field and field.get('default') not in values:
                    errors.append(f'{where}.default 不在 options 中')
    return errors


def _load_backend_class(plugin_dir: Path, entry: str, class_name: str):
    module_path = (plugin_dir / entry).resolve()
    module_name = f"omnibox_spec_{re.sub(r'[^0-9A-Za-z_]', '_', plugin_dir.name)}"
    spec = importlib.util.spec_from_file_location(module_name, str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError('无法为后端模块创建 importlib spec')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise RuntimeError(f'后端模块中不存在类 {class_name}')
    if not isinstance(cls, type):
        raise RuntimeError(f'{class_name} 不是类')
    from shell.backend.plugin_base import PluginBase

    if not issubclass(cls, PluginBase):
        raise RuntimeError(f'{class_name} 未继承 PluginBase')
    return cls


def check_plugins(plugins_dir: Path | None = None, load_backends: bool = True) -> Tuple[List[str], List[str]]:
    plugins_dir = Path(plugins_dir or DEFAULT_PLUGINS_DIR)
    errors: List[str] = []
    warnings: List[str] = []
    discovered: List[Tuple[Path, str, dict]] = []

    if not plugins_dir.exists():
        return [f'插件目录不存在: {plugins_dir}'], []

    shell_version = parse_version(get_shell_version())
    routes: Dict[str, str] = {}

    for plugin_dir in sorted([p for p in plugins_dir.iterdir() if p.is_dir()], key=lambda p: p.name):
        folder_name = plugin_dir.name
        if folder_name.startswith('_'):
            continue
        where = f'[{folder_name}]'
        _, data, read_error = _read_manifest(plugin_dir)
        if read_error:
            errors.append(f'{where} {read_error}')
            continue
        assert data is not None

        name = data.get('name')
        if not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name):
            errors.append(f'{where} manifest.name 缺失或不是合法的 slug: {name!r}')
        elif name != folder_name:
            errors.append(f'{where} manifest.name={name!r} 必须与目录名一致')

        if isinstance(data.get('version'), str):
            if not re.fullmatch(r'\d+\.\d+\.\d+', data['version']):
                errors.append(f'{where} manifest.version 建议使用 x.y.z 格式')
        elif 'version' in data:
            errors.append(f'{where} manifest.version 缺失或不是字符串')

        frontend = data.get('frontend')
        if not isinstance(frontend, dict):
            errors.append(f'{where} frontend 缺失或不是对象')
            frontend = {}
        route = frontend.get('route')
        if not isinstance(route, str) or not route.strip().startswith('/'):
            errors.append(f'{where} frontend.route 缺失或必须以 / 开头')
        else:
            route = route.strip()
            if route in RESERVED_ROUTES:
                errors.append(f'{where} frontend.route={route} 是保留路由')
            elif route in routes:
                errors.append(f'{where} frontend.route={route} 与插件 {routes[route]} 重复')
            else:
                routes[route] = name if isinstance(name, str) else folder_name
        frontend_entry = frontend.get('entry') if isinstance(frontend, dict) else None
        if not isinstance(frontend_entry, str) or not frontend_entry.strip():
            errors.append(f'{where} frontend.entry 缺失或无效')
        elif not _entry_is_safe(plugin_dir, frontend_entry):
            errors.append(f'{where} frontend.entry 不存在或越界: {frontend_entry}')
        else:
            errors.extend(f'[{folder_name}] {error}' for error in _check_frontend_assets(plugin_dir, frontend_entry))


        backend = data.get('backend')
        if not isinstance(backend, dict):
            errors.append(f'{where} backend 缺失或不是对象')
            backend = {}
        backend_entry = backend.get('entry') if isinstance(backend, dict) else None
        class_name = backend.get('class') if isinstance(backend, dict) else None
        if not isinstance(backend_entry, str) or not backend_entry.strip():
            errors.append(f'{where} backend.entry 缺失或无效')
        elif not _entry_is_safe(plugin_dir, backend_entry):
            errors.append(f'{where} backend.entry 不存在或越界: {backend_entry}')
        if not isinstance(class_name, str) or not class_name.strip():
            errors.append(f'{where} backend.class 缺失或无效')

        dependencies = data.get('dependencies', [])
        if not isinstance(dependencies, list) or not all(isinstance(dep, str) and dep for dep in dependencies):
            errors.append(f'{where} dependencies 必须是字符串数组')
            dependencies = []

        min_shell = data.get('minShellVersion')
        if min_shell is not None:
            if not isinstance(min_shell, str):
                errors.append(f'{where} minShellVersion 必须是字符串')
            elif parse_version(min_shell) > shell_version:
                errors.append(
                    f'{where} minShellVersion={min_shell} 高于当前 shell 版本 {get_shell_version()}'
                )

        if (plugin_dir / 'settings.json').exists():
            errors.append(f'{where} 插件目录残留 settings.json，应迁移到 SettingsStore')
        for py_file in plugin_dir.rglob('*.py'):
            try:
                code = py_file.read_text(encoding='utf-8')
            except Exception:
                continue
            for marker in LEGACY_SETTINGS_MARKERS:
                if marker in code:
                    errors.append(f'{where} {py_file.name} 仍包含旧设置文件代码标记 {marker!r}')
                    break
            if LOCAL_SIBLING_LOADER_MARKER in code:
                errors.append(f'{where} {py_file.name} 不应自定义 _load_sibling，请使用 shell.backend.plugin_utils.load_sibling')


        if data.get('kind') == 'local-adapter':
            warnings.append(
                f'{where} kind="local-adapter" 尚未实装（docs/adapter-spec.md 为规划中），当前声明不会生效'
            )
        if data.get('permissions'):
            warnings.append(
                f'{where} permissions 声明当前仅记录、尚未强制执行'
            )

        if isinstance(name, str) and PLUGIN_NAME_RE.fullmatch(name) and name == folder_name:
            discovered.append((plugin_dir, name, data))

    plugin_names = {name for _, name, _ in discovered}
    for plugin_dir, name, data in discovered:
        where = f'[{name}]'
        for dep in data.get('dependencies', []):
            if dep == name:
                errors.append(f'{where} dependencies 不能依赖自身: {dep!r}')
            elif dep not in plugin_names:
                errors.append(f'{where} 依赖的插件不存在: {dep!r}')

    if load_backends:
        for plugin_dir, name, data in discovered:
            where = f'[{name}]'
            backend = data.get('backend', {})
            entry = backend.get('entry')
            class_name = backend.get('class')
            if not isinstance(entry, str) or not isinstance(class_name, str):
                continue
            if not _entry_is_safe(plugin_dir, entry):
                continue
            try:
                cls = _load_backend_class(plugin_dir, entry, class_name)
            except Exception as exc:
                errors.append(f'{where} 后端加载失败: {exc}')
                continue
            for error in _check_schema(cls):
                errors.append(f'{where} {error}')
            if not callable(getattr(cls, 'register_api', None)):
                errors.append(f'{where} 后端类缺少 register_api 方法')

    return errors, warnings


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='校验 OmniBox 插件是否符合 manifest/代码规范')
    parser.add_argument('--plugins-dir', type=Path, default=DEFAULT_PLUGINS_DIR)
    parser.add_argument('--no-load', action='store_true', help='只做静态检查，不导入插件后端')
    args = parser.parse_args(list(argv) if argv is not None else None)

    errors, warnings = check_plugins(args.plugins_dir, load_backends=not args.no_load)
    for warning in warnings:
        print(f'[warning] {warning}')
    for error in errors:
        print(f'[error] {error}')
    if errors:
        print(f'check_plugins: {len(errors)} 个错误, {len(warnings)} 个警告')
        return 1
    print(f'check_plugins: OK ({len(warnings)} 个警告)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
