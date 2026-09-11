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


def _write_plugin(root: Path, name: str, source: str, deps=None) -> Path:
    plugin_dir = root / name
    (plugin_dir / 'backend').mkdir(parents=True)
    manifest = {
        'name': name,
        'version': '1.0.0',
        'displayName': name,
        'dependencies': deps or [],
        'backend': {'entry': 'backend/main.py', 'class': 'Plugin'},
        'frontend': {'entry': 'frontend/index.html', 'route': f'/{name}'},
    }
    (plugin_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False), encoding='utf-8'
    )
    (plugin_dir / 'backend' / 'main.py').write_text(source, encoding='utf-8')
    return plugin_dir


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


if __name__ == '__main__':
    unittest.main()
