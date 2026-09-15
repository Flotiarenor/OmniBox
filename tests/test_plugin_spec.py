"""插件规范检查器的单元测试。

运行：
    python -m unittest tests.test_plugin_spec -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.check_plugins import DEFAULT_PLUGINS_DIR, _check_manifest_fields, check_plugins


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
        """按指南书写的 manifest 不得有 error。

        不断言 warnings 为空：version / frontend.entry 归 CHECKER_ONLY_FIELDS，
        它们确实只被检查器消费，必须如实告警而不是静默通过（见 1.4.c）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'good-plugin', _manifest('good-plugin', '/good'))
            errors, _ = check_plugins(root, load_backends=False)
            self.assertEqual(errors, [])

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

    # ===== 凭据类设置项必须申请 Shell 文件防护（PluginBase.get_protected_paths）=====
    #
    # 这条规则的意义在于"下一个插件不会再犯"：凭据值会落到
    # <config>/plugins/<name>.json，而该文件（或它所在的目录）可能正好落在某个
    # 插件的媒体根之内 —— 那是结构性的，靠 review 记得不住。
    # pixiv-sync 当前靠 test_bundled_plugins_pass_full_spec 覆盖：它的
    # refresh_token 一旦去掉 "secret": True，上面那条用例立刻报错。

    def _plugin_with_schema(self, root: Path, name: str, schema: str):
        """写一个真的继承 PluginBase 的最小后端（检查器会校验这一继承关系）。"""
        class_name = f"{name.title().replace('-', '')}Plugin"
        code = (
            "from shell.backend.plugin_base import PluginBase\n"
            "\n"
            "\n"
            f"class {class_name}(PluginBase):\n"
            f"    settings_schema = {schema}\n"
            "\n"
            "    def register_api(self):\n"
            "        return {}\n"
        )
        _make_plugin(root, name, _manifest(name, f'/{name}'), backend_code=code)

    def test_credential_key_without_secret_flag_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._plugin_with_schema(root, 'leaky', '[{"key": "refresh_token", "type": "text"}]')
            errors, _ = check_plugins(root, load_backends=True)
            self.assertTrue(any('secret' in error and 'refresh_token' in error for error in errors),
                            f'应拦住未申报的凭据类设置项，实际 errors={errors}')

    def test_credential_key_with_secret_flag_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._plugin_with_schema(
                root, 'safe',
                '[{"key": "refresh_token", "type": "text", "secret": True}]')
            errors, _ = check_plugins(root, load_backends=True)
            self.assertEqual(errors, [])

    def test_non_credential_key_needs_no_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._plugin_with_schema(root, 'ordinary', '[{"key": "download_dir", "type": "text"}]')
            errors, _ = check_plugins(root, load_backends=True)
            self.assertEqual(errors, [])

    def test_non_bool_secret_is_an_error(self):
        """写成字符串 "false" 是真值 → 会得到与作者意图相反的结论。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._plugin_with_schema(
                root, 'stringy',
                '[{"key": "api_token", "type": "text", "secret": "false"}]')
            errors, _ = check_plugins(root, load_backends=True)
            self.assertTrue(any('secret 应为 bool' in error for error in errors),
                            f'应拦住非 bool 的 secret，实际 errors={errors}')

    def test_credential_key_variants_are_caught(self):
        """键名的大小写与分隔符不得成为盲区。

        早期用的是 `(?:^|_)(?:token|…)(?:$|_)` 且区分大小写，实测
        `API_TOKEN` / `Api_Token` / `refreshToken` / `privateKey` / `PASSWORD`
        全部漏过 —— 而"凭据类键名却没申报"正是这条规则唯一要防的事。
        """
        variants = ['API_TOKEN', 'Api_Token', 'refreshToken', 'privateKey',
                    'PASSWORD', 'sessionId', 'authCookie']
        for index, key in enumerate(variants):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._plugin_with_schema(
                    root, f'variant{index}',
                    f'[{{"key": "{key}", "type": "text"}}]')
                errors, _ = check_plugins(root, load_backends=True)
                self.assertTrue(
                    any('secret' in error and key in error for error in errors),
                    f'{key} 未被门禁拦住，errors={errors}')

    def test_ordinary_keys_are_not_flagged_as_credentials(self):
        """不搞过宽的匹配：把普通设置项误判成凭据会让作者去改无意义的键名。"""
        from tools.check_plugins import looks_like_secret_key
        for key in ['per_page', 'root_dir', 'sort_by', 'download_dir', 'proxy',
                    'keyboard_shortcut', 'media_roots', 'row_height']:
            with self.subTest(key=key):
                self.assertFalse(looks_like_secret_key(key))

    # ===== manifest 字段必须有读取方（docs/code-review.md §5） =====

    def test_unregistered_manifest_field_is_an_error(self):
        """指南教了"代码不读"的字段 → 作者填了没效果，必须由检查器拦住。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_plugin(root, 'unknown-field', _manifest('unknown-field', '/unknown', extra={'mystery': 1}))
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('没有任何代码读取它' in error for error in errors))

    def test_doc_only_field_warns_but_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _manifest('doc-only', '/doc-only', extra={'description': '说明文字'})
            _make_plugin(root, 'doc-only', manifest)
            errors, warnings = check_plugins(root, load_backends=False)
            self.assertEqual(errors, [])
            self.assertTrue(any('运行时无效果' in warning for warning in warnings))

    def test_version_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = _manifest('no-version', '/no-version')
            del manifest['version']
            _make_plugin(root, 'no-version', manifest)
            errors, _ = check_plugins(root, load_backends=False)
            self.assertTrue(any('version' in error for error in errors))

    def test_reader_registry_matches_current_code(self):
        """登记表必须与真实代码一致（否则它就只是一张没人维护的表格）。"""
        from tools.check_plugins import _check_reader_registry
        self.assertEqual(_check_reader_registry(), [])

    # ===== runtime 块与父键回退（docs/core-contract-fixes.md §1） =====

    def test_runtime_block_produces_no_errors(self):
        """按 docs/plugin-guide.md §2.2 书写的 runtime 块曾拿到 6 条 error。

        runtime 是整块登记进 DOC_ONLY_FIELDS 的，摊平出来的 runtime.kind 等子字段必须
        逐级回退命中父键；否则照文档写的 manifest 过不了门禁（image-tagger 被阻断）。
        """
        manifest = _manifest('runtime-probe', '/runtime-probe', extra={
            'runtime': {
                'kind': 'python-venv',
                'entry': 'backend/runtime/worker.py',
                'venv': 'backend/runtime/venv',
                'requirements': 'backend/runtime/requirements.txt',
                'startup': 'manual',
                'timeoutSeconds': 30,
            },
        })
        data = dict(manifest)
        data.pop('name', None)  # 直接测字段判定，与插件目录结构无关
        errors, warnings = _check_manifest_fields(data, '[runtime-probe]')
        self.assertEqual(
            [error for error in errors if 'runtime' in error], [],
            f'unregistered runtime.* fields must not be errors, got {errors}',
        )
        self.assertTrue(any('manifest.runtime.kind' in warning for warning in warnings))

    def test_fallback_does_not_excuse_unknown_fields(self):
        """父键回退不得过宽：未登记的字段仍然必须是 error。"""
        data = {'runtimeFoo': 1, 'mystery': 2, 'nested': {'sub': 3}}
        errors, _ = _check_manifest_fields(data, '[unknown]')
        for field in ('runtimeFoo', 'mystery', 'nested.sub'):
            self.assertTrue(
                any(f'manifest.{field}' in error for error in errors),
                f'manifest.{field} 必须报 error，实际: {errors}',
            )

    def test_checker_only_fields_warn_about_no_runtime_effect(self):
        """只被检查器消费的字段必须告警，不能静默通过。"""
        data = {
            'version': '1.0.0',
            'minShellVersion': '1.2.0',
            'kind': 'local-adapter',
            'frontend': {'route': '/x', 'entry': 'frontend/index.html'},
        }
        errors, warnings = _check_manifest_fields(data, '[checker-only]')
        self.assertEqual(errors, [])
        for field in ('version', 'minShellVersion', 'kind', 'frontend.entry'):
            matched = [w for w in warnings if f'manifest.{field} ' in w]
            self.assertTrue(matched, f'manifest.{field} 必须产出 warning，实际: {warnings}')
            self.assertTrue(
                '运行时无效果' in matched[0],
                f'warning 文本必须说明运行时无效果: {matched[0]}',
            )

    def test_runtime_is_not_registered_as_runtime_reader(self):
        """runtime 若被塞进 RUNTIME_FIELD_READERS 就等于规则空转满足。"""
        from tools import check_plugins as checker
        self.assertNotIn('runtime', checker.RUNTIME_FIELD_READERS)
        self.assertIn('runtime', checker.DOC_ONLY_FIELDS)

    def test_stale_reader_registry_is_detected(self):
        from tools import check_plugins as checker
        with mock.patch.dict(
            checker.RUNTIME_FIELD_READERS,
            {'ghost': [('shell/backend/plugin_manager.py', '这段代码不存在')]},
            clear=False,
        ):
            errors = checker._check_reader_registry()
        self.assertTrue(any('登记表过期' in error for error in errors))


if __name__ == '__main__':
    unittest.main()
