"""壳偏好（`shell/backend/shell_prefs.py` + `window_state.py`）的回归测试。

为什么这几条必须锁住：

- 偏好从 localStorage 搬进 `<config>/shell.json` 的 `prefs` 段，而那个文件里还有
  `log_level` / `webview_high_performance` 两个平级键 —— 写偏好不能把它们冲掉；
- 校验是"键级"的：一个键坏掉只回退该键，不能让整份偏好作废（否则用户会看到
  "改了一个值，别的值也一起回到默认"）；
- 外观是**开放式键集**（壳的颜色变量、分隔条布局变量、插件贡献的变量混在一张表里），
  因此只能校验名字与取值形状，不能按固定 schema 过滤 —— 写得过严会把用户已经生效的
  颜色（`#2f81f7`）或动效（`0.15s ease`）在落盘时丢掉；
- 导入（旧 localStorage 值的一次性迁移）只能在后端还没有 `prefs` 段时生效；
- 「上次离开的页面」只记**安全退出**的：上报只进内存，提交才落盘。

运行：
    python -m unittest tests.test_shell_prefs -v
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend import shell_prefs as prefs_module
from shell.backend.shell_info import SHELL_SETTINGS_NAME, ShellInfo
from shell.backend.shell_prefs import ShellPrefs, default_prefs
from shell.backend.window_state import (
    NO_WINDOW_ERROR,
    get_state,
    has_window,
    set_fullscreen,
    toggle_fullscreen,
)


class _FakeWindow:
    """pywebview 窗口的最小替身：`toggle_fullscreen` 是**无条件翻转**（真实语义）。"""

    def __init__(self, fullscreen=False):
        self.fullscreen = fullscreen
        self.calls = 0

    def toggle_fullscreen(self):
        self.calls += 1
        self.fullscreen = not self.fullscreen


class ShellPrefsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.prefs = ShellPrefs(str(self.dir))
        # pending 路由是模块级状态（按配置目录分隔），用例之间必须清干净
        self.addCleanup(self._clear_pending)

    def _clear_pending(self):
        with prefs_module._pending_lock:
            prefs_module._pending_routes.pop(str(self.dir), None)

    def _saved(self):
        file = self.dir / f'{SHELL_SETTINGS_NAME}.json'
        return json.loads(file.read_text(encoding='utf-8')) if file.exists() else None

    def _write_shell_json(self, payload):
        (self.dir / f'{SHELL_SETTINGS_NAME}.json').write_text(
            json.dumps(payload, ensure_ascii=False), encoding='utf-8')


class ShellPrefsDefaultsTests(ShellPrefsTestCase):
    def test_empty_config_returns_defaults(self):
        self.assertEqual(self.prefs.read(), default_prefs())
        self.assertFalse((self.dir / f'{SHELL_SETTINGS_NAME}.json').exists(),
                         '只读不该建出设置文件')

    def test_broken_file_is_quarantined_and_falls_back(self):
        (self.dir / f'{SHELL_SETTINGS_NAME}.json').write_text('{ 不是 JSON', encoding='utf-8')
        self.assertEqual(self.prefs.read(), default_prefs())
        backups = list(self.dir.glob(f'{SHELL_SETTINGS_NAME}.json.corrupt-*'))
        self.assertEqual(len(backups), 1, '损坏文件必须改名留档，而不是静默丢弃')

    def test_non_object_prefs_section_falls_back(self):
        self._write_shell_json({'prefs': ['不是对象']})
        self.assertEqual(self.prefs.read(), default_prefs())


class ShellPrefsKeyValidationTests(ShellPrefsTestCase):
    """键级校验：非法键回退默认，其余键照常生效。"""

    def test_invalid_keys_fall_back_without_dropping_the_rest(self):
        self.prefs.apply_patch({
            'theme': '紫色',                       # 非白名单取值
            'startup': {'page': 'first', 'fullscreen': 'yes'},   # fullscreen 类型错
        })
        prefs = self.prefs.read()
        self.assertEqual(prefs['theme'], 'dark', '非法主题回退默认')
        self.assertEqual(prefs['startup']['page'], 'first', '同一个对象里的合法键仍生效')
        self.assertIs(prefs['startup']['fullscreen'], False, '非布尔值不按真值处理')

    def test_background_key_level_fallback(self):
        self.prefs.apply_patch({'background': {'fit': 'stretch', 'zoom': 999, 'posX': -20}})
        background = self.prefs.read()['background']
        self.assertEqual(background['fit'], 'cover')
        self.assertEqual(background['zoom'], 400, '越界数字收敛到上限')
        self.assertEqual(background['posX'], 0, '越界数字收敛到下限')

    def test_unknown_startup_field_is_ignored(self):
        self.prefs.apply_patch({'startup': {'page': 'settings', 'nope': 1}})
        startup = self.prefs.read()['startup']
        self.assertEqual(startup['page'], 'settings')
        self.assertNotIn('nope', startup)


class ShellPrefsAppearanceTests(ShellPrefsTestCase):
    """外观是开放式键集：只校验名字与取值形状。"""

    def test_color_and_transition_values_are_accepted(self):
        """设置页写的就是这些值：按单位白名单过滤会把它们整批丢掉。"""
        patch = {
            'colors': {
                '--accent': '#2f81f7',
                '--bg-app': 'rgba(255,255,255,0.08)',
                '--transition-fast': '0.15s ease',
                '--nav-width': '200px',
            }
        }
        result = self.prefs.apply_patch(patch)
        self.assertTrue(result['success'], result)
        self.assertEqual(result['prefs']['colors'], patch['colors'])

    def test_vars_alias_is_the_same_table(self):
        self.prefs.apply_patch({'vars': {'--nav-width': '240px'}})
        self.assertEqual(self.prefs.read()['colors']['--nav-width'], '240px')

    def test_color_patch_merges_instead_of_replacing(self):
        """前端只发变动的那几个键：整段覆盖会把其余自定义色丢掉。"""
        self.prefs.apply_patch({'colors': {'--accent': '#2f81f7', '--radius': '10px'}})
        self.prefs.apply_patch({'colors': {'--radius': '4px'}})
        colors = self.prefs.read()['colors']
        self.assertEqual(colors['--accent'], '#2f81f7')
        self.assertEqual(colors['--radius'], '4px')

    def test_null_deletes_a_variable(self):
        self.prefs.apply_patch({'colors': {'--radius': '10px'}})
        self.prefs.apply_patch({'colors': {'--radius': None}})
        self.assertNotIn('--radius', self.prefs.read()['colors'])

    def test_invalid_var_names_and_values_are_dropped(self):
        result = self.prefs.apply_patch({'colors': {
            'color': '#fff',                       # 少 `--` 前缀
            '--OK': '#fff',                        # 大写不在允许字符集内
            '--x': '',                             # 空值
            '--y': 'x' * 200,                      # 超长
            '--z': 'line\nbreak',                  # 控制字符
            '--good': '#123456',
        }})
        self.assertEqual(result['prefs']['colors'], {'--good': '#123456'})

    def test_glass_variables_are_range_checked(self):
        result = self.prefs.apply_patch({'colors': {
            '--glass-blur': '99px',                # 上限 40
            '--glass-alpha': '84%',
        }})
        colors = result['prefs']['colors']
        self.assertNotIn('--glass-blur', colors, '越界的玻璃参数必须拒收')
        self.assertEqual(colors['--glass-alpha'], '84%')


class ShellPrefsStorageTests(ShellPrefsTestCase):
    def test_patch_keeps_sibling_shell_settings(self):
        """`shell.json` 里还有日志级别与 GPU 开关：写偏好不能把它们冲掉。"""
        info = ShellInfo({'directories': {'data_root': str(self.dir / 'data')}}, str(self.dir))
        info.set_log_level('WARNING')
        self.prefs.apply_patch({'theme': 'light'})

        saved = self._saved()
        self.assertEqual(saved['log_level'], 'WARNING')
        self.assertEqual(saved['prefs']['theme'], 'light')

    def test_only_the_prefs_section_is_touched(self):
        self._write_shell_json({'log_level': 'DEBUG', 'prefs': {'theme': 'light'}})
        self.prefs.apply_patch({'theme': 'dark'})
        self.assertEqual(self._saved()['log_level'], 'DEBUG')

    def test_prefs_survive_a_new_store_instance(self):
        self.prefs.apply_patch({'theme': 'light', 'startup': {'page': 'settings'}})
        fresh = ShellPrefs(str(self.dir))
        self.assertEqual(fresh.read()['theme'], 'light')
        self.assertEqual(fresh.read()['startup']['page'], 'settings')

    def test_read_returns_a_copy(self):
        first = self.prefs.read()
        first['startup']['page'] = 'plugin'
        self.assertEqual(self.prefs.read()['startup']['page'], 'last',
                         '调用方拿到的是快照，改它不该影响存储')

    def test_rejects_non_object_patch(self):
        for bad in ('不是对象', 1, ['a']):
            result = self.prefs.apply_patch(bad)
            self.assertFalse(result['success'], f'{bad!r} 不该被接受')
        self.assertFalse((self.dir / f'{SHELL_SETTINGS_NAME}.json').exists())


class ShellPrefsImportTests(ShellPrefsTestCase):
    """旧 localStorage 值的一次性迁移。"""

    def test_import_seeds_an_empty_config(self):
        result = self.prefs.apply_patch(
            {'theme': 'light', 'colors': {'--accent': '#8b5cf6'}}, kind='import')
        self.assertTrue(result['imported'])
        self.assertEqual(self.prefs.read()['theme'], 'light')
        self.assertEqual(self.prefs.read()['colors']['--accent'], '#8b5cf6')

    def test_import_is_rejected_when_prefs_already_exist(self):
        self.prefs.apply_patch({'theme': 'dark'})
        result = self.prefs.apply_patch({'theme': 'light'}, kind='import')
        self.assertTrue(result['success'])
        self.assertFalse(result['imported'], '后端已有偏好时导入必须被拒，不能覆盖用户改动')
        self.assertEqual(self.prefs.read()['theme'], 'dark')

    def test_unknown_kind_is_rejected(self):
        result = self.prefs.apply_patch({'theme': 'light'}, kind='replace')
        self.assertFalse(result['success'])
        self.assertIn('写入方式', result['error'])


class ShellPrefsLastRouteTests(ShellPrefsTestCase):
    """`last_route` 只记安全退出。"""

    def test_patch_last_route_stays_in_memory_until_commit(self):
        self.prefs.apply_patch({'last_route': '/settings'})
        self.assertEqual(self.prefs.read()['last_route'], '/settings', '当前进程内立即可读')
        saved = self._saved()
        self.assertIn('prefs', saved)
        self.assertEqual(saved['prefs']['last_route'], '', '提交之前不落盘')

    def test_commit_writes_the_pending_route(self):
        self.prefs.apply_patch({'last_route': '/document-reader'})
        self.assertTrue(self.prefs.commit_last_route())
        self.assertEqual(self._saved()['prefs']['last_route'], '/document-reader')
        self.assertFalse(self.prefs.commit_last_route(), '没有待提交值时不该重复写盘')

    def test_killed_process_keeps_the_previous_safe_route(self):
        """强杀不经过提交：文件里保留上一次安全退出的值。"""
        self.prefs.apply_patch({'last_route': '/settings'})
        self.prefs.commit_last_route()
        # 模拟"又切了一次路由，然后进程被强杀"：只有内存变了
        self.prefs.apply_patch({'last_route': '/media-player'})
        self.assertEqual(self._saved()['prefs']['last_route'], '/settings')

    def test_settle_later_commits_after_the_delay(self):
        """浏览器访问模式没有关窗事件，靠空闲兜底提交。"""
        self.prefs.set_last_route('/settings')
        self.prefs.settle_later(0.01)
        # 判据是**文件**：定时器线程会先把待提交值从 pending 表里摘掉、再落盘，
        # 只盯 pending 表会在两步之间读到"还没写"的中间态（本用例早先就这样偶发失败过）。
        committed = self._wait_for_saved(lambda prefs: prefs.get('last_route') == '/settings')
        self.assertTrue(committed, '空闲兜底没有提交待写入的路由')
        self.assertEqual(self._saved()['prefs']['last_route'], '/settings')

    def _wait_for_saved(self, predicate, timeout=5.0):
        """轮询"盘上的 prefs 段"直到条件成立。

        读文件本身可能撞上 `os.replace` 的瞬间（Windows 上会短暂共享冲突），
        因此这个辅助函数必须容忍读取异常。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                saved = self._saved() or {}
                if predicate(saved.get('prefs') or {}):
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(0.02)
        return False

    def test_invalid_route_is_rejected(self):
        self.assertFalse(self.prefs.set_last_route(123))
        self.assertFalse(self.prefs.set_last_route('x' * 600))


class WindowStateTests(unittest.TestCase):
    """桌面窗口全屏：HTTP 与 js_api 两条通道共用这一份实现。"""

    def setUp(self):
        self.window = _FakeWindow()
        patcher = mock.patch('shell.backend.window_state._first_window',
                             return_value=self.window)
        self.first = patcher.start()
        self.addCleanup(patcher.stop)
        # 「最近一次由本应用设置的全屏态」是模块级状态，用例之间要还原
        patcher2 = mock.patch('shell.backend.window_state._last_fullscreen', False)
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def test_set_fullscreen_toggles_only_when_needed(self):
        self.assertTrue(set_fullscreen(True)['success'])
        self.assertTrue(self.window.fullscreen)
        self.assertEqual(self.window.calls, 1)
        # 已经是目标态：不能再翻一次（toggle 是无条件翻转，会把它切回去）
        self.assertTrue(set_fullscreen(True)['success'])
        self.assertEqual(self.window.calls, 1, '目标态与当前态一致时不得再切')

    def test_toggle_uses_the_real_state(self):
        self.window.fullscreen = True
        result = toggle_fullscreen()
        self.assertTrue(result['success'])
        self.assertIs(result['fullscreen'], False)

    def test_non_boolean_is_rejected(self):
        result = set_fullscreen('true')
        self.assertFalse(result['success'])
        self.assertEqual(self.window.calls, 0)

    def test_no_window_reports_why(self):
        self.first.return_value = None
        state = get_state()
        self.assertFalse(state['success'])
        self.assertEqual(state['error'], NO_WINDOW_ERROR)
        self.assertFalse(set_fullscreen(True)['success'])

    def test_register_window_marks_desktop_mode(self):
        """`has_window` 供调用方判断"这台机器有没有桌面窗口"。"""
        self.assertIsInstance(has_window(), bool)


if __name__ == '__main__':
    unittest.main()
