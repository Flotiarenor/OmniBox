"""壳偏好的真实浏览器端到端用例（本地手动运行，不进 CI）。

为什么需要它
------------
偏好的读路径从 localStorage 搬到了「服务端注入 + `<config>/shell.json`」，
这条链路上有两类缺陷只有真实浏览器 + 真实服务能抓到：

  - 注入块放晚了：`data-theme` 会在首帧之后才被改，界面上闪一下默认色；
  - 写路径断了：设置页改了、界面也变了，但落盘没发生 —— 重启后回到旧值，
    而这一切在控制台里不报任何错（这正是原始缺陷的形状）。

因此本用例驱动真实 Chrome，断言「注入状态 → 首帧外观 → 改设置 → 落盘 → 重启后仍是新值」
这条完整链路，并顺带覆盖「上次离开的页面只记安全退出」的语义。

本地运行
--------
    venv/Scripts/pip install -r requirements-e2e.txt
    venv/Scripts/python -m unittest tests.test_shell_prefs_browser_e2e -v

依赖：本机已安装 Chrome（Edge 不算，理由见 `tests/harness/browser_binary.py`）与
selenium 4.6+（自带 driver 管理，自动下载驱动）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.harness.app_instance import AppInstance, boot_prerequisites, wait_until
from tests.harness.browser_binary import browser_binary

#: 界面操作后的等待上限（真实浏览器里 Vue 的更新是异步的）
UI_TIMEOUT = 10.0
#: 首屏（插件列表 + 壳挂载）的等待上限
STARTUP_TIMEOUT = 40.0
#: 去抖窗口（前端 300ms）之后落盘需要的时间
FLUSH_WAIT = 6.0

BOOT_OK, BOOT_REASON = boot_prerequisites()


def _have_selenium() -> bool:
    try:
        import selenium  # noqa: F401
        from selenium import webdriver  # noqa: F401
    except Exception:
        return False
    return True


def _saved_prefs(instance: AppInstance) -> dict:
    """实例配置目录里 `shell.json` 的 prefs 段（不存在时返回空字典）。"""
    file = instance.config_dir / 'shell.json'
    if not file.exists():
        return {}
    return json.loads(file.read_text(encoding='utf-8')).get('prefs', {})


SKIP_REASON = None if (_have_selenium() and browser_binary() is not None and BOOT_OK) else (
    '跳过：' + ('未安装 selenium（见 requirements-e2e.txt）' if not _have_selenium()
               else '未检测到 Chrome' if browser_binary() is None else BOOT_REASON))


@unittest.skipIf(SKIP_REASON, SKIP_REASON or '')
class ShellPrefsBrowserE2ETests(unittest.TestCase):
    """起一个隔离实例 + 无头 Chrome，验证壳偏好的读、写与持久化。"""

    driver = None
    instance = None

    @classmethod
    def setUpClass(cls):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        # `OMNIBOX_HOME` 隔离：壳偏好会写进 `<user_data>/.config/shell.json`，
        # 不隔离就会把开发机上的真实偏好改掉。
        cls.instance = AppInstance('prefs', root).start()

        options = Options()
        binary = browser_binary()
        if binary:
            options.binary_location = binary
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1280,800')
        options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
        try:
            cls.driver = webdriver.Chrome(options=options)
        except Exception as exc:  # pragma: no cover - 取决于本机驱动
            cls._cleanup()
            raise unittest.SkipTest(f'无法启动 WebDriver（{type(exc).__name__}: {exc}）') from exc
        cls.driver.set_page_load_timeout(30)

        # 打开首页时壳会种下 HttpOnly 的令牌 Cookie，页面内的 fetch 自动带凭据
        cls.driver.get(cls.instance.base_url)
        if not cls._wait_for(lambda: bool(cls._js(
                "return document.querySelectorAll('.nav-item').length > 0;")), STARTUP_TIMEOUT):
            cls._cleanup()
            raise unittest.SkipTest('壳前端未在超时内就绪')
        time.sleep(1.0)

    @classmethod
    def _cleanup(cls):
        if cls.driver is not None:
            try:
                cls.driver.quit()
            except Exception:
                pass
            cls.driver = None
        if cls.instance is not None:
            cls.instance.stop()
            cls.instance = None
        if getattr(cls, '_tmp', None) is not None:
            cls._tmp.cleanup()
            cls._tmp = None

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    # ------------------------------------------------------------------ 工具

    @classmethod
    def _js(cls, script: str, *args):
        return cls.driver.execute_script(script, *args)

    @classmethod
    def _wait_for(cls, predicate, timeout: float = UI_TIMEOUT) -> bool:
        return bool(wait_until(predicate, timeout, interval=0.2))

    def _wait_for_saved(self, predicate, timeout: float = FLUSH_WAIT) -> dict:
        """等一次落盘：去抖 300ms + 请求往返。"""
        wait_until(lambda: predicate(_saved_prefs(self.instance)), timeout, interval=0.2)
        return _saved_prefs(self.instance)

    def _call(self, patch) -> dict:
        """页面内调偏好写接口（页面 Cookie 就是令牌）。"""
        result = self.driver.execute_async_script("""
            const patch = arguments[0]; const done = arguments[arguments.length - 1];
            fetch('/api/system_set_shell_prefs', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ args: [patch] }),
            }).then(r => r.json()).then(j => done(j)).catch(e => done({ error: String(e) }));
        """, patch)
        self.assertNotIn('error', result or {}, f'写偏好失败: {result}')
        self.assertTrue((result or {}).get('result', {}).get('success'), f'写偏好失败: {result}')
        return result['result']

    # ------------------------------------------------------------------ 注入

    def test_injected_state_reaches_the_document(self):
        """注入的状态必须真的到了页面（壳页面唯一的数据源）。"""
        state = self._js("return window.__OMNIBOX_SHELL_STATE__ || null;")
        self.assertIsInstance(state, dict, 'index.html 里没有注入 __OMNIBOX_SHELL_STATE__')
        self.assertIn('prefs', state)
        self.assertIn('theme', state['prefs'])
        self.assertIn('stored', state, '注入里缺少"盘上有没有偏好段"的标记')

    def test_first_paint_uses_the_injected_theme(self):
        """首帧的外观来自注入：html 上的 data-theme 必须已经是偏好里的值。"""
        self.assertEqual(
            self._js("return document.documentElement.getAttribute('data-theme');"), 'dark')

    def test_startup_falls_back_to_the_first_plugin(self):
        """默认偏好是「上次离开的页面」，但这是空白实例：应落在第一个插件上。"""
        routes = self._js(
            "return Array.from(document.querySelectorAll('.nav-item'))"
            ".map(e => e.textContent.trim()).filter(t => !t.includes('设置'));")
        self.assertTrue(routes, f'导航里没有插件: {routes}')
        first = self.instance.system('system_get_plugins')[0]['route']
        self.assertEqual(self._js("return location.pathname;"), first,
                         '首屏没有落在第一个插件的路由上')

    # ------------------------------------------------------------------ 写路径

    def test_color_write_reaches_shell_json(self):
        self._call({'colors': {'--accent-harness': '#123456'}})
        saved = self._wait_for_saved(lambda p: p.get('colors', {}).get('--accent-harness'))
        self.assertEqual(saved['colors']['--accent-harness'], '#123456')

    def test_theme_write_leaves_the_previous_values_intact(self):
        """局部更新：写主题不能把刚写过的颜色键冲掉。"""
        self._call({'theme': 'light'})
        saved = self._wait_for_saved(lambda p: p.get('theme') == 'light')
        self.assertEqual(saved['theme'], 'light')
        self.assertEqual(saved['colors']['--accent-harness'], '#123456')

    def test_unknown_variable_is_dropped_and_others_kept(self):
        """键级校验：一个坏键只丢它自己，同一次写入里的好键照常生效。"""
        result = self._call({'colors': {'--ok-var': '12px', 'bad-name': '12px'}})
        self.assertEqual(result['prefs']['colors'].get('--ok-var'), '12px')
        self.assertNotIn('bad-name', result['prefs']['colors'])

    # ------------------------------------------------------------------ 重启

    def test_prefs_survive_a_restart(self):
        """核心回归：重启后读到的仍是用户改过的值（原缺陷就是这里丢的）。

        本用例自成一体（不依赖同类的其它用例先跑过）：先自己写一遍主题与变量，
        确认**盘上**已经是新值，再重启。
        """
        self._call({'theme': 'light', 'colors': {'--restart-probe': '200px'}})
        saved = self._wait_for_saved(
            lambda p: p.get('colors', {}).get('--restart-probe'), timeout=FLUSH_WAIT)
        self.assertEqual(saved.get('theme'), 'light', f'重启前盘上不是 light: {saved}')
        self.instance.restart()
        after = _saved_prefs(self.instance)
        self.assertEqual(after.get('theme'), 'light', f'重启后盘上的主题变了: {after}')
        prefs = self.instance.system('system_get_shell_prefs')
        self.assertEqual(prefs['colors']['--restart-probe'], '200px')
        self.assertEqual(prefs['theme'], 'light')

    def test_injected_page_after_restart_carries_the_stored_prefs(self):
        """重启后的首页注入里必须带上落盘的偏好（首帧就直接正确，不闪）。

        自己先把盘上的值写成已知状态，再看注入 —— 与 `test_prefs_survive_a_restart`
        一样不依赖同类的其它用例。
        """
        self._call({'theme': 'light', 'colors': {'--restart-probe': '200px'}})
        self._wait_for_saved(lambda p: p.get('theme') == 'light')
        self.instance.restart()
        self.driver.get(self.instance.base_url)
        self.assertTrue(self._wait_for(lambda: bool(self._js(
            "return document.querySelectorAll('.nav-item').length > 0;")), STARTUP_TIMEOUT))
        state = self._js("return window.__OMNIBOX_SHELL_STATE__;")
        self.assertIs(state.get('stored'), True, '重启后盘上应当已有偏好段')
        self.assertEqual(state['prefs']['theme'], 'light')
        self.assertEqual(state['prefs']['colors']['--restart-probe'], '200px')
        self.assertEqual(
            self._js("return document.documentElement.getAttribute('data-theme');"), 'light',
            '首帧没有用注入的主题')

    def test_no_console_errors_on_shell_load(self):
        """偏好搬家后壳的加载不应产生任何 console.error / 未捕获异常。"""
        logs = [entry for entry in self.driver.get_log('browser')
                if entry.get('level') in ('SEVERE', 'ERROR')]
        self.assertEqual([entry.get('message', '')[:300] for entry in logs], [],
                         '壳前端加载时报错（真实浏览器控制台）')


@unittest.skipIf(SKIP_REASON, SKIP_REASON or '')
class ShellPrefsExitTests(unittest.TestCase):
    """「上次离开的页面」只记安全退出 —— 单独一套实例，因为它要把实例停掉。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.instance = AppInstance('exit', Path(self._tmp.name)).start()
        self.addCleanup(lambda: self.instance.stop() if self.instance.alive else None)

    def test_last_route_is_not_written_before_a_safe_exit(self):
        """上报的路由只进内存：写盘要等安全退出（或空闲兜底）。

        这条断言的是**语义**，不是"提交一定发生了" —— 桌面模式被强杀、浏览器模式被
        强制结束进程都不算安全退出，那种场景下文件里必须保留上一次安全退出的值。
        """
        self.instance.system('system_set_shell_prefs', {'last_route': '/settings'})
        self.assertEqual(self.instance.system('system_get_shell_prefs')['last_route'],
                         '/settings', '同一进程内应当立刻读得到刚上报的页面')
        self.assertNotEqual(_saved_prefs(self.instance).get('last_route'), '/settings',
                            '上报的路由不该立刻落盘')

    def test_idle_settle_writes_the_route_without_an_exit(self):
        """浏览器访问没有关窗事件：空闲兜底也要把 `last_route` 写下来。

        兜底窗口是 20 秒（`shell_prefs.IDLE_COMMIT_SECONDS`），这里按那段时间等待，
        不去改生产常量 —— 用例改常量就等于没测这条路径。
        """
        self.instance.system('system_set_shell_prefs', {'last_route': '/settings'})
        committed = wait_until(
            lambda: _saved_prefs(self.instance).get('last_route') == '/settings',
            timeout=40.0, interval=0.5)
        self.assertTrue(committed, '空闲兜底没有提交 last_route')

    def test_invalid_patch_is_rejected_as_a_whole(self):
        """非对象补丁整条拒绝：写接口不能把 shell.json 改成非对象内容。"""
        result = self.instance.system('system_set_shell_prefs', '不是对象')
        self.assertFalse(result['success'])
        self.assertIn('JSON', result['error'])
        saved = _saved_prefs(self.instance)
        self.assertIsInstance(saved, dict, '被拒绝的写入不该改出非对象的偏好段')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
