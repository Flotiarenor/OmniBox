"""插件状态 API 的端到端测试（docs/code-review.md §4.2）。

链路：PluginManager 收集加载失败 → /api/system_get_plugin_status 暴露 →
壳内 /status 视图展示。这里把前两段串起来验证：用户（或前端）真的能问出
"某个插件为什么不见了"。

运行：
    python -m unittest tests.test_status_api -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import create_app
from shell.backend.paths import get_config_dir
from shell.backend.plugin_manager import PluginManager

_OK_PLUGIN = '''
from shell.backend.plugin_base import PluginBase


class Plugin(PluginBase):
    def register_api(self):
        return {"ping": lambda: "pong"}
'''

_BROKEN_PLUGIN = '''
from shell.backend.plugin_base import PluginBase


class Plugin(PluginBase):
    """on_load 抛错：模块本身没问题，但插件起不来。"""

    def register_api(self):
        return {"ghost": lambda: None}

    def on_load(self):
        raise RuntimeError("故意失败：缺少依赖")
'''


def _write_plugin(root: Path, name: str, source: str) -> Path:
    plugin_dir = root / name
    (plugin_dir / 'backend').mkdir(parents=True, exist_ok=True)
    manifest = {
        'name': name,
        'version': '1.0.0',
        'displayName': name,
        'dependencies': [],
        'backend': {'entry': 'backend/main.py', 'class': 'Plugin'},
        'frontend': {'entry': 'frontend/index.html', 'route': f'/{name}'},
    }
    (plugin_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    (plugin_dir / 'backend' / 'main.py').write_text(source, encoding='utf-8')
    return plugin_dir


class PluginStatusApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        plugins_dir = tmp / 'plugins'
        plugins_dir.mkdir()
        data_root = tmp / 'data'
        data_root.mkdir()
        _write_plugin(plugins_dir, 'alpha', _OK_PLUGIN)
        _write_plugin(plugins_dir, 'broken', _BROKEN_PLUGIN)

        config = {
            'server': {'host': '127.0.0.1', 'port': 18080},
            'directories': {'data_root': str(data_root)},
        }
        self.manager = PluginManager([str(plugins_dir)], config=config)
        self.manager.load_all()
        app = create_app(config, self.manager)
        self.client = app.test_client()
        self.token = get_or_create_token(get_config_dir())
        self.headers = {TOKEN_HEADER: self.token}

    def _call(self, method: str):
        resp = self.client.post(f'/api/{method}', headers=self.headers, json={})
        self.addCleanup(resp.close)
        return resp

    def test_status_api_reports_failures_and_loaded(self):
        resp = self._call('system_get_plugin_status')
        self.assertEqual(resp.status_code, 200)
        result = resp.get_json()['result']
        self.assertEqual(result['loaded'], ['alpha'])
        self.assertEqual([f['name'] for f in result['failures']], ['broken'])
        self.assertIn('故意失败', result['failures'][0]['reason'])

    def test_failed_plugin_is_absent_from_frontend_manifests(self):
        """失败插件不进清单 —— 这正是"用户觉得插件没了"的现象来源。"""
        resp = self._call('system_get_plugins')
        names = [m['name'] for m in resp.get_json()['result']]
        self.assertEqual(names, ['alpha'])

    def test_status_api_requires_token(self):
        resp = self.client.post('/api/system_get_plugin_status', json={})
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 401)

    def test_repaired_plugin_is_reported_as_loaded(self):
        """插件修好后：状态里不再有失败项，且出现在已加载列表。"""
        plugins_dir = Path(self._tmp.name) / 'plugins'
        self.manager.unload_all()
        _write_plugin(plugins_dir, 'broken', _OK_PLUGIN)  # 用可加载的实现覆盖

        manager = PluginManager([str(plugins_dir)], config=self.manager.config)
        manager.load_all()
        self.addCleanup(manager.unload_all)

        status = manager.get_plugin_status()
        self.assertEqual(status['failures'], [])
        self.assertEqual(status['loaded'], ['alpha', 'broken'])


if __name__ == '__main__':
    unittest.main()
