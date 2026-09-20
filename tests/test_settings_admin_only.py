"""能力边界类设置项（`"admin_only": True`）的契约用例。

背景（审计项 P1-1 / P1-4 / P1-7）：`/file`、`/files`、`/thumbs` 的允许范围**就是**
插件自己返回的根（`get_file_roots()` / `thumb_dir`），而根由设置项决定。于是
"任何持令牌者都能改根"等价于"任何持令牌者都能读、删、移本机任意文件"：越权点不在
文件路由，而在设置入口。同理，"进程会带着凭据去请求的地址"（朗读端点）被改写就等于
把凭据交给任意主机，并让本进程成为可读回内网响应的 SSRF 跳板。

本文件锁住三件事：

  1. 声明了 `admin_only` 的键，**非管理员主体改不动**（`save_settings` 与
     `update_setting` 两条写入路径都要判，后者是运行期回写，绕过它就等于绕过面板）；
  2. 未声明的键行为不变（普通主体的每页数量、字号一类偏好不能被一起限权）；
  3. 真实插件的"根类/端点类"设置项确实声明了该标记 —— 标记被删掉就是一次 P1 回归。

"没有主体"按放行处理：HTTP 路径下主体必然存在（壳在 `before_request` 里鉴权后注入），
没有主体的调用方是进程内的可信代码（用例、CLI、插件自身的运行期回写）。

运行：
    python -m unittest tests.test_settings_admin_only -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.plugin_base import SECRET_MASK, PluginBase, admin_only_keys
from shell.backend.principal import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    PrincipalContext,
    use_principal,
)
from shell.backend.settings_store import SettingsStore


class _StubPlugin(PluginBase):
    """形状最小的宿主：只声明 schema，不实现任何业务方法。"""

    settings_schema: ClassVar[List[Dict]] = [
        {'key': 'root_dir', 'label': '根目录', 'type': 'text', 'admin_only': True},
        {'key': 'endpoint', 'label': '端点', 'type': 'text', 'admin_only': True},
        {'key': 'api_key', 'label': 'Key', 'type': 'text',
         'secret': True, 'admin_only': True},
        {'key': 'per_page', 'label': '每页数量', 'type': 'number', 'default': 40},
    ]

    def register_api(self):   # pragma: no cover - 用例不经过 API 注册
        return {}


def _principal(role: str, name: str = '测试主体') -> PrincipalContext:
    return PrincipalContext(id=name, name=name, role=role, source='enrolled')


class AdminOnlySettingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = SettingsStore(str(Path(self._tmp.name) / 'plugins'))
        self.plugin = _StubPlugin(manifest={'name': 'stub'},
                                 config={'directories': {'data_root': self._tmp.name}})
        self.plugin._settings_store = self.store

    def _stored(self) -> Dict:
        return self.store.get('stub')

    # ---------- save_settings ----------

    def test_member_cannot_change_admin_only_key(self):
        with use_principal(_principal(ROLE_MEMBER)):
            result = self.plugin.save_settings({'root_dir': '/etc'})
        self.assertFalse(result.get('success'))
        self.assertIn('管理员', result.get('error', ''))
        self.assertNotIn('root_dir', self._stored(), '越权写入仍然落盘了')

    def test_admin_can_change_admin_only_key(self):
        for role in (ROLE_OWNER, ROLE_ADMIN):
            with self.subTest(role=role), use_principal(_principal(role)):
                self.assertTrue(self.plugin.save_settings({'root_dir': '/srv/图片'})['success'])
                self.assertEqual(self._stored()['root_dir'], '/srv/图片')

    def test_member_can_still_change_ordinary_key(self):
        """普通偏好不能被一起限权：那会让非管理员连每页数量都改不了。"""
        with use_principal(_principal(ROLE_MEMBER)):
            self.assertTrue(self.plugin.save_settings({'per_page': 80})['success'])
        self.assertEqual(self._stored()['per_page'], 80)

    def test_process_internal_call_without_principal_is_allowed(self):
        """无主体 = 进程内可信调用（用例 / CLI / 插件自身），按放行处理。"""
        self.assertTrue(self.plugin.save_settings({'root_dir': '/srv/漫画'})['success'])
        self.assertEqual(self._stored()['root_dir'], '/srv/漫画')

    def test_member_submitting_secret_mask_is_not_denied(self):
        """掩码原样回传 = 不改动：不该因为它是 admin_only 键而被拒。"""
        self.plugin.save_settings({'api_key': 'REAL-KEY'})          # 无主体，先写入真值
        with use_principal(_principal(ROLE_MEMBER)):
            result = self.plugin.save_settings({'api_key': SECRET_MASK, 'per_page': 10})
        self.assertTrue(result['success'])
        self.assertEqual(self._stored()['api_key'], 'REAL-KEY')
        self.assertEqual(self._stored()['per_page'], 10)

    def test_member_submitting_real_value_for_secret_admin_key_is_denied(self):
        with use_principal(_principal(ROLE_MEMBER)):
            result = self.plugin.save_settings({'api_key': 'ATTACKER'})
        self.assertFalse(result['success'])
        self.assertNotIn('api_key', self._stored())

    # ---------- update_setting（运行期回写）----------

    def test_update_setting_is_guarded_too(self):
        """绕过面板的运行期回写会落进同一个 JSON、重启后生效，因此同判。"""
        self.plugin.update_setting('root_dir', '/srv/初始')
        with use_principal(_principal(ROLE_MEMBER)):
            self.assertFalse(self.plugin.update_setting('root_dir', '/etc'))
            self.assertTrue(self.plugin.update_setting('per_page', 30))
        self.assertEqual(self._stored()['root_dir'], '/srv/初始')
        self.assertEqual(self._stored()['per_page'], 30)

    def test_admin_only_keys_helper_reads_schema(self):
        self.assertEqual(admin_only_keys(self.plugin.settings_schema),
                         {'root_dir', 'endpoint', 'api_key'})
        self.assertEqual(admin_only_keys(None), set())
        self.assertEqual(admin_only_keys([{'key': 'x'}, 'not-a-dict', {'admin_only': True}]),
                         set())


class RealPluginCapabilityKeyTests(unittest.TestCase):
    """真实插件的根类/端点类设置项必须声明 admin_only（删掉标记 = P1 回归）。

    期望表按插件与键写死：它记录的是"哪个键决定插件能碰本机哪些文件 / 能把凭据发给
    哪个地址"，是审计结论的机器可复核形式。
    """

    EXPECTED: ClassVar[Dict[str, set]] = {
        'image-viewer': {'root_dir', 'extra_roots'},
        'media-player': {'media_roots'},
        'document-reader': {'root_dir', 'tts_base_url', 'tts_api_key'},
        'manga-library': {'root_dir'},
        'pixiv-sync': {'download_dir'},
        'group-mesh': {'download_dir'},
    }

    @staticmethod
    def _load_schema(plugin_name: str) -> List[Dict]:
        plugin_dir = PROJECT_ROOT / 'plugins' / plugin_name
        manifest = json.loads((plugin_dir / 'manifest.json').read_text(encoding='utf-8'))
        backend = manifest.get('backend') or {}
        entry = backend.get('entry', 'backend/main.py')
        class_name = backend.get('class', 'Plugin')
        # 与 PluginManager 一致：先把插件自带的库目录放进 sys.path
        inserted = []
        for rel in (manifest.get('libs') or ['backend/libs']):
            lib_dir = plugin_dir / rel
            if lib_dir.is_dir():
                sys.path.insert(0, str(lib_dir))
                inserted.append(str(lib_dir))
        try:
            spec = importlib.util.spec_from_file_location(
                f'test_admin_only_{plugin_name.replace("-", "_")}',
                str(plugin_dir / entry))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return list(getattr(getattr(module, class_name), 'settings_schema', []))
        finally:
            # 用例之间必须还原：插件私有库留在 sys.path 上会让别的插件误用
            for path in inserted:
                try:
                    sys.path.remove(path)
                except ValueError:
                    pass

    def test_capability_keys_are_marked(self):
        for plugin_name, expected in sorted(self.EXPECTED.items()):
            with self.subTest(plugin=plugin_name):
                schema = self._load_schema(plugin_name)
                marked = admin_only_keys(schema)
                missing = expected - marked
                self.assertFalse(
                    missing,
                    f'{plugin_name} 的能力边界设置项 {sorted(missing)} 未声明 '
                    f'"admin_only": True —— 任何持令牌者都能改它，等于改写本机文件访问范围')


class RootRewriteChainTests(unittest.TestCase):
    """端到端链路：`save_folder_settings` → 基类限权 → `get_file_roots()` 不变。

    单看基类的返回值不够：image-viewer 覆写了 `save_settings`（`save_folder_settings`），
    而限权必须对覆写后的入口同样生效 —— 这正是"把判定写在插件里"与"写在基类里"的
    差别，也是审计里 `save_settings` 被当成任意文件读/删入口的原因。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.media = base / '媒体根'
        self.media.mkdir()
        self.outside = base / '授权范围外'
        self.outside.mkdir()
        (self.outside / 'secret.txt').write_text('TOP-SECRET', encoding='utf-8')

    def _plugin(self):
        plugin_dir = PROJECT_ROOT / 'plugins' / 'image-viewer' / 'backend' / 'main.py'
        spec = importlib.util.spec_from_file_location('test_admin_only_image_viewer',
                                                      str(plugin_dir))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin = module.ImageViewerPlugin(
            manifest={'name': 'image-viewer'},
            config={'directories': {'data_root': str(self.media)}})
        plugin._settings_store = SettingsStore(str(Path(self._tmp.name) / '.config' / 'plugins'))
        plugin.save_settings({'root_dir': str(self.media)})     # 无主体：初始化根
        return plugin

    def _roots(self, plugin):
        return [Path(root).resolve() for root in plugin.get_file_roots()]

    def test_member_cannot_widen_file_roots(self):
        plugin = self._plugin()
        self.assertIn(self.media.resolve(), self._roots(plugin))
        self.assertNotIn(self.outside.resolve(), self._roots(plugin))

        with use_principal(_principal(ROLE_MEMBER)):
            result = plugin.save_folder_settings('', {'root_dir': str(self.outside)})

        self.assertFalse(result.get('success'))
        self.assertIn('管理员', result.get('error', ''))
        self.assertNotIn(self.outside.resolve(), self._roots(plugin),
                         '非管理员改根后 /file 的允许范围被放大了')

    def test_admin_can_widen_file_roots(self):
        """限权不能把功能本身弄坏：管理员改根后必须立即生效。"""
        plugin = self._plugin()
        with use_principal(_principal(ROLE_OWNER)):
            result = plugin.save_folder_settings('', {'root_dir': str(self.outside)})
        self.assertTrue(result.get('success'), result)
        self.assertIn(self.outside.resolve(), self._roots(plugin))


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
