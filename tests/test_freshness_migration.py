"""指纹库格式迁移与多根命名空间的回归测试（`shell/backend/freshness.py`）。

固化三条契约（都是"升级后第一趟"才会踩到的路径，普通同步用例覆盖不到）：

- **键格式升级**（`meta.key_layout`）：旧库里声明了 `key_of` 的插件存的是裸插件键
  （没有根前缀）。升级后的第一趟不能把它们判成"文件消失"交给 `prune` —— 插件会
  据此删派生缓存甚至用户数据（document-reader 的阅读进度）。迁移只清指纹库、保留
  业务章（`content_version`），由这一趟全量重建。
- **根前缀唯一**：默认前缀按目录名生成，同名的两个根一定会撞（`__Music` /
  `__Music`）。目录键相撞后后者被 `seen` 静默丢弃 —— 整棵根从不进索引、也不报错。
  去重规则：第一个保留，后续同名根追加 `-<根序号>`；盘符根（目录名为空）用
  `__root<序号>`；声明的前缀重复同样处理并告警。
- **不可达根**：缺一块盘、删一个目录不是"读取错误"。不算 errors（否则整趟的 meta
  章永远盖不上、界面永久提示需要校验），不清理它的目录行，也不计入 `stale_dirs`。

运行：
    venv/Scripts/python.exe -m unittest tests.test_freshness_migration -v
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.freshness import (
    FreshnessEngine,
    FreshnessSpec,
    FsSource,
    engine_for,
)

_OLD_SCHEMA = '''
CREATE TABLE dirs (
    key TEXT PRIMARY KEY,
    dir_mtime REAL NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    size_sum INTEGER NOT NULL DEFAULT 0,
    max_mtime REAL NOT NULL DEFAULT 0
);
CREATE TABLE entries (
    key TEXT PRIMARY KEY,
    dir_key TEXT NOT NULL,
    mtime REAL NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
'''


class _FakePlugin:
    """只装配引擎需要的两件事：缓存目录与 `freshness_spec()`。"""

    def __init__(self, cache_dir: Path, spec: dict | None):
        self.name = 'migration-plugin'
        self._cache_dir = cache_dir
        self._spec = spec
        self._freshness_engine: Any = None
        self.derived = []
        self.pruned = []
        self.audits = []

    def get_cache_dir(self) -> Path:
        return self._cache_dir

    def freshness_spec(self) -> dict | None:
        return self._spec


def _spec(plugin: _FakePlugin, root: Path, **over) -> dict:
    spec = {
        'roots': lambda: [root],
        'include': ('.jpg',),
        'content_version': 1,
        'derive': lambda items: plugin.derived.extend(i['key'] for i in items),
        'prune': lambda keys: plugin.pruned.extend(keys),
        'on_verified': lambda keys: plugin.audits.append(set(keys)),
        'unit': '项',
        'min_sync_interval': 0.0,
    }
    spec.update(over)
    return spec


def _require_engine(plugin: _FakePlugin) -> FreshnessEngine:
    engine = engine_for(plugin)
    assert engine is not None, '插件必须能建出引擎'
    return engine


def _seed_old_database(db: Path, dirs: list, entries: list,
                       meta: dict | None = None) -> None:
    """按升级前的表结构（无 `dirs.version` / `dirs.prefix`）播种一个旧指纹库。"""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_OLD_SCHEMA)
        conn.executemany(
            'INSERT INTO dirs(key, dir_mtime, file_count, size_sum, max_mtime)'
            ' VALUES (?,?,?,?,?)', dirs)
        conn.executemany(
            'INSERT INTO entries(key, dir_key, mtime, size) VALUES (?,?,?,?)', entries)
        for key, value in (meta or {}).items():
            conn.execute('INSERT INTO meta(key, value) VALUES (?,?)', (key, str(value)))
        conn.commit()
    finally:
        conn.close()


class KeyLayoutMigrationTests(unittest.TestCase):
    """A1：键格式升级不清插件派生缓存、不把旧键交给 prune。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / 'doc'
        self.root.mkdir()
        self.extra = self.tmp / 'extra'
        self.extra.mkdir()
        (self.root / 'a.txt').write_bytes(b'x' * 8)
        (self.extra / 'b.txt').write_bytes(b'x' * 8)

    def _engine(self) -> tuple[FreshnessEngine, _FakePlugin]:
        plugin = _FakePlugin(self.tmp / '.cache' / 'doc', None)
        plugin._spec = _spec(
            plugin, self.root,
            roots=lambda: [self.root, self.extra],
            include=('.txt',),
            # 与 document-reader 同形：键是根内相对路径（只保证根内唯一）
            key_of=lambda abs_path: Path(abs_path).name)
        return _require_engine(plugin), plugin

    def test_old_bare_keys_are_rebuilt_instead_of_pruned(self):
        """旧库的裸插件键 → 迁移清库重扫：removed=0、prune 不收旧键、新键带前缀。"""
        engine, plugin = self._engine()
        b_path = self.extra / 'b.txt'
        b_stat = b_path.stat()
        extra_stat = self.extra.stat()
        _seed_old_database(
            engine.store.db_path,
            dirs=[('__extra', extra_stat.st_mtime, 1, 8, b_stat.st_mtime)],
            entries=[('b.txt', '__extra', b_stat.st_mtime, 8)],
            meta={'content_version': '1'})

        conn = engine.store.connect()
        try:
            self.assertEqual(engine.store.entry_keys(conn), {'b.txt'}, '播种的是旧格式裸键')
            self.assertIsNone(engine.store.meta_get(conn, 'key_layout'))

            engine._ensure_key_layout(conn)

            self.assertEqual(engine.store.meta_get(conn, 'key_layout'), '2')
            self.assertEqual(engine.store.entry_keys(conn), set(), '旧键整库作废')
            self.assertEqual(engine.store.dirs(conn), {}, '旧目录行整库作废')
            self.assertEqual(engine.store.meta_get(conn, 'content_version'), '1',
                             '业务章（content_version）不能被迁移删掉')
        finally:
            conn.close()

        report = engine.sync('', force=True)

        self.assertEqual(report['removed'], 0, '旧键不是"消失的文件"')
        self.assertEqual(plugin.pruned, [], 'prune 不能收到旧裸键')
        self.assertEqual(report['added'], 2, '两个根各一个文件，都是首次入库')
        self.assertEqual(sorted(plugin.derived), ['a.txt', 'b.txt'],
                         '交给 derive 的仍是插件键')
        conn = engine.store.connect()
        try:
            self.assertEqual(engine.store.entry_keys(conn),
                             {'a.txt', '__extra/b.txt'}, '新键 = 根前缀 + 插件键')
        finally:
            conn.close()

        # 第二趟稳态：不再有增删
        steady = engine.sync('', force=True)
        self.assertEqual((steady['added'], steady['changed'], steady['removed']), (0, 0, 0))

    def test_plugin_without_key_of_only_gains_the_marker(self):
        """未声明 `key_of` 时存储键一直是虚拟路径：迁移只补标记，已有指纹不丢。"""
        plugin = _FakePlugin(self.tmp / '.cache' / 'plain', None)
        plugin._spec = _spec(plugin, self.root)
        engine = _require_engine(plugin)

        a_path = self.root / 'a.txt'
        a_path.write_bytes(b'x' * 8) if not a_path.exists() else None
        sub = self.root / 'sub'
        sub.mkdir()
        file_path = sub / 'page.jpg'
        file_path.write_bytes(b'x' * 8)
        file_stat = file_path.stat()
        sub_stat = sub.stat()
        _seed_old_database(
            engine.store.db_path,
            dirs=[('sub', sub_stat.st_mtime, 1, 8, file_stat.st_mtime)],
            entries=[('sub/page.jpg', 'sub', file_stat.st_mtime, 8)],
            meta={'content_version': '1'})

        conn = engine.store.connect()
        try:
            engine._ensure_key_layout(conn)
            self.assertEqual(engine.store.meta_get(conn, 'key_layout'), '2')
            self.assertEqual(engine.store.entry_keys(conn), {'sub/page.jpg'},
                             '未声明 key_of 的插件不该被清库')
        finally:
            conn.close()

        report = engine.sync('', force=True)
        self.assertEqual(report['added'], 0, '虚拟路径格式没变：不判成新增')
        self.assertEqual(report['removed'], 0)
        self.assertEqual(plugin.pruned, [])
        conn = engine.store.connect()
        try:
            self.assertEqual(engine.store.entry_keys(conn), {'sub/page.jpg'},
                             '已有条目不能被迁移清掉')
        finally:
            conn.close()


class PrefixNamespaceTests(unittest.TestCase):
    """A2：根前缀唯一，同名根 / 盘符根 / 重复声明都不能让目录键相撞。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_same_name_roots_get_unique_default_prefixes(self):
        source = FsSource(roots=(self.tmp / 'main', Path('a/Music'), Path('b/Music')))
        self.assertEqual(source.prefixes, ('', '__Music', '__Music-2'))

    def test_drive_root_uses_indexed_placeholder(self):
        anchor = Path(Path.cwd().anchor)
        source = FsSource(roots=(self.tmp, anchor))
        self.assertEqual(source.prefixes, ('', '__root1'))

    def test_declared_duplicate_prefixes_are_disambiguated_and_warn(self):
        roots = (self.tmp / 'r0', self.tmp / 'r1', self.tmp / 'r2', self.tmp / 'r3')
        with self.assertLogs('shell.backend.freshness', level='WARNING') as logs:
            source = FsSource(roots=roots, prefixes=('', '__a', '__a', '__a-1'))
        self.assertEqual(source.prefixes, ('', '__a', '__a-2', '__a-1'))
        self.assertIn('__a', '\n'.join(logs.output))

    def test_all_same_name_roots_are_indexed(self):
        """回归：三根同名时，第三个根整棵目录曾被 `seen` 静默丢弃（不进索引、不报错）。"""
        main = self.tmp / 'main'
        real = self.tmp / 'a' / 'Music'
        backup = self.tmp / 'b' / 'Music'
        for path, name in ((main, 'm.jpg'), (real, 'x.jpg'), (backup, 'y.jpg')):
            path.mkdir(parents=True)
            (path / name).write_bytes(b'x' * 8)

        plugin = _FakePlugin(self.tmp / '.cache' / 'multi', None)
        plugin._spec = _spec(plugin, main, roots=lambda: [main, real, backup])
        engine = _require_engine(plugin)
        self.assertEqual(engine.spec.source.prefixes, ('', '__Music', '__Music-2'))

        report = engine.sync('', force=True)

        self.assertEqual(report['added'], 3, report)
        conn = engine.store.connect()
        try:
            self.assertEqual(engine.store.entry_keys(conn),
                             {'m.jpg', '__Music/x.jpg', '__Music-2/y.jpg'})
        finally:
            conn.close()
        plugin.derived.clear()
        steady = engine.sync('', force=True)
        self.assertEqual((steady['added'], steady['changed'], steady['removed']), (0, 0, 0))
        self.assertEqual(plugin.derived, [])


class UnreachableRootTests(unittest.TestCase):
    """A3：不可达根不算读取错误、不清理、不计入 stale_dirs。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / 'media'
        self.root.mkdir()
        (self.root / 'a.jpg').write_bytes(b'x' * 8)
        self.missing = self.tmp / 'gone'

    def _engine(self, version: int = 2) -> FreshnessEngine:
        plugin = _FakePlugin(self.tmp / '.cache' / 'unreachable', None)
        plugin._spec = _spec(
            plugin, self.root, roots=lambda: [self.root, self.missing],
            content_version=version)
        return _require_engine(plugin)

    def _seed_missing_root_dir(self, engine: FreshnessEngine, version: int) -> None:
        """模拟"上一趟还能访问"时留下的目录行（缺盘后它在库里）。"""
        conn = engine.store.connect()
        try:
            engine.store.replace_dir(conn, '__gone', (1.0, 1, 8, 1.0),
                                     [('__gone/c.jpg', 1.0, 8)],
                                     version=version, prefix='__gone')
        finally:
            conn.close()

    def test_missing_root_does_not_block_the_meta_chapter(self):
        engine = self._engine(version=1)
        self._seed_missing_root_dir(engine, version=1)
        engine.sync('', force=True)             # 可达根先按版本 1 铺底

        engine.spec.content_version = 2
        report = engine.sync('', force=True)

        self.assertEqual(report['unreachable_roots'], [str(self.missing)], report)
        self.assertEqual(report['errors'], [], '缺盘不是读取错误')
        self.assertEqual(report['vanished_dirs'], [], '不清理不可达根的目录行')
        self.assertEqual(report['removed'], 0)
        self.assertEqual(report['restamped'], 1, '只重算可达的那个目录')

        state = engine.state()
        self.assertEqual(state['unreachable_roots'], [str(self.missing)])
        self.assertEqual(state['content_version'], 2, 'meta 章能正常盖上')
        self.assertFalse(state['version_stale'])
        self.assertEqual(state['stale_dirs'], 0, '不可达根的目录不算"待重算"')

        conn = engine.store.connect()
        try:
            self.assertIn('__gone', engine.store.dirs(conn), '目录行必须保留')
            self.assertIn('__gone/c.jpg', engine.store.entry_keys(conn))
        finally:
            conn.close()

        # 再跑一趟也不报错、不清理
        again = engine.sync('', force=True)
        self.assertEqual(again['errors'], [])
        self.assertEqual(again['vanished_dirs'], [])

    def test_restored_root_is_picked_up_on_the_next_pass(self):
        engine = self._engine(version=2)
        self._seed_missing_root_dir(engine, version=1)
        engine.sync('', force=True)

        self.missing.mkdir()
        (self.missing / 'c.jpg').write_bytes(b'x' * 8)
        report = engine.sync('', force=True)

        self.assertEqual(report['unreachable_roots'], [])
        state = engine.state()
        self.assertEqual(state['unreachable_roots'], [])
        self.assertEqual(state['stale_dirs'], 0)
        self.assertFalse(state['version_stale'])
        conn = engine.store.connect()
        try:
            self.assertIn('__gone', engine.store.dirs(conn))
            self.assertIn('__gone/c.jpg', engine.store.entry_keys(conn))
        finally:
            conn.close()


class FreshnessSpecTrackEntriesTests(unittest.TestCase):
    """A7：`track_entries` 的声明归一（默认 True，显式 False 保留）。"""

    def test_track_entries_defaults_true_and_keeps_explicit_false(self):
        plugin = _FakePlugin(Path('.'), None)
        default = FreshnessSpec.normalize(plugin, {'roots': ['.']})
        assert default is not None
        self.assertTrue(default.track_entries)
        off = FreshnessSpec.normalize(plugin, {'roots': ['.'], 'track_entries': False})
        assert off is not None
        self.assertFalse(off.track_entries)
        on = FreshnessSpec.normalize(plugin, {'roots': ['.'], 'track_entries': True})
        assert on is not None
        self.assertTrue(on.track_entries)


if __name__ == '__main__':
    unittest.main()
