"""壳前端的真实浏览器端到端用例（本地手动运行，不进 CI）。

为什么需要它
------------
Python 单测与 Node 桩都测不到一类缺陷：**界面在真实浏览器里静默不更新**。
实证过一次代价很高的回归（见 docs/core-contract-fixes.md 与本次提交）：

  - 接口在浏览器里正常返回 4 个插件（正文、status、cookie 全部正常）；
  - 但导航只渲染"暂无插件"，且全过程**没有任何报错**（不抛异常、控制台为空）；
  - 根因是插件列表不是响应式数据，computed 在数据到达前求值并把空数组永久缓存。

这种缺陷用 unittest / vm 桩都发现不了，只有真实浏览器 + 真实构建产物能抓到，
所以本用例直接驱动 Chrome 打开 `python main.py --web-only` 起的服务，断言
「接口数据 → 导航渲染 → iframe 挂载 → 生命周期通知送达」这条完整链路。

为什么不在 CI 里跑（按项目约定）
--------------------------------
这类问题在本地打开应用一眼可见，属于本地自测范围；CI 上跑真实浏览器既慢又脆弱，
且每台 runner 都要下载 ChromeDriver。因此 selenium 只声明在 `requirements-e2e.txt`
（CI 安装的是 `requirements-dev.txt`），未安装时本用例自动跳过，不会拖红 CI。

本地运行
--------
    venv/Scripts/pip install -r requirements-e2e.txt
    venv/Scripts/python -m unittest tests.test_shell_browser_e2e -v

依赖：本机已安装 Chrome 或 Edge，以及 selenium 4.6+（自带 driver 管理，自动下载驱动）。
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 断言用的等待上限：CI 之外的机器冷启动（含首次下载 driver）可能偏慢
STARTUP_TIMEOUT = 40.0
PAGE_SETTLE_SECONDS = 2.0


def _have_selenium() -> bool:
    try:
        import selenium  # noqa: F401
    except Exception:
        return False
    return True


def _have_browser() -> bool:
    """Chrome / Edge 是否存在（selenium 4.6+ 会自己下载匹配的 driver）。"""
    for exe in ('chrome', 'chrome.exe', 'msedge', 'msedge.exe', 'chromium', 'chromium-browser'):
        if shutil.which(exe):
            return True
    for candidate in (
        Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
        Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
    ):
        if candidate.is_file():
            return True
    return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _wait_health(base_url: str, timeout: float = STARTUP_TIMEOUT) -> bool:
    """等服务起来：连不上/超时都属正常，继续轮询（不能裸 except，否则吞掉 Ctrl+C）。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f'{base_url}/health', timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


@unittest.skipUnless(
    _have_selenium(),
    '未安装 selenium（本地端到端用例）：pip install -r requirements-e2e.txt',
)
@unittest.skipUnless(_have_browser(), '未检测到 Chrome/Edge，跳过真实浏览器端到端用例')
class ShellBrowserE2ETests(unittest.TestCase):
    """启动真实服务 + 无头 Chrome，核对壳前端的完整链路。"""

    driver = None
    server = None
    base_url = ''

    @classmethod
    def setUpClass(cls):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        cls.port = _free_port()
        cls.base_url = f'http://127.0.0.1:{cls.port}'
        cls.server = subprocess.Popen(
            [sys.executable, 'main.py', '--web-only', '--port', str(cls.port)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_health(cls.base_url):
            cls.server.terminate()
            cls.server = None
            raise unittest.SkipTest('服务未在超时内就绪，跳过浏览器端到端用例')

        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1400,900')
        options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
        try:
            cls.driver = webdriver.Chrome(options=options)
        except Exception as exc:   # 驱动下载失败 / 浏览器不匹配：跳过而不是判失败
            cls.server.terminate()
            cls.server = None
            raise unittest.SkipTest(f'无法启动 WebDriver（{type(exc).__name__}: {exc}）') from exc
        cls.driver.set_page_load_timeout(30)

        cls.driver.get(cls.base_url)
        cls._wait_for(lambda: bool(cls.driver.execute_script(
            "return document.querySelectorAll('.nav-item').length > 0;"
        )))
        time.sleep(PAGE_SETTLE_SECONDS)

    @classmethod
    def tearDownClass(cls):
        if cls.driver is not None:
            try:
                cls.driver.quit()
            except Exception:
                pass
            cls.driver = None
        if cls.server is not None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.server.kill()
            cls.server = None

    @classmethod
    def _wait_for(cls, predicate, timeout: float = 15.0) -> bool:
        """轮询等待条件成立（真实浏览器渲染是异步的，不能用固定 sleep 硬等）。"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    # ------------------------------------------------------------------ 工具

    def _nav_items(self) -> list[str]:
        return self.driver.execute_script(
            "return Array.from(document.querySelectorAll('.nav-item')).map(e => e.textContent.trim());"
        )

    def _plugin_nav_items(self) -> list[str]:
        """排除"设置"后的插件导航项。"""
        return [text for text in self._nav_items() if '设置' not in text]

    def _hints(self) -> list[str]:
        return self.driver.execute_script(
            "return Array.from(document.querySelectorAll('.hint,.error,.loading'))"
            ".map(e => e.textContent.trim());"
        )

    def _api_plugin_names(self) -> list[str]:
        """同步 XHR（不走异步脚本，避免挂起）取接口的真实正文。"""
        result = self.driver.execute_script("""
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/system_get_plugins', false);
            xhr.setRequestHeader('Content-Type', 'application/json');
            let err = null;
            try { xhr.send(JSON.stringify({ args: [] })); } catch (e) { err = String(e); }
            let names = null;
            try {
                const parsed = JSON.parse(xhr.responseText);
                names = (parsed.result || []).map(p => p.name);
            } catch (e) { names = null; }
            return { error: err, status: xhr.status, names: names };
        """)
        self.assertIsNone(result.get('error'), f'页面内请求接口失败: {result}')
        self.assertEqual(result.get('status'), 200, f'接口状态异常: {result}')
        self.assertIsInstance(result.get('names'), list, f'接口正文无法解析: {result}')
        return result['names'] or []

    def _click_nav(self, text: str) -> bool:
        return bool(self.driver.execute_script("""
            const target = arguments[0];
            const items = Array.from(document.querySelectorAll('.nav-item'));
            const hit = items.find(e => e.textContent.includes(target));
            if (hit) { hit.click(); return true; }
            return false;
        """, text))

    def _plugin_frame_state(self) -> dict:
        """读取当前插件 iframe 内的 PluginLifecycle.state。"""
        return self.driver.execute_script("""
            const frame = document.querySelector('iframe');
            if (!frame) return { error: 'no-iframe' };
            try {
                const win = frame.contentWindow;
                if (!win.PluginLifecycle) return { error: 'no-lifecycle' };
                return { state: JSON.parse(JSON.stringify(win.PluginLifecycle.state)) };
            } catch (e) { return { error: String(e) }; }
        """)

    # ------------------------------------------------------------------ 用例

    def test_no_console_errors_on_shell_load(self):
        """壳加载不应产生任何 console.error / 未捕获异常。"""
        logs = [entry for entry in self.driver.get_log('browser')
                if entry.get('level') in ('SEVERE', 'ERROR')]
        self.assertEqual(
            [entry.get('message', '')[:300] for entry in logs], [],
            '壳前端加载时报错（真实浏览器控制台）',
        )

    def test_api_returns_visible_plugins(self):
        """回归 1：接口必须返回可见插件（hidden 插件不在此列，由后端过滤）。"""
        names = self._api_plugin_names()
        self.assertTrue(names, 'system_get_plugins 返回空列表：前端拿不到任何插件')
        self.assertIn('image-viewer', names, f'缺少 image-viewer: {names}')

    def test_navigation_lists_plugins(self):
        """回归 2（本次缺陷的正面断言）：导航必须把插件渲染出来。

        缺陷表现是 nav 只剩「设置」、提示「暂无插件」，而接口数据完全正常 ——
        所以这条断言必须同时断言"接口有数据"与"界面渲染出来"。
        """
        self.assertTrue(self._api_plugin_names(), '前置条件失败：接口未返回插件')
        plugin_items = self._plugin_nav_items()
        self.assertTrue(
            plugin_items,
            f'接口有插件但导航未渲染：nav={self._nav_items()} hint={self._hints()}',
        )

    def test_navigation_does_not_show_empty_hint(self):
        """导航渲染出插件后，不得同时显示"暂无插件"。"""
        self.assertTrue(self._plugin_nav_items(), '未渲染插件，前置条件不成立')
        self.assertNotIn('暂无插件', self._hints(), f'提示区异常: {self._hints()}')

    def test_active_plugin_iframe_is_mounted(self):
        """回归 3：当前插件必须真的挂载出 iframe，并指向插件入口。"""
        self.assertTrue(
            self._wait_for(lambda: bool(self.driver.execute_script(
                "return document.querySelectorAll('.plugin-frame-container iframe').length;"))),
            '插件路由下没有挂载任何 iframe',
        )
        srcs = self.driver.execute_script(
            "return Array.from(document.querySelectorAll('iframe')).map(e => e.getAttribute('src'));"
        )
        self.assertTrue(srcs, f'iframe src 为空: {srcs}')
        self.assertTrue(
            any('/plugins/' in (src or '') for src in srcs),
            f'iframe 未指向插件入口: {srcs}',
        )

    def test_lifecycle_notifications_reach_plugin_frame(self):
        """回归 4：生命周期通知必须真的送达插件（第 3 项在前端侧的验收）。

        切到设置页 → 插件应收到 hidden；切回插件 → 应收到 shown。
        这是"插件在后台停止轮询 / 定时器"能力的前提，只在真实 iframe 里可验证。
        """
        self.assertTrue(
            self._wait_for(lambda: self._plugin_frame_state().get('state') is not None),
            f'插件 iframe 内没有 PluginLifecycle: {self._plugin_frame_state()}',
        )
        initial = self._plugin_frame_state().get('state') or {}
        self.assertTrue(initial.get('visible'), f'首个插件的初始状态应为可见: {initial}')

        self.assertTrue(self._click_nav('设置'), '找不到「设置」导航项')
        self.assertTrue(
            self._wait_for(lambda: not (self._plugin_frame_state().get('state') or {}).get('visible', True)),
            f'切到设置后插件未收到 hidden: {self._plugin_frame_state()}',
        )
        hidden = self._plugin_frame_state().get('state') or {}
        self.assertTrue(hidden.get('hideSeen'), f'hidden 未被记录: {hidden}')
        self.assertFalse(hidden.get('visible'), f'状态仍为可见: {hidden}')

        first_plugin_label = self._nav_items()[0]
        self.assertTrue(self._click_nav(first_plugin_label), '切回插件失败')
        self.assertTrue(
            self._wait_for(lambda: (self._plugin_frame_state().get('state') or {}).get('visible', False)),
            f'切回插件后未收到 shown: {self._plugin_frame_state()}',
        )

    def test_plugin_nav_count_matches_api(self):
        """正面断言：导航渲染出的插件数量必须与接口返回的可见插件数一致。

        这条最直接地锁住本次缺陷——接口 4 个、界面 0 个，过去能一路静默通过。
        """
        names = self._api_plugin_names()
        self.assertEqual(
            len(self._plugin_nav_items()), len(names),
            f'导航插件数与接口不一致：nav={self._nav_items()} api={names} hint={self._hints()}',
        )


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
