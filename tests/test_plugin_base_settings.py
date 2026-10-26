"""`PluginBase.save_settings` 的服务端设置校验契约。

锁住三组语义：

  1. `number` / `range` 按 `settings_schema` 的 `min` / `max` 校验；非法值返回
     字段级错误且**整次调用不落盘**（不能出现"一半字段写进去"）；
  2. `secret` 的写入语义：键缺失 / 掩码 = 不改动，空字符串 = 清除，非空值 = 覆盖；
     其中"空字符串=清除"是 pixiv-sync 撤销 Refresh Token 的既有能力；
  3. `admin_only` 仍按当前主体角色判定；失败时 `errors` 按字段给出文案，同时保留
     可直接展示的 `error` 汇总。

运行：
    python -m unittest tests.test_plugin_base_settings -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend import plugin_base as plugin_base_module
from shell.backend.plugin_base import SECRET_MASK, PluginBase
from shell.backend.principal import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    PrincipalContext,
    use_principal,
)
from shell.backend.settings_store import SettingsStore

SCHEMA: List[Dict[str, Any]] = [
    {'key': 'per_page', 'label': '每页数量', 'type': 'number', 'min': 1, 'max': 500, 'default': 40},
    {'key': 'threshold', 'label': '阈值', 'type': 'range', 'min': 0, 'max': 16, 'default': 8},
    {'key': 'ratio', 'label': '比例', 'type': 'number', 'min': 0.5, 'max': 2.5},
    {'key': 'min_only', 'label': '只有下限', 'type': 'number', 'min': 0},
    {'key': 'max_only', 'label': '只有上限', 'type': 'number', 'max': 10},
    {'key': 'unbounded', 'label': '无边界', 'type': 'number'},
    {'key': 'label', 'label': '文本', 'type': 'text', 'min': 5, 'max': 10},
    {'key': 'api_key', 'label': '密钥', 'type': 'text', 'secret': True},
    {'key': 'root_dir', 'label': '根目录', 'type': 'text', 'admin_only': True},
    {'key': 'admin_token', 'label': '管理员令牌', 'type': 'text',
     'secret': True, 'admin_only': True},
    {'key': 'info_row', 'label': '只读行', 'type': 'info'},
]


class _SettingsPlugin(PluginBase):
    """形状最小的宿主：只声明 schema，并记录 on_settings_changed 的调用。"""

    def __init__(self, manifest, config, schema):
        super().__init__(manifest, config)
        self.settings_schema = schema
        self.changed_calls: List[set] = []

    def register_api(self):
        return {'get_settings': self.get_settings, 'save_settings': self.save_settings}

    def on_settings_changed(self, changed_keys):
        self.changed_calls.append(set(changed_keys))


def _principal(role: str, name: str = '测试主体') -> PrincipalContext:
    return PrincipalContext(id=name, name=name, role=role, source='enrolled')


class _SettingsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.plugin = self._new_plugin()

    def _new_plugin(self, schema: List[Dict[str, Any]] | None = None,
                    name: str = 'demo') -> _SettingsPlugin:
        plugin = _SettingsPlugin(
            {'name': name},
            {'directories': {'data_root': str(self.tmp / f'data-{name}')}},
            SCHEMA if schema is None else schema,
        )
        plugin._settings_store = SettingsStore(str(self.tmp / f'config-{name}'))
        return plugin

    def _stored(self) -> Dict[str, Any]:
        return self.plugin._settings_store.get('demo') or {}


class NumberRangeValidationTests(_SettingsTest):
    def test_bounds_are_inclusive(self):
        self.assertEqual(self.plugin.save_settings({'per_page': 1}), {'success': True})
        self.assertEqual(self.plugin.save_settings({'per_page': 500}), {'success': True})
        self.assertEqual(self.plugin.save_settings({'threshold': 0, 'ratio': 0.5}), {'success': True})
        self.assertEqual(self.plugin.save_settings({'threshold': 16, 'ratio': 2.5}), {'success': True})

        stored = self._stored()
        self.assertEqual(stored['per_page'], 500)
        self.assertEqual(stored['threshold'], 16)
        self.assertEqual(stored['ratio'], 2.5)

    def test_out_of_range_values_are_rejected_per_field(self):
        cases = [
            ({'per_page': 0}, 'per_page', '1'),
            ({'per_page': 501}, 'per_page', '500'),
            ({'threshold': -1}, 'threshold', '0'),
            ({'threshold': 17}, 'threshold', '16'),
            ({'ratio': 0.49}, 'ratio', '0.5'),
            ({'ratio': 2.51}, 'ratio', '2.5'),
            ({'min_only': -1}, 'min_only', '0'),
            ({'max_only': 11}, 'max_only', '10'),
        ]
        for payload, key, bound in cases:
            with self.subTest(payload=payload):
                result = self.plugin.save_settings(payload)
                self.assertFalse(result['success'])
                self.assertIn(key, result['errors'])
                self.assertIn(bound, result['errors'][key])
                self.assertIn(key, result['error'])
                self.assertNotIn(key, self._stored())

    def test_non_numeric_values_are_rejected(self):
        for value in ('many', '', None, True, [], {}):
            with self.subTest(value=value):
                result = self.plugin.save_settings({'per_page': value})
                self.assertFalse(result['success'])
                self.assertIn('必须是数字', result['errors']['per_page'])
                self.assertNotIn('per_page', self._stored())

    def test_non_finite_values_are_rejected(self):
        for value in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(value=value):
                result = self.plugin.save_settings({'unbounded': value})
                self.assertFalse(result['success'])
                self.assertIn('必须是数字', result['errors']['unbounded'])

    def test_invalid_value_prevents_any_write(self):
        self.assertEqual(self.plugin.save_settings({'per_page': 40}), {'success': True})
        self.plugin.changed_calls.clear()
        before = dict(self._stored())
        result = self.plugin.save_settings({'per_page': 41, 'threshold': 99})
        self.assertFalse(result['success'])
        self.assertEqual(self._stored(), before, '一次失败调用写入了部分字段')
        self.assertEqual(self.plugin.changed_calls, [], '失败调用不该触发 on_settings_changed')

    def test_numeric_strings_are_accepted_and_normalised(self):
        values = {'per_page': '42', 'threshold': '12', 'ratio': '1.5'}
        self.assertEqual(self.plugin.save_settings(values), {'success': True})
        stored = self._stored()
        self.assertEqual(stored['per_page'], 42)
        self.assertIsInstance(stored['per_page'], int)
        self.assertEqual(stored['threshold'], 12)
        self.assertEqual(stored['ratio'], 1.5)
        self.assertEqual(values, {'per_page': '42', 'threshold': '12', 'ratio': '1.5'},
                         'save_settings 不得改写调用方传入的字典')

    def test_existing_out_of_range_value_round_trips_but_cannot_be_introduced(self):
        """历史配置里的越界值原样保存不报错；从合法值改成越界值仍然拒绝。

        group-mesh 用 `port=0` 表示"由内核分配端口"，而 schema 的 min 是 1024：
        设置面板整体提交时不能因为一条历史值让"什么都不改"也保存失败。
        """
        self.plugin.update_setting('per_page', 0)          # 运行期状态：绕过 schema
        self.assertEqual(self.plugin.save_settings({'per_page': 0}), {'success': True})
        self.assertEqual(self._stored()['per_page'], 0)

        self.plugin.update_setting('per_page', 40)
        result = self.plugin.save_settings({'per_page': 0})
        self.assertFalse(result['success'])
        self.assertIn('per_page', result['errors'])
        self.assertEqual(self._stored()['per_page'], 40)

    def test_text_field_ignores_number_bounds(self):
        self.assertEqual(self.plugin.save_settings({'label': 'x'}), {'success': True})
        self.assertEqual(self._stored()['label'], 'x')

    def test_unbounded_number_accepts_finite_values(self):
        self.assertEqual(self.plugin.save_settings({'unbounded': -12.5}), {'success': True})
        self.assertEqual(self._stored()['unbounded'], -12.5)

    def test_error_names_field_and_received_value(self):
        result = self.plugin.save_settings({'per_page': 0})
        message = result['errors']['per_page']
        self.assertIn('per_page', message)
        self.assertIn('每页数量', message)
        self.assertIn('0', message)
        self.assertIn('1–500', message)


class NumberTypeEdgeTests(_SettingsTest):
    """极端取值：超长数字串、超大整数、嵌套对象、min>max 的 schema。"""

    def test_trailing_garbage_is_not_a_number(self):
        for value in ('12abc', '12,5', '0x10', '1 2', '--3'):
            with self.subTest(value=value):
                result = self.plugin.save_settings({'per_page': value})
                self.assertFalse(result['success'])
                self.assertIn('per_page', result['errors'])
                self.assertNotIn('per_page', self._stored())

    def test_overlong_numeric_string_is_rejected(self):
        result = self.plugin.save_settings({'per_page': '9' * 400})
        self.assertFalse(result['success'])
        self.assertIn('必须是数字', result['errors']['per_page'])

    def test_huge_integer_is_rejected(self):
        """超出 float 可表示范围的大整数不能悄悄存成 inf。"""
        result = self.plugin.save_settings({'per_page': 10 ** 400})
        self.assertFalse(result['success'])
        self.assertIn('per_page', result['errors'])

    def test_nested_mapping_is_rejected(self):
        for value in ({'value': 40}, [40], (40,)):
            with self.subTest(value=value):
                result = self.plugin.save_settings({'per_page': value})
                self.assertFalse(result['success'])
                self.assertIn('per_page', result['errors'])

    def test_schema_min_greater_than_max_rejects_untouched_default(self):
        """schema 写反时 check_plugins 会拒绝；这里锁住运行时的兜底语义。"""
        schema = [{'key': 'broken', 'label': '倒置边界', 'type': 'number',
                   'min': 10, 'max': 2, 'default': 5}]
        plugin = self._new_plugin(schema, name='broken')
        # 默认值 5 是历史生效值，原样回传不报错（否则打开设置就保存失败）
        self.assertEqual(plugin.save_settings({'broken': 5}), {'success': True})
        # 任何"新"取值都落在 10–2 之外，按越界拒绝
        result = plugin.save_settings({'broken': 6})
        self.assertFalse(result['success'])
        self.assertIn('broken', result['errors'])


class SecretSemanticsTests(_SettingsTest):
    def _seed(self, value: str = 'REAL-TOKEN') -> None:
        self.plugin.update_setting('api_key', value)

    def test_mask_round_trip_keeps_existing_secret(self):
        self._seed()
        result = self.plugin.save_settings({'api_key': SECRET_MASK, 'per_page': 10})
        self.assertEqual(result, {'success': True})
        self.assertEqual(self._stored()['api_key'], 'REAL-TOKEN')
        self.assertEqual(self._stored()['per_page'], 10)

    def test_absent_key_keeps_existing_secret(self):
        self._seed()
        self.assertEqual(self.plugin.save_settings({'per_page': 10}), {'success': True})
        self.assertEqual(self._stored()['api_key'], 'REAL-TOKEN')

    def test_empty_string_clears_secret(self):
        self._seed()
        self.assertEqual(self.plugin.save_settings({'api_key': ''}), {'success': True})
        self.assertEqual(self._stored()['api_key'], '')
        self.assertFalse(self.plugin.setting('api_key'), '清除后 setting() 应读到假值')
        self.assertEqual(self.plugin.get_settings()['api_key'], '')

    def test_new_value_replaces_secret(self):
        self._seed()
        self.assertEqual(self.plugin.save_settings({'api_key': 'NEW-TOKEN'}), {'success': True})
        self.assertEqual(self._stored()['api_key'], 'NEW-TOKEN')

    def test_mask_without_existing_value_writes_nothing(self):
        self.assertEqual(self.plugin.save_settings({'api_key': SECRET_MASK}), {'success': True})
        self.assertNotIn('api_key', self._stored())

    def test_clearing_secret_is_reported_as_change(self):
        self._seed()
        self.assertEqual(self.plugin.save_settings({'api_key': ''}), {'success': True})
        self.assertEqual(self.plugin.changed_calls, [{'api_key'}])


class AdminOnlyValidationTests(_SettingsTest):
    def test_member_is_denied_with_field_error_and_no_write(self):
        member = _principal(ROLE_MEMBER)
        with use_principal(member):
            result = self.plugin.save_settings({'root_dir': '/etc', 'per_page': 10})
        self.assertFalse(result['success'])
        self.assertIn('root_dir', result['errors'])
        self.assertIn('管理员', result['errors']['root_dir'])
        self.assertIn('测试主体', result['errors']['root_dir'])
        self.assertIn('member', result['errors']['root_dir'])
        self.assertNotIn('root_dir', self._stored(), '越权写入仍然落盘了')
        self.assertNotIn('per_page', self._stored(), '越权调用不该顺带写入普通字段')

    def test_admin_role_can_write(self):
        for role in (ROLE_OWNER, ROLE_ADMIN):
            with self.subTest(role=role), use_principal(_principal(role)):
                self.assertEqual(self.plugin.save_settings({'root_dir': '/srv/图片'}), {'success': True})
                self.assertEqual(self._stored()['root_dir'], '/srv/图片')

    def test_member_can_still_write_ordinary_fields(self):
        with use_principal(_principal(ROLE_MEMBER)):
            self.assertEqual(self.plugin.save_settings({'per_page': 80}), {'success': True})
        self.assertEqual(self._stored()['per_page'], 80)

    def test_mask_of_admin_secret_is_not_treated_as_change(self):
        self.plugin.update_setting('admin_token', 'ADMIN-REAL')
        with use_principal(_principal(ROLE_MEMBER)):
            result = self.plugin.save_settings({'admin_token': SECRET_MASK, 'per_page': 10})
        self.assertTrue(result['success'])
        self.assertEqual(self._stored()['admin_token'], 'ADMIN-REAL')
        self.assertEqual(self._stored()['per_page'], 10)

    def test_member_cannot_clear_admin_secret(self):
        self.plugin.update_setting('admin_token', 'ADMIN-REAL')
        with use_principal(_principal(ROLE_MEMBER)):
            result = self.plugin.save_settings({'admin_token': ''})
        self.assertFalse(result['success'])
        self.assertIn('admin_token', result['errors'])
        self.assertEqual(self._stored()['admin_token'], 'ADMIN-REAL')

    def test_multiple_field_errors_are_collected(self):
        with use_principal(_principal(ROLE_MEMBER)):
            result = self.plugin.save_settings({
                'root_dir': '/etc',
                'admin_token': 'ATTACKER',
                'per_page': 0,
            })
        self.assertFalse(result['success'])
        self.assertEqual(set(result['errors']), {'root_dir', 'admin_token', 'per_page'})
        for key in ('root_dir', 'admin_token', 'per_page'):
            self.assertIn(key, result['error'])
        self.assertEqual(self._stored(), {})

    def test_update_setting_keeps_admin_guard(self):
        self.plugin.update_setting('root_dir', '/srv/初始')
        with use_principal(_principal(ROLE_MEMBER)):
            self.assertFalse(self.plugin.update_setting('root_dir', '/etc'))
            self.assertTrue(self.plugin.update_setting('per_page', 30))
        self.assertEqual(self._stored()['root_dir'], '/srv/初始')
        self.assertEqual(self._stored()['per_page'], 30)


class SchemaCompatibilityTests(_SettingsTest):
    def test_schema_less_plugin_keeps_legacy_behaviour(self):
        plugin = self._new_plugin([], name='bare')
        self.assertEqual(plugin.save_settings({'anything': {'nested': 1}}), {'success': True})
        self.assertEqual(plugin._settings_store.get('bare')['anything'], {'nested': 1})

    def test_info_field_is_ignored_and_never_validated(self):
        self.assertEqual(self.plugin.save_settings({'info_row': 'x'}), {'success': True})
        self.assertNotIn('info_row', self._stored())

    def test_unknown_keys_are_ignored(self):
        self.assertEqual(self.plugin.save_settings({'unknown': 1}), {'success': True})
        self.assertNotIn('unknown', self._stored())

    def test_missing_bounds_mean_unlimited(self):
        self.assertEqual(self.plugin.save_settings({'max_only': -100, 'min_only': 100}),
                         {'success': True})
        self.assertEqual(self._stored()['max_only'], -100)
        self.assertEqual(self._stored()['min_only'], 100)


class PublicValidatorApiTests(unittest.TestCase):
    """数字校验函数是跨模块公开 API，不是 `_` 私有实现细节。

    image-viewer 的文件夹级设置保存复用同一套 number/range 校验；把函数改回私有会
    让插件再次 import 私有名字（跨模块私有耦合），因此这里同时锁公开名字可用与
    私有名字不存在。
    """

    def test_public_functions_are_importable_and_close_to_base_behaviour(self):
        from shell.backend.plugin_base import parse_setting_number, validate_number_setting

        # 公开 API 与 save_settings 用的是同一套行为（数字字符串归一、布尔不算数字）。
        self.assertEqual(parse_setting_number('42'), 42)
        self.assertEqual(parse_setting_number('1.5'), 1.5)
        self.assertIsNone(parse_setting_number(True))
        message, normalized = validate_number_setting(
            {'key': 'n', 'label': 'N', 'type': 'number', 'min': 0, 'max': 10}, 5)
        self.assertIsNone(message)
        self.assertEqual(normalized, 5)
        message, normalized = validate_number_setting(
            {'key': 'n', 'label': 'N', 'type': 'number', 'min': 0, 'max': 10}, 11)
        self.assertIsNotNone(message)
        self.assertIsNone(normalized)

        # 不允许保留私有别名：插件不该再 import `_parse_setting_number` 一类名字。
        self.assertFalse(hasattr(plugin_base_module, '_parse_setting_number'))
        self.assertFalse(hasattr(plugin_base_module, '_validate_number_setting'))


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
