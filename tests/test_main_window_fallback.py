"""`main.py` 桌面窗口后端的探测、降级与 CI 冒烟钩子。

窗口起不来的原因在环境（Windows 缺 WebView2 Runtime、Linux 缺 GTK/WebKitGTK），
不在代码，所以探测逻辑必须能在任意平台上单测：平台判定（`_probe_window_backend`）
与各平台的探测实现分开，用例用 monkeypatch 造出 Linux 的 gi 与 Windows 的注册表 /
安装目录。本文件不建真窗、不依赖显示器，也不启动 Flask。

运行：
    python -m unittest tests.test_main_window_fallback -v
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_main():
    """按绝对路径加载仓库根的 main.py。

    不能直接 `import main`：其他用例会把插件 backend 目录插到 `sys.path`（其中有同名
    main.py），`unittest discover` 的导入顺序会让这里的 `import main` 命中插件模块。
    """
    spec = importlib.util.spec_from_file_location('cubby_main_window_fallback_under_test',
                                                  PROJECT_ROOT / 'main.py')
    if spec is None or spec.loader is None:
        raise ImportError('无法为 main.py 创建模块加载器')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = _load_main()


def _fake_gi(available_versions):
    """构造假的 gi 模块：只有 `available_versions` 里的 WebKit2 版本能用。

    返回 `(模块, 调用记录)`；调用记录用于断言"先试 4.1 再退 4.0"。
    """
    module = types.ModuleType('gi')
    calls = []

    def require_version(namespace, version):
        calls.append((namespace, version))
        if version not in available_versions:
            raise ValueError(f'Namespace {namespace} not available for version {version}')

    module.require_version = require_version
    return module, calls


class LinuxBackendProbeTests(unittest.TestCase):
    """Linux 探测的三条分支：缺 gi、缺 WebKit2、可用。"""

    def test_missing_gi_is_reported(self):
        with mock.patch.object(main.importlib.util, 'find_spec', lambda name: None):
            available, reason = main._probe_window_backend('Linux')
        self.assertFalse(available)
        self.assertIn('PyGObject', reason)
        self.assertIn('gi', reason)

    def test_missing_webkit2_is_reported_after_trying_both_versions(self):
        gi_module, calls = _fake_gi(available_versions=())
        with mock.patch.object(main.importlib.util, 'find_spec', lambda name: object()), \
                mock.patch.dict(sys.modules, {'gi': gi_module}):
            available, reason = main._linux_window_backend_probe()
        self.assertFalse(available)
        self.assertIn('WebKit2', reason)
        self.assertEqual([version for _, version in calls], ['4.1', '4.0'],
                         'WebKit2 必须先试 4.1、再退 4.0')

    def test_unloadable_typelib_is_reported(self):
        """require_version 通过但类型库导不进来（装了一半的 webkit2gtk）同样算不可用。"""
        gi_module, _ = _fake_gi(available_versions=('4.1',))
        repository = types.ModuleType('gi.repository')     # 没有 WebKit2 属性
        with mock.patch.object(main.importlib.util, 'find_spec', lambda name: object()), \
                mock.patch.dict(sys.modules, {'gi': gi_module, 'gi.repository': repository}):
            available, reason = main._linux_window_backend_probe()
        self.assertFalse(available)
        self.assertIn('WebKit2', reason)

    def test_falls_back_to_webkit2_4_0(self):
        gi_module, calls = _fake_gi(available_versions=('4.0',))
        repository = types.ModuleType('gi.repository')
        repository.WebKit2 = object()
        with mock.patch.object(main.importlib.util, 'find_spec', lambda name: object()), \
                mock.patch.dict(sys.modules, {'gi': gi_module, 'gi.repository': repository}):
            available, reason = main._linux_window_backend_probe()
        self.assertTrue(available, reason)
        self.assertEqual(reason, '')
        self.assertEqual([version for _, version in calls], ['4.1', '4.0'])

    def test_webkit2_4_1_is_preferred(self):
        gi_module, calls = _fake_gi(available_versions=('4.1', '4.0'))
        repository = types.ModuleType('gi.repository')
        repository.WebKit2 = object()
        with mock.patch.object(main.importlib.util, 'find_spec', lambda name: object()), \
                mock.patch.dict(sys.modules, {'gi': gi_module, 'gi.repository': repository}):
            available, reason = main._linux_window_backend_probe()
        self.assertTrue(available, reason)
        self.assertEqual([version for _, version in calls], ['4.1'])


class _FakeKey:
    """注册表键的上下文管理替身（`with winreg.OpenKey(...)` 用得到）。"""

    def __init__(self, version):
        self.version = version

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeWinreg(types.ModuleType):
    """winreg 替身：只实现探测用到的三个名字。

    `versions` 以 `(根, 路径)` 为键，缺失即 OpenKey 抛 FileNotFoundError，与真实
    注册表"键不存在"的行为一致。
    """

    HKEY_CURRENT_USER = 'HKCU'
    HKEY_LOCAL_MACHINE = 'HKLM'

    def __init__(self, versions):
        super().__init__('winreg')
        self.versions = versions
        self.opened = []

    def OpenKey(self, root, path):
        self.opened.append((root, path))
        if (root, path) not in self.versions:
            raise FileNotFoundError(path)
        return _FakeKey(self.versions[(root, path)])

    def QueryValueEx(self, key, name):
        return key.version, 1


class WindowsBackendProbeTests(unittest.TestCase):
    """Windows 探测：注册表命中/未命中、安装目录兜底。"""

    def test_registry_version_is_read_from_the_first_available_key(self):
        path = main._WEBVIEW2_REGISTRY_PATHS[0]
        winreg = _FakeWinreg({('HKCU', path): '120.0.2210.91'})
        with mock.patch.dict(sys.modules, {'winreg': winreg}):
            version = main._webview2_registry_version()
        self.assertEqual(version, '120.0.2210.91')
        self.assertEqual(winreg.opened[0], ('HKCU', path))

    def test_registry_hit_makes_the_backend_available(self):
        path = main._WEBVIEW2_REGISTRY_PATHS[1]
        winreg = _FakeWinreg({('HKLM', path): '120.0.2210.91'})
        with mock.patch.dict(sys.modules, {'winreg': winreg}), \
                mock.patch.object(main, '_webview2_browser_executable', return_value=None):
            available, reason = main._windows_window_backend_probe()
        self.assertTrue(available, reason)

    def test_missing_runtime_reports_an_actionable_reason(self):
        winreg = _FakeWinreg({})
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(sys.modules, {'winreg': winreg}), \
                mock.patch.object(main, '_webview2_install_roots', return_value=[root]):
            available, reason = main._windows_window_backend_probe()
        self.assertFalse(available)
        self.assertIn('WebView2', reason)
        self.assertIn('mshtml', reason)

    def test_install_dir_fallback_makes_the_backend_available(self):
        with tempfile.TemporaryDirectory() as root:
            executable = (Path(root) / 'Microsoft' / 'EdgeWebView' / 'Application'
                          / '120.0.2210.91' / 'msedgewebview2.exe')
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'')
            winreg = _FakeWinreg({})       # 注册表被清理过，只剩安装目录
            with mock.patch.dict(sys.modules, {'winreg': winreg}), \
                    mock.patch.object(main, '_webview2_install_roots', return_value=[root]):
                found = main._webview2_browser_executable()
                available, reason = main._windows_window_backend_probe()
        self.assertEqual(found, executable)
        self.assertTrue(available, reason)

    def test_registry_is_unavailable_off_windows(self):
        """`winreg` 不存在时返回空串，而不是让探测抛异常（该函数在 Linux 上也会被调用）。"""
        with mock.patch.dict(sys.modules, {'winreg': None}), \
                mock.patch.object(main, '_webview2_install_roots', return_value=[]):
            self.assertEqual(main._webview2_registry_version(), '')
            self.assertFalse(main._windows_window_backend_probe()[0])


class PlatformDispatchTests(unittest.TestCase):
    """平台判定与探测实现分开：任意平台都能验证分派逻辑。"""

    def test_dispatch_by_platform_name(self):
        with mock.patch.object(main, '_linux_window_backend_probe',
                               return_value=(False, '缺 gi')), \
                mock.patch.object(main, '_windows_window_backend_probe',
                                  return_value=(True, '')):
            self.assertEqual(main._probe_window_backend('Linux'), (False, '缺 gi'))
            self.assertEqual(main._probe_window_backend('OpenBSD'), (False, '缺 gi'))
            self.assertEqual(main._probe_window_backend('Windows'), (True, ''))

    def test_other_platforms_are_assumed_usable(self):
        with mock.patch.object(main, '_linux_window_backend_probe',
                               side_effect=AssertionError('Darwin 不应走 Linux 探测')), \
                mock.patch.object(main, '_windows_window_backend_probe',
                                  side_effect=AssertionError('Darwin 不应走 Windows 探测')):
            self.assertEqual(main._probe_window_backend('Darwin'), (True, ''))

    def test_default_platform_comes_from_platform_system(self):
        with mock.patch.object(main.platform, 'system', return_value='Linux'), \
                mock.patch.object(main, '_linux_window_backend_probe',
                                  return_value=(False, '缺 gi')):
            self.assertEqual(main._probe_window_backend(), (False, '缺 gi'))


class WindowRouteTests(unittest.TestCase):
    """降级决策：纯函数，不碰环境。"""

    def test_explicit_web_only_is_not_a_fallback(self):
        self.assertEqual(main._resolve_window_route(True, (False, '缺 GTK')), (True, ''))

    def test_unavailable_backend_degrades_with_the_reason(self):
        self.assertEqual(main._resolve_window_route(False, (False, '缺 GTK')),
                         (True, '缺 GTK'))

    def test_available_backend_keeps_the_desktop_window(self):
        self.assertEqual(main._resolve_window_route(False, (True, '')), (False, ''))

    def test_startup_route_skips_the_probe_for_explicit_web_only(self):
        with mock.patch.object(main.sys, 'argv', ['main.py', '--web-only']), \
                mock.patch.object(main, '_probe_window_backend',
                                  side_effect=AssertionError('显式 --web-only 不应探测平台')):
            self.assertEqual(main._resolve_startup_route(), (True, ''))

    def test_startup_route_uses_the_probe_result(self):
        with mock.patch.object(main.sys, 'argv', ['main.py']), \
                mock.patch.object(main, '_probe_window_backend',
                                  return_value=(False, '缺 GTK')):
            self.assertEqual(main._resolve_startup_route(), (True, '缺 GTK'))


class WindowSmokeEnvTests(unittest.TestCase):
    """`CUBBY_WINDOW_SMOKE_MS` 的解析：只认正整数，其余一律忽略。"""

    def _delay(self, raw=None):
        environ = dict(os.environ)
        environ.pop(main.WINDOW_SMOKE_ENV, None)
        if raw is not None:
            environ[main.WINDOW_SMOKE_ENV] = raw
        with mock.patch.dict(os.environ, environ, clear=True):
            return main._window_smoke_delay_ms()

    def test_unset_means_disabled(self):
        self.assertIsNone(self._delay())

    def test_empty_means_disabled(self):
        self.assertIsNone(self._delay(''))

    def test_positive_integer_is_used_as_milliseconds(self):
        self.assertEqual(self._delay('2500'), 2500)

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(self._delay('  3000  '), 3000)

    def test_zero_is_rejected_with_a_warning(self):
        with self.assertLogs(main.log, level='WARNING') as captured:
            self.assertIsNone(self._delay('0'))
        self.assertIn(main.WINDOW_SMOKE_ENV, '\n'.join(captured.output))

    def test_negative_is_rejected_with_a_warning(self):
        with self.assertLogs(main.log, level='WARNING') as captured:
            self.assertIsNone(self._delay('-500'))
        self.assertIn(main.WINDOW_SMOKE_ENV, '\n'.join(captured.output))

    def test_non_integer_is_rejected_with_a_warning(self):
        with self.assertLogs(main.log, level='WARNING') as captured:
            self.assertIsNone(self._delay('1.5'))
        self.assertIn(main.WINDOW_SMOKE_ENV, '\n'.join(captured.output))


class WindowSmokeCloserTests(unittest.TestCase):
    """冒烟回调本体：等窗口显示 → 按毫秒计时 → 关窗；窗口不显示则快速失败。"""

    def test_closer_waits_for_shown_then_schedules_destroy(self):
        window = mock.Mock()
        window.events.shown.wait.return_value = True
        intervals = []

        def fake_timer(seconds, callback):
            timer = mock.Mock()

            def start():
                intervals.append(seconds)
                callback()

            timer.start.side_effect = start
            return timer

        with mock.patch.object(main.threading, 'Timer', fake_timer):
            main._window_smoke_closer(window, 1500)()

        self.assertTrue(window.events.shown.wait.called, '必须等窗口显示后再计时')
        self.assertEqual(intervals, [1.5], '计时长度应为配置的毫秒数换算成秒')
        window.destroy.assert_called_once_with()

    def test_timeout_exits_non_zero_with_the_contract_message(self):
        """窗口没显示时 destroy 是空操作：必须让进程以非 0 退出，而不是挂到 CI 超时。"""
        window = mock.Mock()
        window.events.shown.wait.return_value = False
        exits = []
        with mock.patch.object(main.os, '_exit', exits.append), \
                self.assertLogs(main.log, level='ERROR') as captured:
            main._window_smoke_closer(window, 1000)()
        self.assertEqual(exits, [1])
        self.assertIn('[Cubby] 桌面窗口未能在 10 秒内显示', '\n'.join(captured.output))
        window.destroy.assert_not_called()

    def test_missing_window_exits_non_zero_instead_of_hanging(self):
        exits = []
        with mock.patch.object(main.os, '_exit', exits.append), \
                self.assertLogs(main.log, level='ERROR'):
            main._window_smoke_closer(None, 1000)()
        self.assertEqual(exits, [1])


class WindowIconTests(unittest.TestCase):
    """窗口图标按平台取：Windows / 其它平台用 ICO，Linux 用 Pillow 转出的 PNG。"""

    def test_windows_keeps_the_ico(self):
        path = main._window_icon_path('Windows')
        self.assertEqual(path, str(main._brand_icon_source_path()))
        self.assertTrue(path.endswith('.ico'), 'winforms 的 System.Drawing.Icon 只认 ICO')

    def test_other_platforms_keep_the_existing_behaviour(self):
        self.assertEqual(main._window_icon_path('Darwin'), str(main._brand_icon_source_path()))

    def test_linux_returns_a_converted_png(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as config_dir, \
                mock.patch.object(main, 'get_config_dir', lambda: Path(config_dir)):
            path = main._window_icon_path('Linux')
            self.assertIsNotNone(path)
            png = Path(path)
            self.assertEqual(png.name, 'brand-icon.png')
            self.assertTrue(png.is_file())
            with Image.open(png) as image:
                self.assertEqual(image.format, 'PNG')
                self.assertEqual(image.size, (256, 256), '应取 ICO 里最大的一帧')

    def test_conversion_result_is_cached(self):
        conversions = []
        real_convert = main._write_largest_ico_frame_as_png

        def counting_convert(source, target):
            conversions.append(target)
            real_convert(source, target)

        with tempfile.TemporaryDirectory() as config_dir, \
                mock.patch.object(main, 'get_config_dir', lambda: Path(config_dir)), \
                mock.patch.object(main, '_write_largest_ico_frame_as_png', counting_convert):
            first = main._window_icon_path('Linux')
            second = main._window_icon_path('Linux')
        self.assertEqual(first, second)
        self.assertEqual(len(conversions), 1, '缓存命中时不应重复转换')

    def test_conversion_failure_falls_back_to_no_icon(self):
        with tempfile.TemporaryDirectory() as config_dir, \
                mock.patch.object(main, 'get_config_dir', lambda: Path(config_dir)), \
                mock.patch.object(main, '_brand_icon_source_path',
                                  lambda: Path(config_dir) / 'missing.ico'), \
                self.assertLogs(main.log, level='WARNING') as captured:
            self.assertIsNone(main._window_icon_path('Linux'))
        self.assertIn('窗口图标转换失败', '\n'.join(captured.output))

    def test_missing_pillow_falls_back_to_no_icon(self):
        source = main._brand_icon_source_path()
        with tempfile.TemporaryDirectory() as config_dir, \
                mock.patch.object(main, 'get_config_dir', lambda: Path(config_dir)), \
                mock.patch.object(main, '_write_largest_ico_frame_as_png',
                                  side_effect=ImportError('No module named PIL')), \
                self.assertLogs(main.log, level='WARNING') as captured:
            path = main._linux_window_icon(source, Path(config_dir) / 'brand-icon.png')
        self.assertIsNone(path, 'Pillow 不可用时宁缺图标也要能开窗')
        self.assertIn('图标转换失败', '\n'.join(captured.output))


class FallbackLogContractTests(unittest.TestCase):
    """降级日志的契约：CI 靠固定前缀识别"退回了浏览器访问"。"""

    def setUp(self):
        self.config = {'server': {'host': '127.0.0.1', 'port': 18080},
                       'directories': {'data_root': str(PROJECT_ROOT / 'data')}}

    def test_fallback_logs_the_address_and_the_install_command(self):
        served = []
        with self.assertLogs(main.log, level='WARNING') as captured, \
                mock.patch.object(main, '_run_web_only',
                                  lambda config, manager, host, port: served.append((host, port))), \
                mock.patch.object(main.platform, 'system', return_value='Linux'):
            main._run_app(self.config, mock.Mock(), web_only=True,
                          fallback_reason='缺少 PyGObject（gi 模块）')
        self.assertEqual(served, [('127.0.0.1', 18080)])
        text = '\n'.join(captured.output)
        self.assertIn('[Cubby] 桌面窗口不可用: 缺少 PyGObject（gi 模块）', text)
        self.assertIn('已退回 --web-only: http://127.0.0.1:18080', text)
        self.assertIn('apt install gir1.2-gtk-3.0 gir1.2-webkit2-4.1', text)

    def test_windows_hint_points_at_the_webview2_download_page(self):
        with self.assertLogs(main.log, level='WARNING') as captured, \
                mock.patch.object(main, '_run_web_only', lambda *args: None), \
                mock.patch.object(main.platform, 'system', return_value='Windows'):
            main._run_app(self.config, mock.Mock(), web_only=True,
                          fallback_reason='未检测到 WebView2 Runtime')
        self.assertIn(main._WEBVIEW2_DOWNLOAD_URL, '\n'.join(captured.output))

    def test_explicit_web_only_does_not_emit_a_fallback_line(self):
        with mock.patch.object(main, '_run_web_only', lambda *args: None), \
                self.assertNoLogs(main.log, level='WARNING'):
            main._run_app(self.config, mock.Mock(), web_only=True)


class DesktopWindowWiringTests(unittest.TestCase):
    """建窗后的日志与冒烟钩子接线；`webview` 全部替身，不开真窗。"""

    def _run(self, smoke_value=None):
        started = []
        window = mock.Mock()

        def fake_start(func=None, **_kwargs):
            started.append(func)

        environ = dict(os.environ)
        environ.pop(main.WINDOW_SMOKE_ENV, None)
        if smoke_value is not None:
            environ[main.WINDOW_SMOKE_ENV] = smoke_value
        with (
            mock.patch.object(main, 'create_app', return_value=mock.Mock()),
            mock.patch.object(main, 'wait_for_server', return_value=True),
            mock.patch.object(main.webview, 'create_window', return_value=window),
            mock.patch.object(main.webview, 'start', fake_start),
            mock.patch.object(main, 'register_desktop_window', lambda: None),
            mock.patch.object(main, '_webview_storage_dir', lambda: ''),
            mock.patch.object(main, '_window_icon_path', lambda: ''),
            mock.patch.object(main, '_commit_last_route', lambda: None),
            mock.patch.dict(os.environ, environ, clear=True),
            self.assertLogs(main.log, level='INFO') as captured,
        ):
            main._run_desktop_window({'server': {'host': '127.0.0.1', 'port': 18080}},
                                     mock.Mock(), object(), '127.0.0.1', 18080)
        return started, '\n'.join(captured.output)

    def test_window_creation_is_logged(self):
        _, text = self._run()
        self.assertIn('[Cubby] 桌面窗口已创建', text)
        self.assertIn('[Cubby] 窗口后端:', text)

    def test_no_smoke_callback_without_the_env(self):
        started, _ = self._run()
        self.assertEqual(started, [None], '未设置 CUBBY_WINDOW_SMOKE_MS 时不应装钩子')

    def test_smoke_callback_is_handed_to_webview_start(self):
        started, _ = self._run(smoke_value='1500')
        self.assertEqual(len(started), 1)
        self.assertTrue(callable(started[0]),
                        'CUBBY_WINDOW_SMOKE_MS 生效时应把计时关窗回调交给 webview.start')


if __name__ == '__main__':
    unittest.main()
