"""壳自身信息端点（shell/backend/shell_info.py + ThumbCache 维护入口）的回归测试。

为什么要固化这几条：

- `stats()` 若直接走 `_connect()`，光是"看一眼占用"就会给每个插件凭空建出一个
  空数据库（且占用不再为 0），清理按钮的"释放了多少"也会失真；
- 日志级别是**下一次启动、界面出现之前**就要生效的值，落盘格式或键名写错只会
  表现为"设置看起来成功、重启后回到 INFO"。

运行：
    python -m unittest tests.test_shell_info -v
"""

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.shell_info import (
    LOG_LEVELS,
    SHELL_SETTINGS_NAME,
    WEBVIEW_HIGH_PERFORMANCE_ENV,
    WEBVIEW_HIGH_PERFORMANCE_SETTING,
    ShellInfo,
    configure_webview_environment,
    stored_log_level,
    stored_webview_high_performance,
    webview_high_performance_enabled,
)
from shell.backend.thumb_cache import ThumbCache, cache_stats, clear_all_caches
from shell.backend.webview_gpu import apply_gpu_preference


class ThumbCacheStatsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.cache = ThumbCache(self.dir / '.cache' / 'thumbs.db')

    def _seed(self):
        src = self.dir / 'a.jpg'
        src.write_bytes(b'x' * 32)
        self.assertTrue(self.cache.put('a.jpg', b'fake-jpeg-bytes', 'image/jpeg', src))

    def test_stats_does_not_create_db(self):
        self.assertEqual(self.cache.stats(), {'count': 0, 'bytes': 0})
        self.assertFalse(self.cache.db_path.exists(), '只读占用不应建出数据库文件')

    def test_stats_counts_rows_and_bytes(self):
        self._seed()
        stats = self.cache.stats()
        self.assertEqual(stats['count'], 1)
        self.assertGreater(stats['bytes'], 0)
        self.assertIn(str(self.cache.db_path), [item['path'] for item in cache_stats()])

    def test_clear_all_caches_empties_this_cache(self):
        self._seed()
        result = clear_all_caches()
        self.assertGreaterEqual(result['caches'], 1)
        self.assertGreaterEqual(result['freed_bytes'], 0)
        self.assertEqual(self.cache.stats()['count'], 0)


class ShellInfoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.info = ShellInfo({'directories': {'data_root': str(self.dir / 'data')}}, str(self.dir))
        self._previous_level = logging.getLogger().level
        self.addCleanup(logging.getLogger().setLevel, self._previous_level)

    def _saved(self):
        file = self.dir / f'{SHELL_SETTINGS_NAME}.json'
        return json.loads(file.read_text(encoding='utf-8')) if file.exists() else None

    def test_rejects_unknown_level_without_writing(self):
        for bad in ('VERBOSE', '', None, 5):
            self.assertFalse(self.info.set_log_level(bad)['success'], f'{bad!r} 不该被接受')
        self.assertIsNone(self._saved(), '非法级别不应落盘')

    def test_accepts_level_applies_and_persists(self):
        result = self.info.set_log_level('warning')
        self.assertTrue(result['success'])
        self.assertEqual(result['level'], 'WARNING')
        self.assertEqual(self._saved(), {'log_level': 'WARNING'})
        self.assertEqual(logging.getLogger().level, logging.WARNING)
        self.assertEqual(stored_log_level(str(self.dir)), logging.WARNING)
        self.assertEqual(self.info.get_info()['log_level'], 'WARNING')

    def test_absent_or_invalid_stored_level_falls_back_to_info(self):
        self.assertEqual(stored_log_level(str(self.dir)), logging.INFO)
        (self.dir / f'{SHELL_SETTINGS_NAME}.json').write_text(
            '{"log_level": "NOPE"}', encoding='utf-8'
        )
        self.assertEqual(stored_log_level(str(self.dir)), logging.INFO)

    def test_info_reports_paths_and_totals(self):
        info = self.info.get_info()
        self.assertEqual(info['data_root'], str(self.dir / 'data'))
        self.assertEqual(info['config_dir'], str(self.dir))
        self.assertTrue(info['log_file'].endswith('omnibox.log'))
        self.assertIn(info['log_level'], LOG_LEVELS)
        self.assertEqual(info['cache_total']['count'], sum(c['count'] for c in info['caches']))


class WebViewHighPerformanceTests(unittest.TestCase):
    """高性能 GPU 模式的取值与落盘。

    为什么这些必须锁住：

    - 默认值必须是**开启**：关闭即回到已知的拖动卡顿，而开启的代价只是一个浏览器
      标志。默认值一旦漂移，表现为卡顿复现且不产生任何报错。
    - 环境变量必须**优先于** shell.json：该标志失效时窗口无法启动，界面进不去，
      只有启动前就生效的来源可用。优先级反转即失去逃生口。
    - 环境变量只用于关闭，不作开启：只覆盖一个方向，两个来源不会互相翻转。
    - 参数在**写入时**拼接，而不是让读到的值变长：该属性的真身在 .NET 侧，只有写入
      才会传过去。拦读会让"日志显示已注入、进程命令行里却没有"。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.info = ShellInfo({'directories': {'data_root': str(self.dir / 'data')}}, str(self.dir))
        # 环境是本进程共享的，测完必须还原，否则会污染同进程的其他用例
        self._saved_env = os.environ.get(WEBVIEW_HIGH_PERFORMANCE_ENV)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._saved_env is None:
            os.environ.pop(WEBVIEW_HIGH_PERFORMANCE_ENV, None)
        else:
            os.environ[WEBVIEW_HIGH_PERFORMANCE_ENV] = self._saved_env

    def _saved(self):
        file = self.dir / f'{SHELL_SETTINGS_NAME}.json'
        return json.loads(file.read_text(encoding='utf-8')) if file.exists() else None

    def test_default_is_enabled(self):
        self.assertTrue(stored_webview_high_performance(str(self.dir)))
        self.assertTrue(webview_high_performance_enabled(str(self.dir)))
        info = self.info.get_info()
        self.assertIs(info['webview_high_performance'], True)
        self.assertIs(info['webview_high_performance_effective'], True)

    def test_non_boolean_stored_value_falls_back_to_enabled(self):
        # 手改 shell.json 写成字符串 'false' 不该被 bool() 判成真，也不该被当成关闭
        (self.dir / f'{SHELL_SETTINGS_NAME}.json').write_text(
            json.dumps({WEBVIEW_HIGH_PERFORMANCE_SETTING: 'false'}), encoding='utf-8'
        )
        self.assertTrue(stored_webview_high_performance(str(self.dir)))

    def test_rejects_non_boolean_without_writing(self):
        for bad in ('false', 1, 0, None, 'true'):
            result = self.info.set_webview_high_performance(bad)
            self.assertFalse(result['success'], f'{bad!r} 不该被接受')
            self.assertIsNone(self._saved(), '非法取值不应落盘')

    def test_accepts_and_persists_and_reports_restart(self):
        result = self.info.set_webview_high_performance(False)
        self.assertTrue(result['success'])
        self.assertTrue(result['restart_required'], '环境变量只在下次启动生效，必须告知前端')
        self.assertEqual(self._saved(), {WEBVIEW_HIGH_PERFORMANCE_SETTING: False})
        self.assertFalse(stored_webview_high_performance(str(self.dir)))
        self.assertIs(self.info.get_info()['webview_high_performance'], False)

    def test_env_off_overrides_stored_true(self):
        self.assertTrue(stored_webview_high_performance(str(self.dir)))
        os.environ[WEBVIEW_HIGH_PERFORMANCE_ENV] = '0'
        self.assertFalse(webview_high_performance_enabled(str(self.dir)))
        info = self.info.get_info()
        # 保存值仍是 true，生效值是 false —— 前端靠这两个的差异提示"被环境变量压制"
        self.assertIs(info['webview_high_performance'], True)
        self.assertIs(info['webview_high_performance_effective'], False)

    def test_env_cannot_force_enable_when_stored_false(self):
        self.info.set_webview_high_performance(False)
        os.environ[WEBVIEW_HIGH_PERFORMANCE_ENV] = '1'
        self.assertFalse(webview_high_performance_enabled(str(self.dir)),
                         '环境变量只应覆盖为关闭，不应覆盖为开启')

    def test_configure_reports_enabled_by_default(self):
        self.assertTrue(configure_webview_environment(str(self.dir)))

    def test_apply_gpu_preference_records_state(self):
        self.addCleanup(apply_gpu_preference, False)
        self.assertFalse(apply_gpu_preference(False))
        self.assertTrue(apply_gpu_preference(True))

    @unittest.skipUnless(os.name == 'nt', '依赖 Windows 专属的 pywebview 后端')
    def test_install_wraps_edge_chrome_init_once(self):
        """`install` 必须幂等：重复包装会让 __init__ 每层都套一次。"""
        from webview.platforms import edgechromium

        from shell.backend.webview_gpu import install

        before = edgechromium.EdgeChrome.__init__
        self.addCleanup(setattr, edgechromium.EdgeChrome, '__init__', before)
        install(edgechromium.EdgeChrome)
        first = edgechromium.EdgeChrome.__init__
        install(edgechromium.EdgeChrome)
        self.assertIs(edgechromium.EdgeChrome.__init__, first)

    def test_wrapper_does_not_touch_props_when_disabled(self):
        """关闭态不得替换参数类 —— 否则"关闭"只改了登记值，没有真正关掉行为。"""
        import webview.platforms.edgechromium as ec

        from shell.backend.webview_gpu import install

        class FakeEdgeChrome:
            called = False

            def __init__(self):
                FakeEdgeChrome.called = True

        before = ec.CoreWebView2CreationProperties
        self.addCleanup(setattr, ec, 'CoreWebView2CreationProperties', before)

        install(FakeEdgeChrome)
        apply_gpu_preference(False)
        FakeEdgeChrome()
        self.assertTrue(FakeEdgeChrome.called, '关闭态也必须照常构造控件')
        self.assertIs(ec.CoreWebView2CreationProperties, before,
                      '关闭态不得替换参数类')

    def test_wrapper_restores_props_class_after_failure(self):
        """原始 __init__ 抛异常时，finally 必须把被替换的类还原回去。"""
        import webview.platforms.edgechromium as ec

        from shell.backend.webview_gpu import install

        class BoomEdgeChrome:
            def __init__(self):
                raise RuntimeError('构造失败')

        before = ec.CoreWebView2CreationProperties
        self.addCleanup(setattr, ec, 'CoreWebView2CreationProperties', before)

        install(BoomEdgeChrome)
        apply_gpu_preference(True)
        with self.assertRaises(RuntimeError):
            BoomEdgeChrome()
        self.addCleanup(apply_gpu_preference, False)
        self.assertIs(ec.CoreWebView2CreationProperties, before,
                      '异常路径也必须还原参数类')

    def test_preferred_props_class_rewrites_on_assignment(self):
        """偏好在**写入时**拼接，且原有开关不丢、重复写入不累积。

        为什么断言"存储值"而不是"读到的值"：该属性的真身在 .NET 侧，只有当 Python
        侧发生写入时才会传过去。只让读到的字符串变长（拦 `__getattribute__`）不会
        改变 .NET 里的值 —— 那正是此前"日志显示注入成功、命令行里却没有该参数"的原因。
        这里用一个纯 Python 替身模拟"存储"，因此断言的是真正被写回的内容。
        """
        from shell.backend.webview_gpu import GPU_PREFERENCE_ARG, _preferred_props_class

        class FakeProps:
            def __init__(self):
                self.AdditionalBrowserArguments = '--disable-features=ElasticOverscroll'
                self.UserDataFolder = 'x'

        props = _preferred_props_class(FakeProps)()
        stored = object.__getattribute__(props, 'AdditionalBrowserArguments')
        self.assertIn('--disable-features=ElasticOverscroll', stored)
        self.assertIn(GPU_PREFERENCE_ARG, stored)

        # pywebview 会再写一次（`+=` 新开关）：不得变成两份偏好
        props.AdditionalBrowserArguments = stored + ' --allow-file-access-from-files'
        again = object.__getattribute__(props, 'AdditionalBrowserArguments')
        self.assertEqual(again.split().count(GPU_PREFERENCE_ARG), 1)
        self.assertIn('--allow-file-access-from-files', again)

        # 其它属性不参与拼接
        self.assertEqual(props.UserDataFolder, 'x')


if __name__ == '__main__':
    unittest.main()
