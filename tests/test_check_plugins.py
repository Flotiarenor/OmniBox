"""`check_plugins` 的 chrome 声明与 settings_schema 校验契约。

运行：python -m unittest tests.test_check_plugins -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.check_plugins import (
    _check_chrome_schema,
    _check_freshness_contract,
    _check_schema,
    _check_shell_freshness_trigger,
    _strip_js_noise,
    check_plugins,
)


def _frontend(action: dict) -> dict:
    return {
        'chrome': {
            'brand': {'icon': 'icon:test', 'title': '测试'},
            'nav': [{'id': 'g', 'items': [{'id': 'i', 'label': '项'}]}],
            'toolbar': [action],
        },
    }


class ChromeToolbarPlacementTest(unittest.TestCase):
    def test_leading_and_trailing_are_allowed(self):
        for placement in ('leading', 'trailing'):
            with self.subTest(placement=placement):
                errors = _check_chrome_schema(
                    _frontend({'id': 'back', 'label': '返回', 'placement': placement}),
                    'p',
                )
                self.assertEqual(errors, [])

    def test_omitted_placement_is_still_valid(self):
        errors = _check_chrome_schema(
            _frontend({'id': 'back', 'label': '返回'}), 'p')
        self.assertEqual(errors, [])

    def test_invalid_placement_is_an_error(self):
        errors = _check_chrome_schema(
            _frontend({'id': 'back', 'label': '返回', 'placement': 'left'}), 'p')
        self.assertTrue(
            any('placement' in error and 'leading' in error for error in errors),
            f'应拒绝非法 placement，实际 errors={errors}',
        )

    def test_unknown_action_fields_are_still_rejected(self):
        errors = _check_chrome_schema(
            _frontend({'id': 'back', 'label': '返回', 'mystery': 1}), 'p')
        self.assertTrue(any('未知字段' in error for error in errors), errors)


_CHROME_BASE = {
    'brand': {'icon': 'icon:test', 'title': '测试'},
    'nav': [{'id': 'g', 'items': [{'id': 'i', 'label': '项'}]}],
    'toolbar': [{'id': 'a', 'label': 'A'}],
}


class ChromeDeclTypeErrorTests(unittest.TestCase):
    """chrome 声明里的类型错误必须被报成字段错误，而不是让检查器抛异常。

    `content` / `variant` / `kind` / `placement` / `status.kind` 的取值校验原先直接
    做 `值 in frozenset(...)`：值是数组或对象时不可哈希，检查器抛
    `TypeError: unhashable type`。manifest 是校验器的**输入**，类型写错正是它要
    报告的事情；抛异常会中断整次检查，其余插件的错误全都看不到。
    """

    def _errors(self, **overrides) -> list:
        chrome = {**_CHROME_BASE, **overrides}
        return _check_chrome_schema({'chrome': chrome}, 'p')

    def test_unhashable_action_enums_are_field_errors(self):
        cases = {
            'variant': (['primary'], 'variant'),
            'kind': ({'button': True}, 'kind'),
            'placement': (['leading'], 'placement'),
        }
        for field, (value, needle) in cases.items():
            with self.subTest(field=field):
                errors = self._errors(toolbar=[{'id': 'a', 'label': 'A', field: value}])
                self.assertTrue(any(needle in e for e in errors), errors)

    def test_unhashable_status_kind_is_a_field_error(self):
        errors = self._errors(status={'kind': ['text']})
        self.assertTrue(any('status.kind' in e for e in errors), errors)


class InvalidChromeManifestIntegrationTests(unittest.TestCase):
    """整条 `check_plugins` 对非法 chrome 声明也必须只报错、不中断。"""

    def _run_static_check(self, frontend_extra: dict):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / 'probe'
            plugin.mkdir()
            (plugin / 'manifest.json').write_text(json.dumps({
                'name': 'probe',
                'version': '1.0.0',
                'displayName': '探针',
                'backend': {'entry': 'backend/main.py', 'class': 'Plugin'},
                'frontend': {
                    'entry': 'frontend/index.html',
                    'route': '/probe',
                    **frontend_extra,
                },
            }, ensure_ascii=False), encoding='utf-8')
            return check_plugins(Path(tmp), load_backends=False)

    def test_non_object_chrome_is_reported_without_crash(self):
        # chrome 不是对象时，`_check_freshness_contract` 的 shell_mounts 探测曾对
        # list 调 .get()，整条检查以 AttributeError 结束。
        errors, _warnings = self._run_static_check({'chrome': ['not', 'a', 'dict']})
        self.assertTrue(any('frontend.chrome 必须是对象' in e for e in errors), errors)

    def test_unhashable_status_kind_is_reported_without_crash(self):
        errors, _warnings = self._run_static_check({
            'chrome': {
                'brand': {'icon': 'icon:test', 'title': '测试'},
                'nav': [{'id': 'g', 'items': [{'id': 'i', 'label': '项'}]}],
                'status': {'kind': {'mode': 'eq-text'}},
            },
        })
        self.assertTrue(any('status.kind' in e for e in errors), errors)


class RemovedChromeFieldsTests(unittest.TestCase):
    """已删除的 chrome 字段必须变成"未知字段/非法值"，不能继续被接受。

    `frontend.chrome.content`、`nav[].dynamic` 与 `status.kind: "text"` 从未有运行时
    消费方（壳按是否声明 chrome 与 `get_extensions().native` 决定内容形态；nav 运行期
    补丁不需要 dynamic；状态栏除 `eq-text` 外只有一种渲染）。字段已从插件 manifest 与
    检查器里删除，这里锁住它们不会再被静默接受。
    """

    def _errors(self, **overrides) -> list:
        chrome = {**_CHROME_BASE, **overrides}
        return _check_chrome_schema({'chrome': chrome}, 'p')

    def test_chrome_content_is_unknown_field(self):
        errors = self._errors(content='canvas')
        self.assertTrue(any('未知字段' in error and 'content' in error for error in errors),
                        errors)

    def test_nav_dynamic_is_unknown_field(self):
        errors = self._errors(nav=[{
            'id': 'g', 'dynamic': True, 'items': [{'id': 'i', 'label': '项'}],
        }])
        self.assertTrue(any('未知字段' in error and 'dynamic' in error for error in errors),
                        errors)

    def test_status_kind_text_is_rejected(self):
        errors = self._errors(status={'kind': 'text'})
        self.assertTrue(any('status.kind' in error for error in errors), errors)

    def test_action_kind_text_is_still_valid(self):
        """action 的 `kind: "text"` 是另一份枚举（顶栏计数文案），仍在用。"""
        errors = self._errors(toolbar=[
            {'id': 'selection-count', 'label': '已选 0 项', 'kind': 'text'},
        ])
        self.assertEqual(errors, [])


class SchemaFieldTypeGuardTests(unittest.TestCase):
    """`settings_schema[].type` 写错时必须报字段错误，不能让检查器中断。

    `type` 是插件作者手写的 Python 值（schema 在类属性里，不经 JSON 解析），
    写成 `[]` / `{}` 时 `field_type not in ALLOWED_SCHEMA_TYPES` 会抛
    `TypeError: unhashable type`；`field_type in {'number','range'}` 同样会炸。
    检查器的输入写错正是它要报告的事情，不能以异常结束。
    """

    def test_list_type_is_reported_not_raised(self):
        class _Plugin:
            settings_schema: ClassVar[list] = [
                {'key': 'threshold', 'label': '阈值', 'type': []},
            ]

        errors = _check_schema(_Plugin)
        self.assertTrue(any('.type' in error for error in errors), errors)

    def test_dict_type_is_reported_not_raised(self):
        class _Plugin:
            settings_schema: ClassVar[list] = [
                {'key': 'threshold', 'label': '阈值', 'type': {'kind': 'range'}},
            ]

        errors = _check_schema(_Plugin)
        self.assertTrue(any('.type' in error for error in errors), errors)


class ShellFreshnessTriggerTests(unittest.TestCase):
    """壳挂载型统一刷新必须真的触发被动同步，且挂载后与视图 / 作用域变化各一次。

    `Freshness.mount(...)` 只渲染控件并读一次 `system_freshness_state`，它自己不扫盘；
    被动同步（`system_freshness_sync`）只有 `.autoSync(...)` / `Freshness.sync(...)`
    会发起。漏掉时控件、按钮、状态文案、后端端点全都正常，唯一的现象是"外部新增的
    文件永远看不到，必须 Ctrl+F5"——所以门禁必须静态拦住"只挂载不触发"。

    比"文本里出现过 autoSync"更强的三条：
      * 注释与字符串先被清洗掉（`// ctl.autoSync(...)`、`'autoSync('`、模板串都不算）；
      * 只认带接收者的**调用**（`.autoSync(`），所以 `function autoSync(...)` 与
        `autoSync(scope) {}` 这类"只定义未调用"也不算；
      * 只挂载（首帧）之外还必须 `watch([activeView, freshnessScope])`：删掉这条
        watch，切目录 / 从别的插件切回来就再也不同步了。

    正例读的是仓库里真实的 `PluginChrome.vue`：实现被回退时这里先红，而不是等到
    用户发现界面不更新。反例用临时组件文件，避免为了造一个坏壳去改真实组件。
    """

    WATCH = "watch([activeView, freshnessScope], () => syncFreshness());\n"
    MOUNT_ONLY = "const ctl = Freshness.mount({ plugin: props.plugin.name });\n" + WATCH
    OK = (
        "const ctl = Freshness.mount({ plugin: props.plugin.name });\n"
        "const target = freshnessSyncTarget(ctl, scope, {});\n"
        "target.controller.autoSync(target.scope);\n"
        + WATCH
    )

    def _component(self, tmp: str, body: str) -> Path:
        path = Path(tmp) / 'PluginChrome.vue'
        path.write_text(body, encoding='utf-8')
        return path

    def _errors(self, body: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            return _check_shell_freshness_trigger(self._component(tmp, body))

    def test_mount_without_trigger_is_an_error(self):
        errors = self._errors(self.MOUNT_ONLY)
        self.assertTrue(any('autoSync' in error for error in errors), errors)

    def test_trigger_without_view_watch_is_an_error(self):
        """只挂载时同步一次不够：删掉 watch 必须变红。"""
        errors = self._errors(
            "const target = freshnessSyncTarget(ctl, scope, {});\n"
            "target.controller.autoSync(target.scope);\n")
        self.assertTrue(any('watch' in error and 'freshnessScope' in error for error in errors), errors)

    def test_commented_out_call_is_an_error(self):
        """注释掉的调用不算触发（原实现的正则会误判为已接线）。"""
        errors = self._errors(
            "const ctl = Freshness.mount({ plugin: 'p' });\n"
            "// ctl.autoSync(scope)\n"
            "/* ctl.autoSync(scope) */\n"
            "<!-- ctl.autoSync(scope) -->\n"
            + self.WATCH)
        self.assertTrue(any('autoSync' in error for error in errors), errors)

    def test_string_literals_do_not_count(self):
        """单 / 双引号串与模板串里的同名字样不算触发。"""
        errors = self._errors(
            "const ctl = Freshness.mount({ plugin: 'p' });\n"
            "const hint = 'ctl.autoSync(scope)';\n"
            "const other = \"ctl.autoSync(scope)\";\n"
            "const tip = `ctl.autoSync(scope)`;\n"
            + self.WATCH)
        self.assertTrue(any('autoSync' in error for error in errors), errors)

    def test_definition_without_call_is_an_error(self):
        """只定义未调用（函数声明 / 对象方法）不算触发。"""
        errors = self._errors(
            "function autoSync(scope) { return scope; }\n"
            "const ctl = { autoSync(scope) { return scope; } };\n"
            "const mount = Freshness.mount({ plugin: 'p' });\n"
            + self.WATCH)
        self.assertTrue(any('autoSync' in error for error in errors), errors)

    def test_full_trigger_wiring_passes(self):
        self.assertEqual(self._errors(self.OK), [])

    def test_missing_component_is_not_reported_here(self):
        """组件不存在由挂载检查单独报，这里不重复同一件事。"""
        with tempfile.TemporaryDirectory() as tmp:
            errors = _check_shell_freshness_trigger(Path(tmp) / 'absent.vue')
        self.assertEqual(errors, [])

    def test_real_shell_component_triggers_sync(self):
        """真实组件必须过：这条是正例，锁住实现不被回退成"只挂载不触发"。"""
        self.assertEqual(_check_shell_freshness_trigger(), [])


class StripJsNoiseTests(unittest.TestCase):
    """清洗函数本身要可断言：注释/字符串被抹掉、代码与换行保留。"""

    def test_comments_and_strings_are_removed(self):
        source = (
            "const a = 'ctl.autoSync(1)';\n"
            "// ctl.autoSync(2)\n"
            "const b = `ctl.autoSync(3)`;\n"
            "/* ctl.autoSync(4) */\n"
            "<!-- ctl.autoSync(5) -->\n"
            "ctl.autoSync(6);\n"
        )
        cleaned = _strip_js_noise(source)
        self.assertNotIn('autoSync(1', cleaned)
        self.assertNotIn('autoSync(2', cleaned)
        self.assertNotIn('autoSync(3', cleaned)
        self.assertNotIn('autoSync(4', cleaned)
        self.assertNotIn('autoSync(5', cleaned)
        self.assertIn('ctl.autoSync(6)', cleaned)
        self.assertEqual(cleaned.count('\n'), source.count('\n'), '换行必须保留（报错行号才对得上）')

    def test_keep_strings_keeps_plugin_name(self):
        source = "Freshness.mount({ plugin: 'demo', unit: '首' });\n// 注释"
        cleaned = _strip_js_noise(source, keep_strings=True)
        self.assertIn("plugin: 'demo'", cleaned)
        self.assertNotIn('注释', cleaned)


class PluginFreshnessTriggerTests(unittest.TestCase):
    """自绘型（没有 `frontend.chrome.freshness`）必须在插件自己的前端里触发被动同步。"""

    BACKEND = (
        "class DemoPlugin:\n"
        "    def freshness_spec(self):\n"
        "        return {'roots': []}\n"
    )
    MOUNT = (
        "const ctl = Freshness.mount({ plugin: 'demo', unit: '项' });\n"
    )

    def _plugin(self, tmp: str, frontend: str, *, name: str = 'demo',
                script_name: str = 'app.js') -> Path:
        root = Path(tmp) / name
        (root / 'backend').mkdir(parents=True)
        (root / 'backend' / 'main.py').write_text(self.BACKEND, encoding='utf-8')
        (root / 'frontend' / 'js').mkdir(parents=True)
        (root / 'frontend' / 'js' / script_name).write_text(frontend, encoding='utf-8')
        return root

    def _errors(self, frontend: str, *, shell_mounts: bool = False) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._plugin(tmp, frontend)
            return _check_freshness_contract(root, 'demo', shell_mounts=shell_mounts)

    def test_mount_without_trigger_is_reported(self):
        errors = self._errors(self.MOUNT)
        self.assertTrue(any('被动同步' in error for error in errors), errors)

    def test_auto_sync_call_passes(self):
        self.assertEqual(self._errors(self.MOUNT + "ctl.autoSync('');\n"), [])

    def test_module_level_freshness_sync_passes(self):
        """内容页拿不到句柄时，`Freshness.sync(plugin, scope)` 是合法触发。"""
        code = self.MOUNT + "async function go() { await Freshness.sync('demo', ''); }\n"
        self.assertEqual(self._errors(code), [])

    def test_commented_or_quoted_trigger_is_reported(self):
        for frontend in (
            self.MOUNT + "// ctl.autoSync('')\n",
            self.MOUNT + "const hint = 'ctl.autoSync()';\n",
            self.MOUNT + "const tip = `Freshness.sync('demo')`;\n",
            self.MOUNT + "function autoSync(scope) { return scope; }\n",
        ):
            with self.subTest(frontend=frontend):
                errors = self._errors(frontend)
                self.assertTrue(any('被动同步' in error for error in errors), errors)

    def test_shell_mounted_plugin_is_checked_by_the_shell_component(self):
        """壳挂载型的触发在 PluginChrome 里查，插件前端不重复要求。"""
        self.assertEqual(self._errors(self.MOUNT, shell_mounts=True), [])

    def test_real_repo_plugins_pass(self):
        """真实插件逐个过：自绘型（image-cleaner / netease-music / pixiv-sync）与壳挂载型。

        这条是正例：任一插件的触发被删掉/注释掉时它会红，而不是等用户发现界面不更新。
        """
        plugin_roots = sorted(
            path for path in (PROJECT_ROOT / 'plugins').iterdir()
            if path.is_dir() and (path / 'manifest.json').is_file())
        self.assertGreaterEqual(len(plugin_roots), 8, '插件目录数量异常，用例会变成空转')
        for root in plugin_roots:
            with self.subTest(plugin=root.name):
                manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
                frontend = manifest.get('frontend') or {}
                chrome = frontend.get('chrome') if isinstance(frontend, dict) else None
                shell_mounts = bool(isinstance(chrome, dict) and chrome.get('freshness'))
                backend = ''.join(
                    path.read_text(encoding='utf-8')
                    for path in sorted((root / 'backend').rglob('*.py')))
                if 'def freshness_spec' not in backend:
                    continue
                errors = _check_freshness_contract(
                    root, root.name, shell_mounts=shell_mounts)
                self.assertEqual(errors, [], f'{root.name}: {errors}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
