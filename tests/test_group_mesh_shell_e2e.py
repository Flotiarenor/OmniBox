"""group-mesh 插件在**真实 Shell 里**的端到端用例（本地手动运行，不进 CI）。

与 `test_group_mesh_frontend_e2e.py` 的分工
-------------------------------------------
那份用**桩 Bridge** 直接加载 `frontend/index.html`，验的是插件自身的渲染与交互；
这份起**真实 Shell 服务**、经导航点进插件 iframe，验的是"装进壳里到底能不能用"。

两者都不能省，因为桩 Bridge 会掩盖真实环境的三类问题：

  1. **鉴权**：`/api/*` 需要 `X-Omnibox-Token`。不带令牌时壳连插件列表都拉不到，
     导航与 iframe 都不会出现 —— 表现为"页面一片空白"，而桩测试完全看不到。
  2. **`Bridge` 的真实来源**：browser 模式下 `core/bridge.ts` 会安装
     `window.pywebview.api` 的 HTTP 垫片；直接打开插件 index.html 是拿不到的
     （会一直报 "PyWebView API 不可用"）。
  3. **插件名前缀**：壳调用插件 API 时用的是 `<plugin>__<method>`，
     桩 Bridge 绕过了这一层。

本地运行（需要 `requirements-e2e.txt`，且本机有 Chrome/Edge）
--------------------------------------------------------------
    venv/Scripts/python -m unittest tests.test_group_mesh_shell_e2e -v
"""

from __future__ import annotations

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

STARTUP_TIMEOUT = 40.0


def _have_selenium() -> bool:
    try:
        import selenium  # noqa: F401
        from selenium import webdriver  # noqa: F401
    except Exception:
        return False
    return True


def _have_browser() -> bool:
    import shutil
    for exe in ('chrome', 'chrome.exe', 'msedge', 'msedge.exe', 'chromium'):
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
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f'{base_url}/health', timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


@unittest.skipUnless(_have_selenium(), '未安装 selenium（见 requirements-e2e.txt）')
@unittest.skipUnless(_have_browser(), '本机没有 Chrome / Edge')
class GroupMeshInShellTest(unittest.TestCase):
    """起真实服务，点进导航里的「团体组网」，在真实 iframe 里验证。"""

    @classmethod
    def setUpClass(cls):
        from selenium import webdriver

        cls.port = _free_port()
        cls.base_url = f'http://127.0.0.1:{cls.port}'
        cls.server = subprocess.Popen(
            [sys.executable, 'main.py', '--web-only', '--port', str(cls.port)],
            cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_health(cls.base_url):
            cls.server.terminate()
            raise unittest.SkipTest('服务未在超时内就绪')

        options = webdriver.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--window-size=1400,900')
        try:
            cls.driver = webdriver.Chrome(options=options)
        except Exception as exc:  # pragma: no cover
            cls.server.terminate()
            raise unittest.SkipTest(f'无法启动 WebDriver: {exc}') from exc

        # 不需要手动准备令牌：打开首页时壳会通过 Set-Cookie 种下 HttpOnly 的
        # 令牌 Cookie（见 file_server 的 _attach_token_cookie），同源请求自动携带。
        cls.driver.get(cls.base_url)
        cls._wait_for(lambda: cls.driver.execute_script(
            "return document.querySelectorAll('.nav-item').length > 0;"))
        time.sleep(1.5)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'driver', None) is not None:
            try:
                cls.driver.quit()
            except Exception:
                pass
            cls.driver = None
        if getattr(cls, 'server', None) is not None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.server.kill()
            cls.server = None

    @classmethod
    def _wait_for(cls, predicate, timeout: float = 20.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def _enter_plugin(self):
        """点导航进入团体组网，切换到插件 iframe。"""
        from selenium.webdriver.common.by import By

        nav = next((e for e in self.driver.find_elements(By.CSS_SELECTOR, '.nav-item')
                    if '团体组网' in e.text), None)
        self.assertIsNotNone(nav, '导航里应当有「团体组网」入口')
        nav.click()
        self.assertTrue(self._wait_for(lambda: len(self.driver.find_elements(By.TAG_NAME, 'iframe')) > 0),
                        '进入插件后应当出现 iframe')
        frame = self.driver.find_element(By.TAG_NAME, 'iframe')
        self.driver.switch_to.frame(frame)
        self.assertTrue(self._wait_for(lambda: len(self.driver.find_elements(
            By.CSS_SELECTOR, '#identity-actions .gm-btn, #roster-actions .gm-btn')) > 0),
            '插件应当渲染出动作按钮（说明 Bridge 与后端调用都是通的）')
        return frame

    def tearDown(self):
        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass

    def test_plugin_loads_inside_shell_with_working_bridge(self):
        from selenium.webdriver.common.by import By
        self._enter_plugin()
        self.assertEqual(self.driver.execute_script('return typeof window.Bridge'), 'object')
        # 真实调用一次后端：验证 <plugin>__<method> 前缀与令牌链路都通
        result = self.driver.execute_script(
            "return window.Bridge.call('get_status')"
            ".then(function (r) { return r && r.kernel ? 'OK' : 'BAD'; })"
            ".catch(function (e) { return 'ERROR:' + e; });")
        self.assertEqual(result, 'OK', f'真实壳里调用插件后端失败: {result}')
        self.assertFalse(self.driver.find_element(By.ID, 'identity-body').text.strip() == '',
                         '身份卡片不应是空白')

    def test_no_modal_overlays_the_plugin_on_open(self):
        """回归：三个弹窗曾因 CSS 覆盖 `hidden` 而一打开就同时铺满整屏。"""
        from selenium.webdriver.common.by import By
        self._enter_plugin()
        visible = [m.get_attribute('id') for m in
                   self.driver.find_elements(By.CSS_SELECTOR, '.gm-modal') if m.is_displayed()]
        self.assertEqual(visible, [], f'打开插件时不应有可见弹窗，实际: {visible}')

    def test_create_or_join_entries_are_reachable(self):
        """全新安装必须能看到创建/加入团体的入口，不能被弹窗盖住。"""
        from selenium.webdriver.common.by import By
        self._enter_plugin()
        actions = self.driver.find_element(By.ID, 'roster-actions').text
        roster = self.driver.find_element(By.ID, 'roster-body').text
        if '还没有团体名单' in roster:
            self.assertIn('创建团体', actions)
            self.assertIn('加入团体', actions)
            self.assertNotIn('添加成员', actions)
        else:
            # 已有团体：入口换成邀请串/添加成员，至少"显示邀请串"必须在
            self.assertIn('显示邀请串', actions)


if __name__ == '__main__':
    unittest.main()
