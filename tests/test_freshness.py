"""统一新鲜度基建（shell/backend/freshness.py）的回归测试。

固化的行为契约（每条都对应旧实现里真实出过的问题）：

- 冷启动同步 = 全部条目算新增，派生数据只对"变化项"重算（对比现状：media-player
  的深度扫描 `merged = {}` 会把整库标签重读一遍）；
- 稳态短路 = 每个目录只 scandir 一次、零 stat（`derive` 不被调用）；
- 去抖与单飞：连续调用只真扫一次，重入返回 busy（现状：image-cleaner 的
  "查-建-赋值"无锁，双击按钮就是两趟全盘 walk）；
- 幽灵清理：文件/目录消失后条目与派生缓存一起被清（现状：media-player 的增量
  扫描只增不减，"删除的媒体永久留在索引里"）；
- 原地覆盖写（同名、新内容、目录 mtime 与名字个数都不变）被动同步看不见，
  由**校验**兜住 —— 这是"同步便宜 / 校验彻底"分工的边界，必须显式固化；
- `content_version` 升级 = 全部条目重算（替代散落各插件的 INDEX_VERSION 常量）；
- 状态查询不建库（"看一眼占用"不该凭空造出数据库，同 ThumbCache.stats 的理由）。

运行：
    venv/bin/python -m unittest tests.test_freshness -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import create_app
from shell.backend.freshness import (
    FreshnessSpec,
    FreshnessStore,
    FsSource,
    all_engines,
    engine_for,
)
from shell.backend.plugin_manager import PluginManager


class _FakePlugin:
    """只装配引擎需要的两件事：缓存目录与 freshness_spec()。"""

    def __init__(self, cache_dir: Path, spec: dict | None):
        self.name = 'fake-plugin'
        self._cache_dir = cache_dir
        self._spec = spec
        self.derived = []        # 每次 derive 收到的条目键
        self.pruned = []         # 每次 prune 收到的键
        self.audits = []         # 每次 on_verified 收到的有效键集合
        self.rebuilt = []

    def get_cache_dir(self) -> Path:
        return self._cache_dir

    def freshness_spec(self) -> dict | None:
        return self._spec


def _spec(plugin: _FakePlugin, root: Path, **over) -> dict:
    spec = {
        'roots': lambda: [root],
        'content_version': 1,
        'derive': lambda items: plugin.derived.append([i['key'] for i in items]),
        'prune': lambda keys: plugin.pruned.extend(keys),
        'on_verified': lambda keys: plugin.audits.append(set(keys)),
        'unit': '项',
        'min_sync_interval': 0.0,
    }
    spec.update(over)
    return spec


class FreshnessEngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'media'
        self.root.mkdir()
        self.cache = Path(self._tmp.name) / '.cache' / 'fake-plugin'
        self.plugin = _FakePlugin(self.cache, None)
        self.plugin._spec = _spec(self.plugin, self.root)
        self.engine = engine_for(self.plugin)
        self.assertIsNotNone(self.engine)

    # ----- 工具 -----

    def _write(self, rel: str, data: bytes = b'x' * 8) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def _sync(self, scope: str = '') -> dict:
        return self.engine.sync(scope, force=True)

    def _derive_keys(self) -> list:
        return [k for batch in self.plugin.derived for k in batch]

    # ----- 契约 -----

    def test_cold_sync_indexes_everything_and_derives_once(self):
        self._write('a.jpg')
        self._write('sub/b.jpg')
        report = self._sync()

        self.assertEqual(report['action'], 'sync')
        self.assertEqual(report['added'], 2)
        self.assertEqual(report['changed'], 0)
        self.assertEqual(report['dirs'], 2)          # 根本身 + sub
        self.assertEqual(sorted(self._derive_keys()), ['a.jpg', 'sub/b.jpg'])
        self.assertEqual(report['skipped_dirs'], 0)   # 冷启动时无可短路

    def test_steady_state_short_circuits_without_stat(self):
        self._write('a.jpg')
        self._write('sub/b.jpg')
        self._sync()
        self.plugin.derived.clear()

        report = self._sync()

        self.assertEqual(report['added'], 0)
        self.assertEqual(report['changed'], 0)
        self.assertEqual(report['dirs'], 2)
        self.assertEqual(report['skipped_dirs'], 2, '目录级短路失效，稳态仍在逐条 stat')
        self.assertEqual(self.plugin.derived, [], '稳态不该重算派生数据')

    def test_sync_is_throttled_and_can_be_forced(self):
        self._write('a.jpg')
        self.engine.spec.min_sync_interval = 30.0
        self.engine.sync('')                        # 第一次真扫
        self.assertEqual(self.engine.sync('')['reason'], 'throttled')
        self.assertEqual(self.engine.sync('', force=True)['action'], 'sync')

    def test_new_file_is_caught_even_if_dir_mtime_is_restored(self):
        """目录 mtime 被外部工具还原时，靠"纳入索引的名字个数"仍然发现新增。"""
        self._write('a.jpg')
        self._sync()
        stamp = self.root.stat()
        self._write('b.jpg')
        os.utime(self.root, (stamp.st_atime, stamp.st_mtime))   # 还原目录 mtime
        self.plugin.derived.clear()

        report = self._sync()

        self.assertEqual(report['added'], 1)
        self.assertEqual(self._derive_keys(), ['b.jpg'])

    def test_removed_file_is_pruned(self):
        keep = self._write('a.jpg')
        gone = self._write('b.jpg')
        self._sync()
        gone.unlink()
        os.utime(self.root, (keep.stat().st_atime, time.time()))   # 确保目录 mtime 变化

        report = self._sync()

        self.assertEqual(report['removed'], 1)
        self.assertEqual(self.plugin.pruned, ['b.jpg'])

    def test_in_place_write_is_invisible_to_sync_but_found_by_verify(self):
        """边界固化①：原地写入不改目录 mtime 也不改名字个数 → 被动同步看不见。

        （同名、同长只是最容易构造的一种；追加写也一样，因为目录 mtime 不变。）
        """
        path = self._write('a.jpg', b'x' * 16)
        self._write('b.jpg')
        self._sync()
        stamp = path.stat()
        path.write_bytes(b'x' * 64)                 # 长度变了，但目录没变
        os.utime(path, (stamp.st_atime, stamp.st_mtime))
        self.plugin.derived.clear()

        self.assertEqual(self._sync()['changed'], 0, '同步不该声称发现了它')

        report = self.engine.verify()               # 校验必须逐项 stat 出来
        self.assertEqual(report['action'], 'verify')
        self.assertEqual(sorted(self._derive_keys()), ['a.jpg'])

    def test_stat_entries_opt_in_makes_sync_see_in_place_writes(self):
        """边界固化②：目录不大、且要求"替换立刻反映"的插件用 stat_entries 换准确度。"""
        self.plugin._spec = _spec(self.plugin, self.root, stat_entries=True)
        self.plugin._freshness_engine = None
        engine = engine_for(self.plugin)
        path = self._write('a.jpg', b'x' * 16)
        engine.sync('', force=True)
        self.plugin.derived.clear()

        stamp = path.stat()
        path.write_bytes(b'x' * 64)
        os.utime(path, (stamp.st_atime, stamp.st_mtime))
        report = engine.sync('', force=True)

        self.assertEqual(report['changed'], 1)
        self.assertEqual(self._derive_keys(), ['a.jpg'])

    def test_verify_drops_vanished_directory_and_audits_keys(self):
        self._write('a.jpg')
        self._write('sub/b.jpg')
        self._sync()
        self.plugin.pruned.clear()

        (self.root / 'sub' / 'b.jpg').unlink()
        (self.root / 'sub').rmdir()
        report = self.engine.verify()

        self.assertEqual(report['action'], 'verify')
        self.assertIn('sub/b.jpg', self.plugin.pruned)
        self.assertEqual(self.plugin.audits[-1], {'a.jpg'})

    def test_content_version_bump_recomputes_everything(self):
        self._write('a.jpg')
        self._write('sub/b.jpg')
        self._sync()
        self.plugin.derived.clear()

        self.engine.spec.content_version = 2
        report = self.engine.verify()

        self.assertTrue(report['force_all'])
        self.assertEqual(sorted(self._derive_keys()), ['a.jpg', 'sub/b.jpg'])
        self.assertEqual(self.engine.state()['content_version'], 2)
        self.assertFalse(self.engine.state()['version_stale'])

    def test_budget_marks_partial_and_leaves_no_half_state(self):
        self._write('d1/a.jpg')
        self._write('d2/b.jpg')
        self.engine.spec.max_dirs_per_sync = 1

        report = self._sync()

        self.assertTrue(report['partial'])
        self.assertEqual(report['action'], 'partial')
        # 落库的目录数不超过这一趟处理的目录数（不写"半份指纹"）
        state = self.engine.state()
        self.assertLessEqual(state['dirs'], 1)

    def test_scope_limits_walk_and_rejects_escape(self):
        self._write('sub/a.jpg')
        self._write('other/b.jpg')
        self._sync()
        self.plugin.derived.clear()

        # 只走 sub：other 下的文件不应出现在派生数据里
        report = self._sync('sub')
        self.assertEqual(report['scope'], 'sub')
        self.assertTrue(all(k.startswith('sub/') for k in self._derive_keys()), self._derive_keys())
        self.assertEqual(report['added'], 0)

        for bad in ('../outside', '/etc', 'a/../../b'):
            self.assertEqual(self.engine.sync(bad, force=True)['reason'], 'bad_scope', bad)

    def test_reentrant_sync_reports_busy(self):
        """单飞：同步过程中重入必须被挡（现状里双击按钮会并发两趟全盘 walk）。"""
        self._write('a.jpg')
        seen = []

        def derive(items):
            seen.append(self.engine.sync('', force=True))

        self.engine.spec.derive = derive
        self._sync()

        self.assertEqual(seen[0]['reason'], 'busy', seen)

    def test_multi_root_prefixes_map_to_virtual_keys(self):
        extra = Path(self._tmp.name) / 'extra'
        extra.mkdir()
        (extra / 'c.jpg').write_bytes(b'x' * 8)
        self.plugin._spec = _spec(self.plugin, self.root, roots=lambda: [self.root, extra],
                                  prefixes=('', '__extra'))
        self.plugin._freshness_engine = None
        engine = engine_for(self.plugin)

        report = engine.sync('', force=True)

        self.assertEqual(report['added'], 1)
        self.assertEqual(self._derive_keys(), ['__extra/c.jpg'])

    def test_rebuild_clears_fingerprints_and_reruns_verify(self):
        """逃生门：清插件派生缓存 + 清指纹库，然后重算一遍（不是"只清不建"）。"""
        self._write('a.jpg')
        self._sync()
        self.plugin.derived.clear()
        calls = []
        self.engine.spec.rebuild = lambda kind: (calls.append(kind), {'cleared': True})[1]

        out = self.engine.rebuild('derived')

        self.assertTrue(out['started'], out)
        self.assertEqual(calls, ['derived'])
        deadline = time.time() + 5
        while time.time() < deadline and self.engine.state()['busy']:
            time.sleep(0.02)
        self.assertFalse(self.engine.state()['busy'])
        self.assertIn('a.jpg', self._derive_keys(), '重建后必须重算，而不是停在空指纹库')

    def test_state_does_not_create_database(self):
        fresh = _FakePlugin(Path(self._tmp.name) / '.cache' / 'untouched',
                            None)
        fresh._spec = _spec(fresh, self.root)
        engine = engine_for(fresh)
        db = engine.store.db_path

        state = engine.state()

        self.assertFalse(db.exists(), '看一眼状态不该建库')
        self.assertFalse(state['busy'])
        self.assertEqual(state['entries'], 0)


class FreshnessSpecTests(unittest.TestCase):
    def test_normalize_rejects_bad_declarations(self):
        plugin = _FakePlugin(Path('.'), None)
        self.assertIsNone(FreshnessSpec.normalize(plugin, None))
        self.assertIsNone(FreshnessSpec.normalize(plugin, {}))
        self.assertIsNone(FreshnessSpec.normalize(plugin, {'roots': []}))
        with self.assertRaises(ValueError):
            FreshnessSpec.normalize(plugin, {'roots': ['.'], 'derive': 'not-callable'})
        with self.assertRaises(ValueError):
            FreshnessSpec.normalize(plugin, {'roots': ['.'], 'prefixes': ['a', 'b']})

    def test_engine_for_requires_a_spec(self):
        plugin = _FakePlugin(Path('.'), None)
        self.assertIsNone(engine_for(plugin))

    def test_engine_is_cached_on_the_plugin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'media'
            root.mkdir()
            plugin = _FakePlugin(Path(td) / '.cache', None)
            plugin._spec = _spec(plugin, root)
            first = engine_for(plugin)
            self.assertIs(first, engine_for(plugin))
            self.assertIn('fake-plugin', [e.name for e in all_engines()])

    def test_source_wants_and_skips(self):
        source = FsSource(roots=(Path('/tmp'),), include=('.jpg',))
        self.assertTrue(source.wants('a.JPG'))
        self.assertFalse(source.wants('a.png'))
        self.assertFalse(source.wants('.hidden.jpg'))
        self.assertTrue(source.skips('.cache'))
        self.assertTrue(source.skips('.git'))
        self.assertFalse(source.skips('normal'))


class FreshnessStoreTests(unittest.TestCase):
    def test_replace_and_drop_dir_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            store = FreshnessStore(Path(td) / 's.db')
            conn = store.connect()
            try:
                store.replace_dir(conn, 'd', (1.0, 2, 20, 9.0),
                                  [('d/a.jpg', 1.0, 8), ('d/b.jpg', 2.0, 12)])
                self.assertEqual(store.entry_keys(conn), {'d/a.jpg', 'd/b.jpg'})
                self.assertEqual(store.counts(conn), {'entries': 2, 'dirs': 1})

                # 整目录替换：旧条目不能残留
                store.replace_dir(conn, 'd', (2.0, 1, 8, 3.0), [('d/c.jpg', 3.0, 8)])
                self.assertEqual(store.entry_keys(conn), {'d/c.jpg'})

                removed = store.drop_dir(conn, 'd')
                self.assertEqual(removed, ['d/c.jpg'])
                self.assertEqual(store.counts(conn), {'entries': 0, 'dirs': 0})
            finally:
                conn.close()


class FreshnessApiTests(unittest.TestCase):
    """壳级端点走真实通道（PluginManager + /api）的端到端用例。

    为什么需要：其余用例只验引擎本体，`freshness_api_methods()` 这张表与
    `api_proxy` 的分发没有任何覆盖 —— 而这两处的错误形态都是"点了没反应"
    （方法名写错、参数顺序写反、表漏一项），单测引擎是发现不了的。
    """

    PLUGIN = '''
from pathlib import Path

from shell.backend.plugin_base import PluginBase


class Plugin(PluginBase):
    def register_api(self):
        return {}

    def get_data_root(self):
        return Path(self.config['directories']['data_root'])

    def freshness_spec(self):
        return {
            'roots': lambda: [self.get_data_root()],
            'include': ('.jpg',),
            'derive': lambda items: {'count': len(items)},
            'prune': lambda keys: len(keys),
            'content_version': 1,
            'unit': '张',
            'min_sync_interval': 0.0,
        }
'''

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        plugins_dir = tmp / 'plugins'
        plugins_dir.mkdir()
        self.data_root = tmp / 'data'
        self.data_root.mkdir()
        plugin_dir = plugins_dir / 'fresh-probe'
        (plugin_dir / 'backend').mkdir(parents=True)
        (plugin_dir / 'manifest.json').write_text(json.dumps({
            'name': 'fresh-probe',
            'version': '1.0.0',
            'displayName': 'fresh-probe',
            'dependencies': [],
            'backend': {'entry': 'backend/main.py', 'class': 'Plugin'},
            'frontend': {'entry': 'frontend/index.html', 'route': '/fresh-probe'},
        }, ensure_ascii=False), encoding='utf-8')
        (plugin_dir / 'backend' / 'main.py').write_text(self.PLUGIN, encoding='utf-8')

        config = {'server': {'host': '127.0.0.1', 'port': 18080},
                  'directories': {'data_root': str(self.data_root)}}
        self.manager = PluginManager([str(plugins_dir)], config=config)
        self.manager.load_all()
        self.assertIn('fresh-probe', self.manager.get_plugin_status()['loaded'])
        with mock.patch('shell.backend.file_server.get_config_dir',
                        return_value=tmp / '.config'):
            app = create_app(config, self.manager)
        self.client = app.test_client()
        self.headers = {TOKEN_HEADER: get_or_create_token(tmp / '.config')}

    def _call(self, method: str, *args):
        resp = self.client.post(f'/api/{method}', headers=self.headers,
                                json={'args': list(args)})
        self.addCleanup(resp.close)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()['result']

    def _verify_and_wait(self, timeout=10.0):
        """起一次全量校验并等到结束；顺带断言任务本身没有失败。

        任务失败（例如 worker 签名不对）时它照样会走到 done，只看 busy 会漏掉 ——
        这里显式校验 success 与 errors，正是为了让那类缺陷在测试里现形。
        """
        started = self._call('system_freshness_verify', 'fresh-probe')
        self.assertTrue(started.get('started'), started)
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._call('system_freshness_state', 'fresh-probe')
            if not state.get('busy'):
                task = state.get('task') or {}
                self.assertFalse(task.get('errors'), f'校验任务失败: {task.get("errors")}')
                self.assertTrue(task.get('success'), task)
                return state
            time.sleep(0.05)
        self.fail(f'校验超时未结束: {state}')
        return None

    def test_sync_state_verify_over_real_api(self):
        (self.data_root / 'a.jpg').write_bytes(b'x' * 8)
        (self.data_root / 'b.jpg').write_bytes(b'x' * 8)

        synced = self._call('system_freshness_sync', 'fresh-probe', '')
        self.assertEqual(synced['action'], 'sync')
        self.assertEqual(synced['added'], 2)

        state = self._call('system_freshness_state', 'fresh-probe')
        self.assertTrue(state['available'])
        self.assertEqual(state['entries'], 2)
        self.assertEqual(state['unit'], '张')
        self.assertFalse(state['version_stale'])

        after = self._verify_and_wait()
        self.assertFalse(after['busy'])
        self.assertEqual(after['entries'], 2)

        # 删掉一个文件：全量校验必须把幽灵条目清掉（现状里 media-player 的
        # 增量扫描永远做不到这件事）
        (self.data_root / 'b.jpg').unlink()
        after = self._verify_and_wait()
        self.assertEqual(after['entries'], 1)

        overview = self._call('system_freshness_overview')
        self.assertEqual([item['plugin'] for item in overview], ['fresh-probe'])

    def test_unknown_plugin_reports_unsupported(self):
        state = self._call('system_freshness_state', 'no-such-plugin')
        self.assertTrue(state['available'] is False)
        self.assertEqual(self._call('system_freshness_sync', 'no-such-plugin', '')['reason'],
                         'unsupported')
        self.assertFalse(self._call('system_freshness_verify', 'no-such-plugin')['started'])

    def test_scope_escape_is_rejected(self):
        (self.data_root / 'a.jpg').write_bytes(b'x' * 8)
        report = self._call('system_freshness_sync', 'fresh-probe', '../../etc')
        self.assertEqual(report['reason'], 'bad_scope')


if __name__ == '__main__':
    unittest.main()
