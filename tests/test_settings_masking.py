"""凭据类设置项的脱敏契约测试（`PluginBase.get_settings` / `SECRET_MASK`）。

背景（审计 §1.3）：`get_settings()` 通常经 `register_api()` 暴露成
`POST /api/<插件>__get_settings`，而插件 iframe 与壳同源。只要它返回明文，
"壳拒绝把设置文件当媒体资源返回"那层防护就被绕开 —— 攻击者不必读文件，
直接问 API 即可。pixiv-sync 的 refresh_token 是账号级长期凭据（可无限换取
access_token 且自动轮换），因此这条必须由基类统一处理，而不是靠每个插件小心。

三个容易出错的点，本文件逐个锁住：
  1. `setting()` 必须仍是**明文** —— 插件自己要用它去认证，脱敏它等于把插件弄坏；
  2. `save_settings()` 的变更检测必须用未脱敏的值 —— 否则凭据每次都被判成
     "已变更"，每次保存都触发 on_settings_changed（pixiv-sync 会重建客户端）；
  3. 掩码原样回传必须表示"不改动" —— 前端把 `get_settings()` 的值回填进输入框
     再整体提交，若把掩码当新值写入，凭据就被静默覆盖成一串星号。

文件路由侧的防护（403）由 tests/test_protected_paths.py 覆盖。

运行：
    python -m unittest tests.test_settings_masking -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.plugin_base import SECRET_MASK, PluginBase
from shell.backend.plugin_manager import PluginManager
from shell.backend.settings_store import SettingsStore

REAL_TOKEN = 'pixiv-refresh-token-REAL-VALUE'


class _Plugin(PluginBase):
    """带一个凭据项与两个普通项的最小插件。"""

    def __init__(self, manifest, config, schema):
        super().__init__(manifest, config)
        self.settings_schema = schema
        self.changed_calls: list = []

    def register_api(self):
        return {'get_settings': self.get_settings, 'save_settings': self.save_settings}

    def on_settings_changed(self, changed_keys):
        self.changed_calls.append(set(changed_keys))


def _schema():
    return [
        {'key': 'refresh_token', 'label': '令牌', 'type': 'text', 'secret': True},
        {'key': 'proxy', 'label': '代理', 'type': 'text'},
        {'key': 'per_page', 'label': '每页', 'type': 'number', 'default': 40},
    ]


class SecretMaskingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.plugin = _Plugin(
            {'name': 'demo'},
            {'directories': {'data_root': str(self.tmp / 'data')}},
            _schema(),
        )
        self.plugin._settings_store = SettingsStore(str(self.tmp / 'config' / 'plugins'))

    # ---------- 读取侧 ----------

    def test_secret_value_is_masked_in_get_settings(self):
        self.plugin.update_setting('refresh_token', REAL_TOKEN)
        settings = self.plugin.get_settings()
        self.assertEqual(settings['refresh_token'], SECRET_MASK)
        self.assertNotIn(REAL_TOKEN, str(settings))

    def test_secret_value_stays_plaintext_for_setting(self):
        """插件自己要用明文认证：setting() 绝不能跟着脱敏。"""
        self.plugin.update_setting('refresh_token', REAL_TOKEN)
        self.assertEqual(self.plugin.setting('refresh_token'), REAL_TOKEN)

    def test_unconfigured_secret_is_not_masked(self):
        """未配置就不打掩码 —— 前端据此显示"未配置"，不必额外加一条协议。

        取值仍是 schema 的 default（本例未声明 default，于是是 None）；掩码只替换
        **非空**值。断言写成 falsy 而不是 == ''：默认值语义是既有行为，脱敏不该
        顺手改掉它（前端 `s.refresh_token || ''` 对 None 与 '' 一视同仁）。
        """
        value = self.plugin.get_settings()['refresh_token']
        self.assertNotEqual(value, SECRET_MASK)
        self.assertFalse(value)

    def test_non_secret_keys_are_untouched(self):
        self.plugin.update_setting('proxy', 'http://127.0.0.1:7890')
        settings = self.plugin.get_settings()
        self.assertEqual(settings['proxy'], 'http://127.0.0.1:7890')
        self.assertEqual(settings['per_page'], 40)

    def test_raw_settings_is_unmasked(self):
        """内部访问器保持明文（save_settings 的变更检测依赖它）。"""
        self.plugin.update_setting('refresh_token', REAL_TOKEN)
        self.assertEqual(self.plugin._raw_settings()['refresh_token'], REAL_TOKEN)

    # ---------- 写入侧 ----------

    def test_mask_round_trip_keeps_the_real_secret(self):
        """掩码原样回传 = 不改动（前端整体提交表单时的主路径）。"""
        self.plugin.update_setting('refresh_token', REAL_TOKEN)
        submitted = dict(self.plugin.get_settings())
        submitted['proxy'] = 'http://new:7890'
        self.assertEqual(self.plugin.save_settings(submitted), {'success': True})

        stored = self.plugin._settings_store.get('demo')
        self.assertEqual(stored['refresh_token'], REAL_TOKEN, '凭据被掩码覆盖了')
        self.assertEqual(stored['proxy'], 'http://new:7890')

    def test_empty_string_clears_the_secret(self):
        self.plugin.update_setting('refresh_token', REAL_TOKEN)
        self.plugin.save_settings({'refresh_token': ''})
        self.assertEqual(self.plugin._settings_store.get('demo')['refresh_token'], '')

    def test_new_value_replaces_the_secret(self):
        self.plugin.update_setting('refresh_token', REAL_TOKEN)
        self.plugin.save_settings({'refresh_token': 'NEW-TOKEN'})
        self.assertEqual(self.plugin._settings_store.get('demo')['refresh_token'], 'NEW-TOKEN')

    def test_mask_round_trip_is_not_reported_as_changed(self):
        """回传掩码不得被判成"已变更"：否则每次保存都触发 on_settings_changed。"""
        self.plugin.update_setting('refresh_token', REAL_TOKEN)
        self.plugin.save_settings(dict(self.plugin.get_settings()))
        self.assertEqual(self.plugin.changed_calls, [],
                         f'凭据掩码被误判为变更: {self.plugin.changed_calls}')

    def test_real_change_is_still_reported(self):
        """脱敏不能把变更检测整体废掉。"""
        self.plugin.update_setting('refresh_token', REAL_TOKEN)
        self.plugin.save_settings({'refresh_token': 'NEW-TOKEN'})
        self.assertEqual(self.plugin.changed_calls, [{'refresh_token'}])

    def test_caller_dict_is_not_mutated(self):
        """save_settings 会 pop 掩码键：不得改动调用方传入的对象。"""
        self.plugin.update_setting('refresh_token', REAL_TOKEN)
        submitted = {'refresh_token': SECRET_MASK, 'proxy': 'x'}
        self.plugin.save_settings(submitted)
        self.assertEqual(submitted, {'refresh_token': SECRET_MASK, 'proxy': 'x'})

    def test_schema_less_plugin_still_works(self):
        """没有 schema 的插件（allowed 为空）走 else 分支，也要照常工作。"""
        plugin = _Plugin({'name': 'bare'}, {'directories': {'data_root': str(self.tmp)}}, [])
        plugin._settings_store = SettingsStore(str(self.tmp / 'config2' / 'plugins'))
        self.assertEqual(plugin.save_settings({'anything': 1}), {'success': True})
        self.assertEqual(plugin._settings_store.get('bare')['anything'], 1)


class PixivSyncMaskingTests(unittest.TestCase):
    """真实插件侧：pixiv-sync 的 `get_settings()` 不得再交出明文 refresh_token。

    这条用例直接构造真实的 PixivSyncPlugin（审计 §1.3 的对象），而不是拿一个
    自造的 schema 模拟 —— 它是这轮改动真正要保护的那个凭据。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _build_real_instance(self):
        import json

        from tools.check_plugins import _load_backend_class

        plugin_dir = PROJECT_ROOT / 'plugins' / 'pixiv-sync'
        manifest = json.loads((plugin_dir / 'manifest.json').read_text(encoding='utf-8'))
        cls = _load_backend_class(plugin_dir, 'backend/main.py', 'PixivSyncPlugin',
                                  lib_dirs=manifest.get('libs') or ['backend/libs'])

        data_root = self.tmp / 'data'
        data_root.mkdir(exist_ok=True)
        # pixiv-sync 需要一个已加载的宿主（image-viewer）来定位数据根与缓存目录
        host = type('_Host', (), {'get_data_root': lambda self: data_root})()
        manager = type('_Mgr', (), {
            'get_plugin_instance': lambda self, name: host if name == 'image-viewer' else None,
        })()
        cls._plugin_manager = manager

        instance = cls(manifest=manifest, config={'directories': {'data_root': str(data_root)}})
        instance._settings_store = SettingsStore(str(self.tmp / 'config' / 'plugins'))
        instance._plugin_manager = manager
        return instance

    def test_real_plugin_get_settings_masks_refresh_token(self):
        instance = self._build_real_instance()
        instance.update_setting('refresh_token', REAL_TOKEN)

        # 插件自身认证要用明文
        self.assertEqual(instance.setting('refresh_token'), REAL_TOKEN)

        # 对外 API 返回值必须脱敏（get_settings 注册在 register_api 里）
        self.assertIn('get_settings', instance.register_api())
        settings = instance.get_settings()
        self.assertEqual(settings['refresh_token'], SECRET_MASK)
        self.assertNotIn(REAL_TOKEN, str(settings))

    def test_real_plugin_mask_round_trip_keeps_token(self):
        """前端把 get_settings() 整体回填再提交时，真凭据不得被掩码覆盖。"""
        instance = self._build_real_instance()
        instance.update_setting('refresh_token', REAL_TOKEN)

        submitted = dict(instance.get_settings())
        self.assertEqual(submitted['refresh_token'], SECRET_MASK)
        self.assertEqual(instance.save_settings(submitted), {'success': True})

        stored = instance._settings_store.get('pixiv-sync')
        self.assertEqual(stored['refresh_token'], REAL_TOKEN, '真凭据被掩码覆盖了')


class _OverridingPlugin(_Plugin):
    """模拟 image-viewer / manga-library：自己实现 get_settings，且不调 super()。

    它们的 get_settings 会直读 `_settings_store`（image-viewer 还要处理 per-folder
    继承），基类的掩码于是整个不执行 —— 而该方法经 register_api() 直接变成
    `POST /api/<插件>__get_settings`。所以 Shell 侧必须在出口再掩一次。
    """

    def get_settings(self):
        return self._raw_settings()


class ShellSideMaskingTests(unittest.TestCase):
    """掩码不能只写在基类：插件覆写就有明文出口，Shell 侧出口必须再掩一次。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.plugin = _OverridingPlugin(
            {'name': 'demo'},
            {'directories': {'data_root': str(self.tmp / 'data')}},
            _schema(),
        )
        self.plugin._settings_store = SettingsStore(str(self.tmp / 'config' / 'plugins'))
        self.plugin.update_setting('refresh_token', REAL_TOKEN)

    def _manager(self):
        # 不走 __init__：它要扫描真实插件目录，而这里只需要实例表与清单表。
        manager = PluginManager.__new__(PluginManager)
        manager._instances = {self.plugin.name: self.plugin}
        manager._manifests = {'demo': {'displayName': 'Demo', 'icon': '📦'}}
        return manager

    def test_override_indeed_bypasses_the_base_class_mask(self):
        """前置条件：覆写后的返回值是明文 —— 这正是需要 Shell 兜住的场景。"""
        self.assertEqual(self.plugin.get_settings()['refresh_token'], REAL_TOKEN)

    def test_exposed_get_settings_is_masked(self):
        """`register_api()` 暴露的那份返回值必须已脱敏（Shell 侧包装）。"""
        exposed = PluginManager._exposed_method(
            self.plugin, 'get_settings', self.plugin.get_settings)
        self.assertEqual(exposed()['refresh_token'], SECRET_MASK)
        self.assertNotIn(REAL_TOKEN, str(exposed()))

    def test_exposed_wrapper_keeps_other_methods_untouched(self):
        """只有 get_settings 被包装，其它方法原样透传。"""
        calls = []

        def probe(value='x'):
            calls.append(value)
            return {'ok': value}

        exposed = PluginManager._exposed_method(self.plugin, 'list_images', probe)
        self.assertIs(exposed, probe)
        self.assertEqual(exposed('a'), {'ok': 'a'})
        self.assertEqual(calls, ['a'])

    def test_non_dict_return_passes_through(self):
        """插件返回自定义形状（非字典）时不得被改写。"""
        wrapped = PluginManager._exposed_method(
            self.plugin, 'get_settings', lambda: ['not-a-dict'])
        self.assertEqual(wrapped(), ['not-a-dict'])

    def test_settings_panel_values_are_masked(self):
        """集中设置面板（system_settings_list）是同一条泄露路径，同样要脱敏。"""
        panels = self._manager().get_settings_panels()
        self.assertEqual(len(panels), 1)
        values = panels[0]['values']
        self.assertEqual(values['refresh_token'], SECRET_MASK)
        self.assertNotIn(REAL_TOKEN, str(panels))


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
