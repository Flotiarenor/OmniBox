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

from tools.check_plugins import _check_chrome_schema, _check_schema, check_plugins


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


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
