"""manga-library 后端组件化之后的**门面契约**回归。

为什么需要：组件化把每个 API 键的实现挪进了组件，用例也随之改成直接调组件 ——
于是「`register_api` 里键名与绑定对象」这一段就没有任何用例覆盖了。前端与壳按名字
取成员（`manga_*` / `download_*` 经 `Bridge.call` 映射，钩子由壳直接取），改名或
漏绑都不会报错，只会静默 404 / 静默退化成默认实现。这里把四件事钉住：

1. `register_api` 的键与**顺序**与迁移前逐字一致，且每个键绑到预期组件；
2. 壳按名调用的钩子仍在入口类上；
3. 设置项 schema 与迁移前逐字一致（设置弹窗与 `save_settings` 的白名单都读它）；
4. `get_settings` 仍返回**生效值**（根目录可能来自默认数据根，不等于存储里的原值）。

入口覆写 `get_settings` 在这里是有意的、迁移前就有：它对外报的是"当前生效的根"，
而 `PluginBase.get_settings` 报存储原值 —— 换掉会让设置弹窗在未配置根时显示空白。

运行：
    python -m unittest tests.test_manga_library_api_table -v
"""

import json
import logging
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'manga-library'
PLUGIN_BACKEND = PLUGIN_DIR / 'backend'

from shell.backend.freshness import engine_for
from shell.backend.plugin_base import PluginBase
from shell.backend.settings_store import SettingsStore
from tools.check_plugins import _load_backend_class

#: 迁移前的键与顺序（前端按名字调用，改动即静默 404）。
EXPECTED_KEYS = [
    'manga_list',
    'manga_search',
    'manga_get_state',
    'manga_toggle_favorite',
    'manga_update_recent',
    'manga_get_detail',
    'manga_get_pages',
    'download_submit',
    'download_list',
    'download_pause',
    'download_resume',
    'download_retry',
    'download_delete',
    'download_start_all',
    'download_pause_all',
    'download_clear_completed',
    'download_get_album_info',
    'get_settings',
    'save_settings',
]

#: 键 → 绑定的组件属性名（绑错组件时"能调用"但行为不对，只有这里能发现）。
EXPECTED_COMPONENTS = {
    'manga_list': 'library',
    'manga_search': 'library',
    'manga_get_state': 'library',
    'manga_toggle_favorite': 'library',
    'manga_update_recent': 'library',
    'manga_get_detail': 'library',
    'manga_get_pages': 'library',
    'download_submit': 'downloads',
    'download_list': 'downloads',
    'download_pause': 'downloads',
    'download_resume': 'downloads',
    'download_retry': 'downloads',
    'download_delete': 'downloads',
    'download_start_all': 'downloads',
    'download_pause_all': 'downloads',
    'download_clear_completed': 'downloads',
    'download_get_album_info': 'downloads',
}

#: 壳按名字从插件实例上取的钩子（不经 `register_api`）。
SHELL_HOOKS = [
    'settings_schema',
    'freshness_spec',
    'get_data_root',
    'get_settings',
    'on_settings_changed',
    'register_api',
]

#: 设置项字段的完整形状：设置弹窗按它渲染，`save_settings` 按它校验白名单。
EXPECTED_SCHEMA_KEYS = ['root_dir', 'recent_count']


class MangaLibraryApiTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'manga'
        self.root.mkdir(parents=True, exist_ok=True)
        cls = _load_backend_class(PLUGIN_DIR, 'backend/main.py', 'MangaLibraryPlugin')
        cls._resolved_config = {'root_dir': str(self.root)}
        self.addCleanup(setattr, cls, '_resolved_config', {})
        self.plugin = cls({'name': 'manga-library'},
                          {'directories': {'data_root': str(self.root)}})
        self.plugin._settings_store = SettingsStore(
            str(Path(self._tmp.name) / 'settings'))
        self.addCleanup(self.plugin.on_unload)

    def test_api_keys_and_order_are_unchanged(self):
        self.assertEqual(list(self.plugin.register_api()), EXPECTED_KEYS)

    def test_every_api_key_binds_expected_component(self):
        api = self.plugin.register_api()
        for key, attribute in EXPECTED_COMPONENTS.items():
            with self.subTest(key=key):
                self.assertIs(getattr(api[key], '__self__', None),
                              getattr(self.plugin, attribute),
                              f'{key} 应绑定到组件的 {attribute}')

    def test_settings_bindings(self):
        api = self.plugin.register_api()
        # save_settings 仍走基类：校验、脱敏与 on_settings_changed 的通知都在那里。
        self.assertEqual(api['save_settings'].__func__, PluginBase.save_settings)
        self.assertEqual(api['get_settings'].__func__,
                         type(self.plugin).get_settings,
                         'get_settings 是入口自己的实现，用来报"当前生效的根"')
        self.assertIs(api['get_settings'].__self__, self.plugin)

    def test_shell_hooks_live_on_the_entry_class(self):
        for name in SHELL_HOOKS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(type(self.plugin), name),
                                f'壳按名调用的 {name} 必须能从插件实例上取到')

    def test_entry_has_no_mixin_and_is_not_a_mixin(self):
        bases = [base.__name__ for base in type(self.plugin).__mro__[1:]]
        self.assertEqual([name for name in bases if name.endswith('Mixin')], [],
                         f'入口不得继承任何 Mixin: {bases}')

    def test_settings_schema_is_unchanged(self):
        schema = self.plugin.settings_schema
        self.assertEqual([item['key'] for item in schema], EXPECTED_SCHEMA_KEYS)
        root_field = schema[0]
        self.assertEqual(root_field['type'], 'directory')
        self.assertTrue(root_field.get('admin_only'),
                        'root_dir 决定 /file 的允许根，改它必须限管理员')
        recent_field = schema[1]
        self.assertEqual((recent_field['min'], recent_field['max']), (1, 50))
        self.assertEqual(recent_field['default'], 10)

    def test_get_settings_reports_effective_values(self):
        settings = self.plugin.get_settings()
        self.assertEqual(settings['root_dir'], str(self.root.resolve()))
        self.assertEqual(settings['recent_count'], 10)

    def test_recent_count_is_clamped_on_settings_change(self):
        library = self.plugin.library
        saved = self.plugin.save_settings({'recent_count': 7})
        self.assertTrue(saved.get('success'), saved)
        self.assertEqual(library.recent_count, 7)
        # 直接改存储里成越界值：on_settings_changed 仍把它夹回范围内
        self.plugin.update_setting('recent_count', 500)
        self.plugin.on_settings_changed({'recent_count'})
        self.assertEqual(library.recent_count, 50)

    def test_get_state_recent_carries_reading_page(self):
        """最近阅读带上次读到的页码；收藏与 `manga_list` 同形，不带该字段。"""
        folder = self.root / 'A'
        folder.mkdir()
        (folder / '001.jpg').write_bytes(b'\x89PNG\r\n\x1a\n')
        library = self.plugin.library
        library.update_recent('A', 3)
        library.toggle_favorite('A')

        state = library.get_state()

        self.assertEqual([m['folder_name'] for m in state['recent']], ['A'])
        self.assertEqual(state['recent'][0]['page'], 3)
        self.assertEqual([m['folder_name'] for m in state['favorites']], ['A'])
        self.assertNotIn('page', state['favorites'][0])

    def test_get_state_projection_does_not_share_shelf_objects(self):
        """投影里的 page 不写回书架缓存与状态文件。"""
        folder = self.root / 'A'
        folder.mkdir()
        (folder / '001.jpg').write_bytes(b'\x89PNG\r\n\x1a\n')
        library = self.plugin.library
        library.update_recent('A', 5)

        first = library.get_state()
        first['recent'][0]['page'] = 99
        first['recent'][0]['title'] = '改名'

        second = library.get_state()
        self.assertEqual(second['recent'][0]['page'], 5)
        self.assertEqual(second['recent'][0]['title'], 'A')
        self.assertEqual(library._state['recent'][0]['page'], 5)

    def test_state_file_is_written_atomically(self):
        """阅读状态走原子写：落盘后是完整 JSON，且不残留临时文件。"""
        library = self.plugin.library
        library.update_recent('A', 3)
        library.toggle_favorite('A')
        state_file = self.plugin.paths.state_file

        payload = json.loads(state_file.read_text(encoding='utf-8'))
        self.assertEqual(payload['recent'][0]['id'], 'A')
        self.assertEqual(payload['favorites'], ['A'])
        leftovers = [path.name for path in state_file.parent.iterdir()
                     if path.name.startswith(f'.{state_file.name}.')]
        self.assertEqual(leftovers, [], '原子写不留下临时文件')

    # ===== 阅读状态文件：零丢写、重试、临时文件清理、并发读改写 =====

    def _library_module(self):
        """`Library` 所在的已加载模块（`load_sibling` 的模块名，用于打桩/挂日志捕获）。"""
        return sys.modules[type(self.plugin.library).__module__]

    @staticmethod
    def _capture_errors(logger: logging.Logger) -> tuple:
        """给 logger 挂一个只收 ERROR 的捕获器；返回 (records, handler)。"""
        records: list = []

        class _Capture(logging.Handler):
            def emit(self, record) -> None:      # pragma: no cover - 直接转发
                records.append(record)

        handler = _Capture(level=logging.ERROR)
        logger.addHandler(handler)
        return records, handler

    def test_concurrent_state_writes_lose_nothing(self):
        """打开 / 关闭阅读器与切换收藏由不同桥接线程发起，会同时写同一份状态文件。

        旧实现里 `write_json` 把"目标文件正被别的句柄打开"的 `os.replace` 失败吞成
        `False`，`_save_state` 只记一条日志 —— 实测 4 写线程 × 15 次丢约 43% 的写，
        而用例当时只断言 JSON 可解析，仍然通过。这里用一个**有界**的外部占用
        （持有句柄 200ms）触发瞬时冲突，断言**零丢写**：不得出现保存失败日志，
        全部写结束后落盘内容必须等于内存状态（最后一次写已落盘）。
        """
        library = self.plugin.library
        module = self._library_module()
        state_file = self.plugin.paths.state_file
        library.update_recent('A', 1)
        errors: list = []
        held = threading.Event()
        release = threading.Event()
        records, handler = self._capture_errors(module.log)

        def squatter() -> None:
            """模拟杀毒 / 同步盘短暂占住状态文件：持有句柄期间改名会失败。"""
            try:
                with open(state_file, encoding='utf-8') as handle:
                    handle.read()
                    held.set()
                    release.wait(5)
            except OSError:
                held.set()

        def writer(offset: int) -> None:
            try:
                held.wait(5)
                for page in range(15):
                    library.update_recent('A', offset * 100 + page)
            except Exception as error:      # pragma: no cover - 失败时给断言留证据
                errors.append(error)

        try:
            squatter_thread = threading.Thread(target=squatter)
            squatter_thread.start()
            self.assertTrue(held.wait(5), '占用方没有拿到状态文件句柄')
            writers = [threading.Thread(target=writer, args=(index,))
                       for index in range(4)]
            for thread in writers:
                thread.start()
            time.sleep(0.2)         # 占用窗口：这段时间内的写只能靠重试追平
            release.set()
            for thread in writers:
                thread.join(10)
            squatter_thread.join(10)
        finally:
            release.set()
            module.log.removeHandler(handler)

        failures = [record.getMessage() for record in records
                    if '保存阅读状态失败' in record.getMessage()]
        self.assertEqual(errors, [])
        self.assertEqual(failures, [], '每一次状态写都必须最终成功（不允许静默丢写）')
        try:
            payload = json.loads(state_file.read_text(encoding='utf-8'))
        except ValueError as error:
            self.fail(f'状态文件不是完整 JSON：{error}')
        self.assertEqual(payload, library._state,
                         '全部写结束后，落盘内容必须与内存状态一致')
        self.assertEqual(payload['recent'][0]['id'], 'A')
        leftovers = [path.name for path in state_file.parent.iterdir()
                     if path.name.startswith(f'.{state_file.name}.')]
        self.assertEqual(leftovers, [], '并发写不留下临时文件')

    def test_state_write_retries_transient_failures(self):
        """`write_json` 把瞬时冲突吞成 False：用户数据必须整次重试到成功。"""
        library = self.plugin.library
        module = self._library_module()
        state_file = self.plugin.paths.state_file
        real_write_json = module.write_json
        calls = {'count': 0}
        records, handler = self._capture_errors(module.log)

        def flaky_write_json(*args, **kwargs):
            calls['count'] += 1
            if calls['count'] <= 2:
                return False        # 前两次模拟 WinError 5（目标文件被占用）
            return real_write_json(*args, **kwargs)

        module.write_json = flaky_write_json
        try:
            library.update_recent('A', 7)
        finally:
            module.write_json = real_write_json
            module.log.removeHandler(handler)

        self.assertEqual(calls['count'], 3, '前两次失败后必须重试到成功')
        failures = [record.getMessage() for record in records
                    if '保存阅读状态失败' in record.getMessage()]
        self.assertEqual(failures, [], '重试成功时不该报保存失败')
        payload = json.loads(state_file.read_text(encoding='utf-8'))
        self.assertEqual(payload['recent'][0]['page'], 7)

    def test_stale_temp_files_are_cleaned_on_load(self):
        """硬杀留下的 `.manga_state.json.<随机>.tmp` 在加载时清掉。"""
        library = self.plugin.library
        library.update_recent('A', 1)
        state_file = self.plugin.paths.state_file
        stale = state_file.with_name(f'.{state_file.name}.crashed.tmp')
        stale.write_text('{"favorites": [', encoding='utf-8')

        library.rebind()

        self.assertFalse(stale.exists(), '硬杀留下的临时文件要在加载时清掉')
        self.assertTrue(state_file.exists(), '清理不能动已落盘的状态文件')

    def test_concurrent_update_recent_keeps_both_books(self):
        """读-改-写整体在锁内：两个桥接线程同时更新不同漫画时不丢条目。"""
        library = self.plugin.library
        module = self._library_module()
        real_datetime = module.datetime
        barrier = threading.Barrier(2, timeout=0.5)
        errors: list = []

        class _SlowDatetime:
            @staticmethod
            def now():
                # 卡在"读旧列表之后、赋值之前"：另一个线程此时也会读到旧列表
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
                return real_datetime.now()

        def worker(folder_name: str) -> None:
            try:
                library.update_recent(folder_name, 3)
            except Exception as error:      # pragma: no cover - 失败时给断言留证据
                errors.append(error)

        module.datetime = _SlowDatetime
        try:
            threads = [threading.Thread(target=worker, args=(name,))
                       for name in ('A', 'B')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        finally:
            module.datetime = real_datetime

        self.assertEqual(errors, [])
        ids = [entry['id'] for entry in library._state['recent']]
        self.assertEqual(sorted(ids), ['A', 'B'], f'并发 update_recent 丢条目：{ids}')
        payload = json.loads(self.plugin.paths.state_file.read_text(encoding='utf-8'))
        self.assertEqual(sorted(entry['id'] for entry in payload['recent']), ['A', 'B'])

    def test_freshness_spec_fields_are_unchanged(self):
        spec = self.plugin.freshness_spec()
        self.assertEqual(
            sorted(spec),
            sorted(['roots', 'include', 'derive', 'prune', 'on_verified', 'rebuild',
                    'content_version', 'unit', 'min_sync_interval', 'stat_entries']))
        self.assertEqual(spec['unit'], '本')
        self.assertEqual(spec['min_sync_interval'], 5.0)
        self.assertIs(spec['stat_entries'], False)
        self.assertEqual(spec['content_version'], self.plugin.SHELF_VERSION)
        self.assertEqual(list(spec['roots']()), [self.root.resolve()])
        self.assertIn('.jpg', spec['include'])

    def test_root_change_rebinds_components(self):
        """换根必须同时搬书架状态与下载任务表，并清掉刷新引擎的去抖。"""
        new_root = Path(self._tmp.name) / 'new-manga'
        new_root.mkdir()
        engine = engine_for(self.plugin)
        engine.spec.min_sync_interval = 0.0
        self.plugin.update_setting('root_dir', str(new_root))

        self.plugin.on_settings_changed({'root_dir'})

        self.assertEqual(self.plugin.get_data_root(), new_root.resolve())
        self.assertEqual(self.plugin.paths.state_file,
                         new_root.resolve() / '.cache' / 'manga_state.json')
        self.assertEqual(self.plugin.get_settings()['root_dir'],
                         str(new_root.resolve()))
        # 下载任务表跟着根走：旧根里的任务不该出现在新根上
        self.assertEqual(self.plugin.downloads.list_tasks()['tasks'], [])
        self.assertTrue(self.plugin.downloads._state_file.startswith(
            str(new_root.resolve())))


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
