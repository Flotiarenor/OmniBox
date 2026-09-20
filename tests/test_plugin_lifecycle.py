"""插件生命周期测试：on_unload 必须真的被调用。

历史问题（docs/code-review.md §4.1-4）：on_unload 写在开发指南里、PluginBase
也留了钩子，但全仓**没有任何调用点** —— 插件线程、SQLite WAL、最后一次状态
落盘都不收尾。这里用两个真实加载的最小插件把「加载 → 卸载」链路固化下来：
正常插件会被卸载、抛错的插件不影响其它插件、重复卸载安全。

运行：
    python -m unittest tests.test_plugin_lifecycle -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from unittest import mock

from shell.backend.paths import get_plugins_config_dir
from shell.backend.plugin_manager import PluginManager

_OK_PLUGIN = '''
from pathlib import Path
from shell.backend.plugin_base import PluginBase

_DIR = Path(__file__).resolve().parent.parent


class Plugin(PluginBase):
    """最小可用插件：加载/卸载各留一个标记文件。"""

    def register_api(self):
        return {"ping": lambda: "pong"}

    def on_load(self):
        (_DIR / "loaded.marker").write_text("ok", encoding="utf-8")

    def on_unload(self):
        (_DIR / "unloaded.marker").write_text("ok", encoding="utf-8")
'''

_BAD_PLUGIN = '''
from pathlib import Path
from shell.backend.plugin_base import PluginBase

_DIR = Path(__file__).resolve().parent.parent


class Plugin(PluginBase):
    """on_unload 抛错的插件：用来验证卸载异常不会影响其它插件。"""

    def register_api(self):
        return {"boom": lambda: None}

    def on_load(self):
        (_DIR / "loaded.marker").write_text("ok", encoding="utf-8")

    def on_unload(self):
        raise RuntimeError("on_unload 故意抛错")
'''

_GHOST_PLUGIN = '''
from shell.backend.plugin_base import PluginBase


class Plugin(PluginBase):
    """on_load 抛错的插件：register_api 登记的方法绝不能留下来。"""

    def register_api(self):
        return {"ghost": lambda: "should-not-be-reachable"}

    def on_load(self):
        raise RuntimeError("on_load 故意抛错")
'''


def _write_plugin(root: Path, name: str, source: str, deps=None, extra=None) -> Path:
    plugin_dir = root / name
    (plugin_dir / 'backend').mkdir(parents=True, exist_ok=True)
    manifest = {
        'name': name,
        'version': '1.0.0',
        'displayName': name,
        'dependencies': deps or [],
        'backend': {'entry': 'backend/main.py', 'class': 'Plugin'},
        'frontend': {'entry': 'frontend/index.html', 'route': f'/{name}'},
    }
    manifest.update(extra or {})
    (plugin_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False), encoding='utf-8'
    )
    (plugin_dir / 'backend' / 'main.py').write_text(source, encoding='utf-8')
    return plugin_dir


# ===== 测试隔离：插件设置目录 =====
#
# `PluginManager.__init__` 在构造时解析"插件设置目录"（`_resolve_config_dir()` →
# 进程级 `<仓库>/.config/plugins`）。本文件里的用例把插件建在临时目录，却**没有**
# 覆盖这个设置目录，于是 `update_setting` / `save_settings` 会把 `alpha.json` 之类
# 写进开发机真实的 `.config/plugins/`：`git status` 看不见（`.config/` 被忽略），
# 但工作区里会持续留下用户数据；与真实插件同名时会直接覆盖开发者的设置。
# 审计项 P3-22。补丁在**构造之前**生效即可 —— 路径在构造时定下，之后的写入自然跟着走。
_MODULE_TMP: tempfile.TemporaryDirectory | None = None
_CONFIG_DIR_PATCH = None


def setUpModule():
    global _MODULE_TMP, _CONFIG_DIR_PATCH
    _MODULE_TMP = tempfile.TemporaryDirectory()
    _CONFIG_DIR_PATCH = mock.patch(
        'shell.backend.plugin_manager._resolve_config_dir',
        return_value=Path(_MODULE_TMP.name) / 'plugins')
    _CONFIG_DIR_PATCH.start()


def tearDownModule():
    global _MODULE_TMP, _CONFIG_DIR_PATCH
    if _CONFIG_DIR_PATCH is not None:
        _CONFIG_DIR_PATCH.stop()
        _CONFIG_DIR_PATCH = None
    if _MODULE_TMP is not None:
        _MODULE_TMP.cleanup()
        _MODULE_TMP = None


class PluginUnloadTests(unittest.TestCase):
    def _make_manager(self, td: str, *plugin_specs):
        root = Path(td) / 'plugins'
        root.mkdir(exist_ok=True)
        data_root = Path(td) / 'data'
        data_root.mkdir(exist_ok=True)
        for name, source, deps in plugin_specs:
            _write_plugin(root, name, source, deps)
        config = {'directories': {'data_root': str(data_root)}}
        manager = PluginManager([str(root)], config=config)
        return root, manager

    def test_on_unload_called_and_state_cleared(self):
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(td, ('alpha', _OK_PLUGIN, None))
            manager.load_all()
            self.assertTrue((root / 'alpha' / 'loaded.marker').exists(), 'on_load 没被调用')
            self.assertIn('alpha__ping', manager.get_api_methods())

            manager.unload_all()

            self.assertTrue(
                (root / 'alpha' / 'unloaded.marker').exists(),
                'on_unload 没有被调用（这正是历史缺陷）',
            )
            self.assertEqual(manager.get_api_methods(), {}, 'API 方法表没有清理')
            self.assertIsNone(manager.get_plugin_instance('alpha'))
            self.assertEqual(manager.get_frontend_manifests(), [])

    def test_unload_all_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(td, ('alpha', _OK_PLUGIN, None))
            manager.load_all()
            manager.unload_all()
            marker = root / 'alpha' / 'unloaded.marker'
            first = marker.stat().st_mtime_ns
            manager.unload_all()  # 不应抛错，也不应重复回调
            self.assertEqual(marker.stat().st_mtime_ns, first)

    def test_failing_plugin_does_not_block_others(self):
        """beta 依赖 alpha（加载 alpha→beta），卸载逆序：beta 抛错后 alpha 仍要卸载。"""
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(
                td,
                ('alpha', _OK_PLUGIN, None),
                ('beta', _BAD_PLUGIN, ['alpha']),
            )
            manager.load_all()
            self.assertIsNotNone(manager.get_plugin_instance('beta'))

            manager.unload_all()  # beta 的 on_unload 会抛错

            self.assertTrue(
                (root / 'alpha' / 'unloaded.marker').exists(),
                '一个插件卸载失败不该阻断其余插件的收尾',
            )
            self.assertEqual(manager.get_api_methods(), {})

    def test_new_api_methods_are_removed_with_plugin(self):
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(td, ('alpha', _OK_PLUGIN, None))
            manager.load_all()
            methods = manager.get_api_methods()
            self.assertTrue(all(k.startswith('alpha__') for k in methods))
            manager.unload_all()
            self.assertEqual(manager.get_api_methods(), {})

    def test_load_failure_does_not_register_partial_plugin(self):
        """入口文件缺失时不应留下任何实例/API/清单（幽灵 API 回归）。"""
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(td, ('alpha', _OK_PLUGIN, None))
            (root / 'alpha' / 'backend' / 'main.py').unlink()
            manager.load_all()
            self.assertIsNone(manager.get_plugin_instance('alpha'))
            self.assertEqual(manager.get_api_methods(), {})
            self.assertEqual(manager.get_frontend_manifests(), [])

    def test_on_load_failure_leaves_no_ghost_api(self):
        """on_load 抛错后，register_api 里的方法必须不可达。

        历史缺陷：方法先写进 _api_methods、再调 on_load、最后才写 _instances，
        于是 on_load 抛错会留下"幽灵 API" —— /api/ghost__ghost 能打到从未进入
        _instances 的半初始化实例。
        """
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(td, ('ghost', _GHOST_PLUGIN, None))
            manager.load_all()
            self.assertIsNone(manager.get_plugin_instance('ghost'))
            self.assertEqual(manager.get_api_methods(), {}, '半初始化实例的方法不应可调用')
            self.assertEqual(manager.get_frontend_manifests(), [])

    def test_failed_load_rolls_back_plugin_lib_paths(self):
        """加载失败要收回插件的 backend/libs，否则别的插件会误用它的私有库。"""
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(td, ('ghost', _GHOST_PLUGIN, None))
            libs = root / 'ghost' / 'backend' / 'libs'
            libs.mkdir(parents=True)
            lib_path = str(libs.resolve())

            manager.load_all()

            self.assertNotIn(lib_path, sys.path, '加载失败后必须收回插件的私有库路径')

    def test_load_failures_are_reported_for_status_page(self):
        """加载失败必须能被 /status 看到（以前只有一行日志，界面只知道插件没了）。"""
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(
                td,
                ('alpha', _OK_PLUGIN, None),
                ('ghost', _GHOST_PLUGIN, None),
            )
            manager.load_all()

            status = manager.get_plugin_status()
            self.assertEqual(status['loaded'], ['alpha'])
            self.assertEqual([f['name'] for f in status['failures']], ['ghost'])
            self.assertIn('on_load 故意抛错', status['failures'][0]['reason'])

    def test_successful_load_clears_previous_failure(self):
        """先失败后成功的插件不应继续挂在失败清单里（热修复后界面要恢复正常）。"""
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(td, ('alpha', _OK_PLUGIN, None))
            (root / 'alpha' / 'backend' / 'main.py').unlink()
            manager.load_all()
            self.assertEqual([f['name'] for f in manager.get_plugin_status()['failures']], ['alpha'])

            _write_plugin(root, 'alpha', _OK_PLUGIN)
            manager.load_all()

            status = manager.get_plugin_status()
            self.assertEqual(status['failures'], [])
            self.assertEqual(status['loaded'], ['alpha'])

    def test_successful_load_keeps_plugin_lib_paths(self):
        """加载成功必须保留 backend/libs（pixiv-sync 的 pixiv_mini 依赖这条）。"""
        with tempfile.TemporaryDirectory() as td:
            root, manager = self._make_manager(td, ('alpha', _OK_PLUGIN, None))
            libs = root / 'alpha' / 'backend' / 'libs'
            libs.mkdir(parents=True)
            lib_path = str(libs.resolve())
            self.addCleanup(lambda: sys.path.remove(lib_path) if lib_path in sys.path else None)

            manager.load_all()

            self.assertIn(lib_path, sys.path, '成功的插件必须保留私有库路径')


class PluginKeepAliveManifestTests(unittest.TestCase):
    """`manifest.keepAlive` 是"离开页面是否保活"的唯一开关，且默认必须是不保活。

    壳的契约是"不声明就卸载"（App.vue 按这个布尔量把插件分成 v-show 常驻组与按需
    挂载组），只有媒体播放、TTS 朗读这类显式申请的插件才常驻。默认值一旦被改成
    true，所有插件都会悄悄恢复常驻 —— 现象是插件切走不停表，且没有任何报错。
    """

    def test_default_false_and_explicit_true_passthrough(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'plugins'
            root.mkdir()
            _write_plugin(root, 'plain', _OK_PLUGIN)
            _write_plugin(root, 'player', _OK_PLUGIN, extra={'keepAlive': True})
            data_root = Path(td) / 'data'
            data_root.mkdir()
            manager = PluginManager([str(root)], config={'directories': {'data_root': str(data_root)}})

            manager.load_all()

            flags = {m['name']: m['keepAlive'] for m in manager.get_frontend_manifests()}
            self.assertEqual(flags, {'plain': False, 'player': True})


class PluginSettingsPersistenceTests(unittest.TestCase):
    """保存设置面板不得抹掉插件写在同一个文件里的运行期状态。

    历史缺陷：PluginBase.save_settings() 用 SettingsStore.set()（整文件覆盖）写入
    过滤后的 schema 键，而插件同时用 update_setting() 把运行期状态
    （pixiv-sync 的 refresh_token、image-viewer 的 folders、media-player 的
    media_set_config）写进**同一个 JSON**。用户在设置面板点一次保存，这些键就被
    静默删除（不报错、不提示），下次启动需要重新登录/重新配置。
    """

    _PLUGIN = '''
from pathlib import Path
from shell.backend.plugin_base import PluginBase

_DIR = Path(__file__).resolve().parent.parent


class Plugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "根目录", "type": "folder", "default": ""},
        {"key": "per_page", "label": "每页数量", "type": "number", "default": 40},
    ]

    def register_api(self):
        return {"ping": lambda: "pong"}

    def on_load(self):
        (_DIR / "loaded.marker").write_text("ok", encoding="utf-8")
'''

    def test_settings_stay_inside_the_isolated_config_dir(self):
        """审计项 P3-22：跑单测不得往真实 `<config>/plugins/` 写文件。

        `git status` 看不到这种写入（`.config/` 被忽略），所以它不会被"提交前检查"
        拦住，只会在开发机的工作区里长期堆积用户数据；用例与真实插件同名时还会直接
        覆盖开发者的设置文件。断言写成"写入落在模块级临时目录"，而不是"真实目录里
        没有该文件" —— 后者会被历史遗留文件弄成假失败。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'plugins'
            data_root = Path(td) / 'data'
            data_root.mkdir(exist_ok=True)
            _write_plugin(root, 'alpha', self._PLUGIN)
            manager = PluginManager([str(root)], config={'directories': {'data_root': str(data_root)}})
            manager.load_all()

            instance = manager.get_plugin_instance('alpha')
            self.assertTrue(instance.update_setting('probe_key', 'probe-value'))

            self.assertNotEqual(Path(manager._config_dir), get_plugins_config_dir(),
                                '设置目录仍是开发机真实的 <config>/plugins')
            isolated = Path(manager._config_dir) / 'alpha.json'
            self.assertTrue(isolated.is_file(), f'设置没有落在隔离目录里: {isolated}')
            self.assertIn('probe-value', isolated.read_text(encoding='utf-8'))

    def test_settings_file_is_protected_without_secret_flag(self):
        """审计项 P3-2：插件设置文件**默认**受保护，不再要求每个插件记得声明 secret。

        凭据可以经 `update_setting()` 写进这个文件（pixiv-sync 的 refresh_token 就是
        这么落的），而"没声明 secret"不该等于"可被 /file 返回"。`secret` 标记仍然管
        另一件事：`get_settings()` 的掩码。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'plugins'
            data_root = Path(td) / 'data'
            data_root.mkdir(exist_ok=True)
            _write_plugin(root, 'plain-plugin', self._PLUGIN)
            manager = PluginManager([str(root)], config={'directories': {'data_root': str(data_root)}})
            manager.load_all()

            protected = [Path(p) for p in manager.get_protected_paths()]
            settings = Path(manager._config_dir) / 'plain-plugin.json'
            self.assertIn(settings, protected,
                          f'未申报 secret 的插件设置文件不在受保护清单里: {protected}')

    def test_save_settings_keeps_runtime_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'plugins'
            data_root = Path(td) / 'data'
            data_root.mkdir(exist_ok=True)
            _write_plugin(root, 'alpha', self._PLUGIN)
            manager = PluginManager([str(root)], config={'directories': {'data_root': str(data_root)}})
            manager.load_all()

            instance = manager.get_plugin_instance('alpha')
            self.assertIsNotNone(instance)

            # 模拟插件运行期落状态（不在 settings_schema 里的键）
            self.assertTrue(instance.update_setting('refresh_token', 'SECRET-TOKEN'))
            self.assertTrue(instance.update_setting('folders', ['a', 'b']))
            # 以及一个 schema 内的旧值
            instance.update_setting('per_page', 20)

            result = instance.save_settings({'root_dir': '/media', 'per_page': 60})
            self.assertEqual(result, {'success': True})

            stored = instance._settings_store.get('alpha')
            self.assertEqual(stored['root_dir'], '/media', 'schema 键必须写入')
            self.assertEqual(stored['per_page'], 60, 'schema 键必须更新')
            self.assertEqual(stored.get('refresh_token'), 'SECRET-TOKEN',
                             '运行期状态被保存设置抹掉了（需要重新登录）')
            self.assertEqual(stored.get('folders'), ['a', 'b'],
                             '运行期状态被保存设置抹掉了')


if __name__ == '__main__':
    unittest.main()
