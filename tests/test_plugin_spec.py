"""插件规范检查器的单元测试。

运行：
    python -m unittest tests.test_plugin_spec -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.check_plugins import DEFAULT_PLUGINS_DIR, check_plugins


def _manifest(name: str, route: str, deps=None, version: str = '1.0.0', extra: dict | None = None) -> dict:
    data = {
        'name': name,
        'version': version,
        'displayName': name,
        'dependencies': deps if deps is not None else [],
        'backend': {'entry': 'backend/main.py', 'class': f'{name.title().replace("-", "")}Plugin'},
        'frontend': {'entry': 'frontend/index.html', 'route': route},
    }
    if extra:
        data.update(extra)
    return data


def _make_plugin(root: Path, name: str, manifest: dict | str, *, backend_code: str = '', legacy_settings_file: bool = False) -> Path:
    plugin_dir = root / name
    (plugin_dir / 'backend').mkdir(parents=True)
    (plugin_dir / 'frontend').mkdir()
    manifest_path = plugin_dir / 'manifest.json'
    if isinstance(manifest, str):
        manifest_path.write_text(manifest, encoding='utf-8')
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    (plugin_dir / 'frontend' / 'index.html').write_text('<html><head></head><body></body></html>', encoding='utf-8')
    (plugin_dir / 'backend' / 'main.py').write_text(backend_code or 'class Plugin:\n    pass\n', encoding='utf-8')
    if legacy_settings_file:
        (plugin_dir / 'settings.json').write_text('{}', encoding='utf-8')
    return plugin_dir


class PluginSpecCheckerTests(unittest.TestCase):
    def test_valid_plugin_passes_static_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'good-plugin', _manifest('good-plugin', '/good'))
            errors, warnings = check_plugins(root, load_backends=False)
            self.assertEqual(errors, [])
            self.assertEqual(warnings, [])

    def test_invalid_json_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'bad-json', '{not json')
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('manifest.json' in error for error in errors))

    def test_name_must_match_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'real-name', _manifest('other-name', '/real'))
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('必须与目录名一致' in error for error in errors))

    def test_duplicate_route_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'alpha', _manifest('alpha', '/same'))
            _make_plugin(root, 'beta', _manifest('beta', '/same'))
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('重复' in error for error in errors))

    def test_reserved_route_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'bad-route', _manifest('bad-route', '/settings'))
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('保留路由' in error for error in errors))

    def test_missing_dependency_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'needs-other', _manifest('needs-other', '/needs', deps=['missing-plugin']))
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('依赖的插件不存在' in error for error in errors))

    def test_min_shell_version_above_current_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'too-new', _manifest('too-new', '/too-new', extra={'minShellVersion': '999.0.0'}))
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('minShellVersion' in error for error in errors))

    def test_legacy_settings_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'legacy-file', _manifest('legacy-file', '/legacy'), legacy_settings_file=True)
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('settings.json' in error for error in errors))

    def test_legacy_settings_code_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code = "class Plugin:\n    def _save_settings_to_file(self):\n        self.settings_file = None\n"
            _make_plugin(root, 'legacy-code', _manifest('legacy-code', '/legacy-code'), backend_code=code)
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('旧设置文件代码标记' in error for error in errors))

    def test_bundled_plugins_pass_full_spec(self):
        errors, _ = check_plugins(DEFAULT_PLUGINS_DIR, load_backends=True)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
