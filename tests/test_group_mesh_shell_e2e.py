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

数据根是一次性的（`main.py --data-root <临时目录>`）：本文件不依赖开发机上是否
注册过身份与团体，需要前提的用例自己用 `init_identity` / `create_group` 建。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

STARTUP_TIMEOUT = 40.0


def text_for(driver, element_id: str) -> str:
    """按 id 取文本，走 JS `innerText` 而不是 Selenium 的 `.text`。

    原因见 `tests/test_group_mesh_frontend_e2e.py` 的 `text_of()`：插件的卡片/按钮带
    入场动画（effects.css 的 obxFadeUp + 交错延迟），Selenium 的 getText 原子会对
    已渲染的元素**间歇性**返回空串，失败信息会把排查引向"没渲染"这个错误方向。
    """
    return driver.execute_script(
        'var n = document.getElementById(arguments[0]);'
        'return n ? (n.innerText || n.textContent || "").trim() : "";', element_id)


def _have_selenium() -> bool:
    try:
        import selenium  # noqa: F401
        from selenium import webdriver  # noqa: F401
    except Exception:
        return False
    return True


def _have_browser() -> bool:
    """有没有**能驱动 ChromeDriver**的浏览器（不算 Edge，理由见
    `tests/harness/browser_binary.py`）。"""
    from tests.harness.browser_binary import browser_binary

    return browser_binary() is not None


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

        from tests.harness.browser_binary import browser_binary

        cls.port = _free_port()
        cls.base_url = f'http://127.0.0.1:{cls.port}'
        # 一次性数据根：原来直接吃仓库的 `data/`，于是"本机已注册过身份与团体"变成了
        # 隐式前提 —— 全新 checkout（或清过 data/）上 `my_endpoint` 返回「尚未创建身份」、
        # `list_peers` 返回「需要先创建身份与团体」，用例必红。改用临时根后前提由用例
        # 自己建（见 `_ensure_identity()`），也不会往开发机的 data/ 里写东西。
        cls._data_tmp = tempfile.TemporaryDirectory()
        cls.server = subprocess.Popen(
            [sys.executable, 'main.py', '--web-only', '--port', str(cls.port),
             '--data-root', cls._data_tmp.name],
            cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_health(cls.base_url):
            cls.server.terminate()
            raise unittest.SkipTest('服务未在超时内就绪')

        options = webdriver.ChromeOptions()
        # 显式指到 Chrome：本机只有 Edge 时，webdriver.Chrome 会因为配不到 ChromeDriver
        # 而失败（`unrecognized Chrome version: Edg/...`），那样用例就永远起不来。
        binary = browser_binary()
        if binary:
            options.binary_location = binary
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
        if getattr(cls, '_data_tmp', None) is not None:
            cls._data_tmp.cleanup()
            cls._data_tmp = None

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
        """点导航进入团体组网，切换到**它自己**那个 iframe。

        不能用 `find_elements(TAG_NAME, 'iframe')[0]`：插件 iframe 常驻，且壳启动就落在
        `plugins[0].route`（`shell/frontend/src/App.vue`），于是最先出现的 iframe 可能是
        别的插件 —— 按目录名排序，`document-reader` 正好排在 `group-mesh` 之前。
        壳给每个 iframe 标了 `data-plugin-name`，按它取才不依赖排序。
        """
        from selenium.webdriver.common.by import By

        nav = next((e for e in self.driver.find_elements(By.CSS_SELECTOR, '.nav-item')
                    if '团体组网' in e.text), None)
        self.assertIsNotNone(nav, '导航里应当有「团体组网」入口')
        nav.click()
        frame_css = 'iframe[data-plugin-name="group-mesh"]'
        self.assertTrue(self._wait_for(lambda: len(self.driver.find_elements(By.CSS_SELECTOR, frame_css)) > 0),
                        '进入插件后应当出现它自己的 iframe')
        frame = self.driver.find_element(By.CSS_SELECTOR, frame_css)
        self.driver.switch_to.frame(frame)
        self.assertTrue(self._wait_for(lambda: len(self.driver.find_elements(
            By.CSS_SELECTOR, '#identity-actions .btn, #roster-actions .btn')) > 0),
            '插件应当渲染出动作按钮（说明 Bridge 与后端调用都是通的）')
        return frame

    def _api_call(self, method: str, payload: dict) -> dict:
        """从**壳页面**同源调插件后端（令牌 Cookie 由壳种下，fetch 自动携带）。

        刻意不经过插件 iframe 里的 Bridge：前提必须在 iframe 第一次加载**之前**
        建好 —— `remote.js` 只在 init 里调一次 `refreshMyEndpoint()`，之后再切面板
        只是显隐切换、不会重新取数，身份后建的话 `#my-endpoint` 会一直停在
        「尚未创建身份」。插件后端在壳启动时就已加载（`manager.load_all()`），
        因此不点导航也能调它。
        """
        self.driver.switch_to.default_content()
        raw = self.driver.execute_async_script(
            "var done = arguments[arguments.length - 1];"
            # 壳的 /api 约定（file_server.py 的 api 处理器）：请求体是 `{args, kwargs}`，
            # 响应体是 `{result: 插件返回值}`，失败时是 `{error: ...}` + 500。
            "fetch('/api/group-mesh__' + arguments[0], {method: 'POST',"
            "  headers: {'Content-Type': 'application/json'},"
            "  body: JSON.stringify({kwargs: arguments[1] || {}})})"
            ".then(function (r) { return r.text(); })"
            ".then(done, function (e) { done(JSON.stringify({threw: String(e)})); });",
            method, payload)
        envelope = json.loads(raw)
        self.assertNotIn('threw', envelope, f'{method} 调用抛出异常：{envelope}')
        self.assertNotIn('error', envelope, f'{method} 返回错误：{envelope}')
        return envelope.get('result') or {}

    def _ensure_identity(self) -> None:
        """建出「已创建身份」这个前提：一次性数据根里原本是空的。"""
        if (self._api_call('get_status', {}).get('identity') or {}):
            return
        result = self._api_call('init_identity', {'name': 'shell-e2e'})
        self.assertTrue(result.get('success'), f'创建身份失败：{result}')
        self._reload_shell()

    def _reload_shell(self) -> None:
        """重新载入壳页面，丢弃已经常驻的插件 iframe。

        插件 iframe 一旦建出来就不再走 init，而 `remote.js` 只在 init 里调一次
        `refreshMyEndpoint()`。按字母序排在 `test_my_endpoint_...` 之前的
        `test_create_or_join_entries_are_reachable` 已经把 iframe 加载过一次，
        那时身份还不存在 —— 不重载的话 `#my-endpoint` 会一直停在「尚未创建身份」。
        """
        from selenium.webdriver.common.by import By

        self.driver.get(self.base_url)
        self.assertTrue(self._wait_for(
            lambda: len(self.driver.find_elements(By.CSS_SELECTOR, '.nav-item')) > 0),
            '壳页面重新载入后导航未出现')

    def _ensure_group(self) -> None:
        """建出「身份 + 团体」这个前提。

        `list_peers` 的门槛是 `identity is None or roster is None`（peers.py），
        只有身份还不够，必须同时有名单。
        """
        if (self._api_call('get_status', {}).get('roster') or {}):
            return
        self._ensure_identity()
        result = self._api_call('create_group', {'group': 'shell-e2e'})
        self.assertTrue(result.get('success'), f'创建团体失败：{result}')

    def tearDown(self):
        try:
            self.driver.switch_to.default_content()
        except Exception:
            pass

    def test_plugin_loads_inside_shell_with_working_bridge(self):
        self._enter_plugin()
        self.assertEqual(self.driver.execute_script('return typeof window.Bridge'), 'object')
        # 真实调用一次后端：验证 <plugin>__<method> 前缀与令牌链路都通
        result = self.driver.execute_script(
            "return window.Bridge.call('get_status')"
            ".then(function (r) { return r && r.kernel ? 'OK' : 'BAD'; })"
            ".catch(function (e) { return 'ERROR:' + e; });")
        self.assertEqual(result, 'OK', f'真实壳里调用插件后端失败: {result}')
        self.assertTrue(self._wait_for(lambda: bool(text_for(self.driver, 'identity-body'))),
                        '身份卡片不应是空白')

    def test_no_modal_overlays_the_plugin_on_open(self):
        """回归：弹窗曾因 CSS 覆盖 `hidden` 而一打开就同时铺满整屏。

        弹窗容器已迁到壳的 `.modal`（base.css），因此这里按壳的选择器断言；
        同时校验用的是壳的模态框结构，避免有人加回一个自绘容器。
        """
        from selenium.webdriver.common.by import By
        self._enter_plugin()
        visible = [m.get_attribute('id') for m in
                   self.driver.find_elements(By.CSS_SELECTOR, '.modal') if m.is_displayed()]
        self.assertEqual(visible, [], f'打开插件时不应有可见弹窗，实际: {visible}')
        # 每个弹窗都必须是壳的 .modal-box 结构（而不是插件自绘的容器）
        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '.modal .modal-box'))
        self.assertTrue(self.driver.find_elements(By.CSS_SELECTOR, '.modal .modal-footer'))

    def test_create_or_join_entries_are_reachable(self):
        """全新安装必须能看到创建/加入团体的入口，不能被弹窗盖住。"""
        self._enter_plugin()
        # 动作区是**按状态异步渲染**的：先等出按钮再断言文案，
        # 否则会在 get_status 还没回来的空档里读到空字符串。
        self.assertTrue(self._wait_for(
            lambda: '创建团体' in text_for(self.driver, 'roster-actions')
            or '显示邀请串' in text_for(self.driver, 'roster-actions')),
            '动作区应当渲染出团体相关入口')
        actions = text_for(self.driver, 'roster-actions')
        roster = text_for(self.driver, 'roster-body')
        if '还没有团体名单' in roster:
            self.assertIn('创建团体', actions)
            # 入口同时承担"更新名单"，因此标签是"加入 / 更新团体"
            self.assertIn('加入 / 更新团体', actions)
            self.assertNotIn('添加成员', actions)
        else:
            # 已有团体：入口换成邀请串/添加成员，至少"显示邀请串"必须在
            self.assertIn('显示邀请串', actions)


    # ── 真实网络调用：不能把界面卡在"正在读取设备" ──────────────────────────

    def test_peers_refresh_returns_quickly_with_real_endpoints(self):
        """真实壳 + 真实网络下调用 list_peers，必须在几秒内返回。

        诚实说明它的判别力：这条用例先自建身份与团体（一次性数据根里本来没有），
        注册表里**只有本机自己**的记录，而本机不作为"对端"被探测 —— 因此候选端点为
        0，这条断言在"退化实现"下也可能通过（我把探测改回串行 20 秒超时跑过一次，
        它照样绿）。它真正守的是"接口在真实壳里不会异常/不返回"这类问题。

        对"不可达端点会不会把刷新拖到几十秒"的确定性守卫在插件层：
        `tests/test_group_mesh_plugin.py::RemoteApiTest.test_probe_with_dead_endpoint_
        respects_overall_budget`，那里会真的放一条一定连不上的端点并卡时间。
        """
        self._ensure_group()
        self._enter_plugin()
        script = (
            "var t0 = Date.now();"
            "return window.Bridge.call('list_peers', {refresh: true})"
            ".then(function (r) { return JSON.stringify({ms: Date.now() - t0,"
            " ok: !!(r && r.success), peers: (r && r.peers || []).length,"
            # 后端给的失败原因必须带进断言消息：只留 ok/peers 的话，失败信息长成
            # `{'ms': 5, 'ok': False, 'peers': 0}`，看着像"接口挂了/太慢"，
            # 真正的原因（没有身份、没有团体）反而被丢掉。
            " error: (r && r.error) || ''}); })"
            ".catch(function (e) { return JSON.stringify({ms: Date.now() - t0, threw: String(e)}); });"
        )
        raw = self.driver.execute_async_script(
            "var done = arguments[arguments.length - 1];"
            "Promise.resolve().then(function () { return (function () {" + script +
            "})(); }).then(done, function (e) { done(JSON.stringify({threw: String(e)})); });")
        payload = json.loads(raw)
        self.assertNotIn('threw', payload, f'list_peers 调用抛出异常: {payload}')
        self.assertTrue(payload.get('ok'), payload)
        self.assertLess(payload['ms'], 15000,
                        f"list_peers 耗时 {payload['ms']}ms，界面会一直停在「正在读取设备」")

    def test_my_endpoint_is_rendered_in_the_shell(self):
        """「我的地址」在真实壳里必须能渲染出来（它是地址交换的入口）。"""
        self._ensure_identity()
        self._enter_plugin()
        # 「我的地址」在「远端共享」面板里：面板是纯显隐切换，先切过去再读
        self.driver.execute_script(
            "document.querySelector('#gm-nav .gm-nav-item[data-panel=\"remote\"]').click();")
        self.assertTrue(self._wait_for(
            lambda: '节点未运行' in text_for(self.driver, 'my-endpoint')
            or ':' in text_for(self.driver, 'my-endpoint')
            or '尚未发布' in text_for(self.driver, 'my-endpoint')),
            '「我的地址」应当渲染出地址或明确的未运行提示，实际：'
            f'{text_for(self.driver, "my-endpoint")!r}')


if __name__ == '__main__':
    unittest.main()
