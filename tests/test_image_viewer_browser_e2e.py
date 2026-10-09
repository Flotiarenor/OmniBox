"""图片相册的**真实浏览器端到端性能回归**（本机无 Chrome / selenium 时跳过）。

为什么必须有这个文件：相册的慢有两次都出在"离线复现不出来"的地方 ——

1. 第一次：`list_albums` 走全树 os.walk。零件级探针（`tools/bench_gallery.py`）
   用同一份代码量到 0.1s，真实壳进程里 8s，因为真进程里还有统一刷新基建在抢盘。
2. 第二次：`_build_albums` 对全树每个目录查一次设置（`SettingsStore.get()` 无缓存、
   每次读盘）。离线用假 SettingsStore 量到 0.019s，真实壳进程里 7.4s，并且它算出的
   `pixiv` 标记与索引里的不一致，导致**9000+ 个目录每次全量重扫**（再叠 12.5s）。

两次都只有"起真服务器 + 真浏览器 + 看服务端计时"才能看见。所以本用例断言的是
**端到端预算**，不是某个函数的实现细节。

依赖：selenium 4.6+、本机 Chrome（见 `requirements-e2e.txt`）。缺任一项自动跳过。
运行：

    venv/Scripts/python.exe -m unittest tests.test_image_viewer_browser_e2e -v
"""

import json
import os
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

# 首屏预算：真实图库上实测 0.10-0.16s（索引命中）；这里留足 40 倍余量，只用来拦
# "又退化成读盘 / 重扫全树"这一类量级错误（那会是 8-25s）
FIRST_PAINT_BUDGET_S = 6.0
# 服务端 list_albums 的预算（诊断钩子报的毫秒数）
LIST_ALBUMS_BUDGET_MS = 1500.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _have_selenium() -> bool:
    try:
        import selenium  # noqa: F401
        from selenium import webdriver  # noqa: F401
        return True
    except Exception:
        return False


class _ShellServer:
    """起一个真实壳服务器（`main.py --web-only`），退出时收干净。"""

    def __init__(self):
        self.port = _free_port()
        self.base = f'http://127.0.0.1:{self.port}'
        self.proc = None
        token_file = PROJECT_ROOT / '.config' / 'auth_token.txt'
        self.token = token_file.read_text(encoding='utf-8').strip() if token_file.exists() else ''

    def start(self, timeout: float = 60.0) -> bool:
        env = {**os.environ, 'DSH_PLUGIN_TRACE': '1', 'PYTHONIOENCODING': 'utf-8'}
        self.proc = subprocess.Popen(
            [sys.executable, 'main.py', '--web-only', '--port', str(self.port)],
            cwd=str(PROJECT_ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.base + '/health', timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                time.sleep(0.3)
        return False

    def call(self, method: str, payload=b'[]', timeout: float = 180.0):
        """打一个壳 API（带令牌）；返回 (响应体, 毫秒)。"""
        req = urllib.request.Request(
            f'{self.base}/api/{method}', data=payload,
            headers={'Content-Type': 'application/json',
                     'X-Omnibox-Token': self.token,
                     'Cookie': f'omnibox_token={self.token}'})
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode('utf-8'))
        except Exception as e:                       # 端点缺失 / 未授权都当成"拿不到"
            body = {'error': str(e)}
        return body, (time.perf_counter() - started) * 1000

    def stop(self):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


@unittest.skipUnless(_have_selenium(), '未安装 selenium（本地端到端用例，见 requirements-e2e.txt）')
class ImageViewerBrowserE2ETestCase(unittest.TestCase):
    """起真服务器 + 真浏览器，量"点进图片相册"的端到端耗时。"""

    server: _ShellServer

    @classmethod
    def setUpClass(cls):
        from tests.harness.browser_binary import browser_binary

        cls.server = _ShellServer()
        if not cls.server.start():
            raise unittest.SkipTest('壳服务未在超时内就绪，跳过浏览器端到端用例')
        if not browser_binary():
            cls.server.stop()
            raise unittest.SkipTest('本机没有可被 ChromeDriver 驱动的 Chrome，跳过')

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    # ---------- 工具 ----------

    def _diagnose(self) -> dict:
        body, _ms = self.server.call('image-viewer__diagnose')
        result = body.get('result')
        self.assertIsInstance(result, dict, f'诊断钩子不可用: {body}')
        return result

    def _list_albums(self):
        body, ms = self.server.call('image-viewer__list_albums')
        self.assertNotIn('error', body, f'list_albums 失败: {body}')
        return body.get('result') or {}, ms

    # ---------- 用例 ----------

    def test_list_albums_never_walks_the_tree_when_index_exists(self):
        """有索引时列相册**不许**走全树：走盘次数必须为 0，且单次在预算内。

        这是那次 8-25s 卡顿的直接判据 —— 走盘次数不为 0 就说明又回到了
        "每次进相册页重扫 9000 多个目录"。
        """
        before = self._diagnose()
        self.assertTrue(before.get('dirs_cache_file_exists'),
                        '目录快照文件不存在：先跑一次会让它生成（下面的调用就会生成）')

        _albums, ms = self._list_albums()
        after = self._diagnose()

        walk_delta = (after['album_dirs_walk_count']
                      - before['album_dirs_walk_count'])
        self.assertEqual(
            walk_delta, 0,
            f'列相册时走了 {walk_delta} 次全树（{after["album_dirs_walk_ms"]:.0f}ms）：'
            f'索引可用时不该走盘')
        self.assertLess(
            after['list_albums_last_ms'], LIST_ALBUMS_BUDGET_MS,
            f'list_albums 耗时 {after["list_albums_last_ms"]}ms，超出预算 '
            f'{LIST_ALBUMS_BUDGET_MS}ms；分阶段: {after.get("list_albums_stages_ms")}')

    def test_album_index_rebuild_does_not_rescan_the_whole_library(self):
        """索引重建**不许**全量重扫：稳态下重扫目录数必须远小于总目录数。

        历史事故：`pixiv` 标记的计算方式与索引里的不一致，于是 9000+ 个目录每次
        都被当成"变了"而重扫（12.5s）—— 这一步把它拦下来。首次运行（索引尚未
        由本版本的规则算过）允许全量迁移一次，因此只在"重建过两轮之后"断言。
        """
        self._list_albums()                  # 第 1 轮：允许全量迁移一次
        self._list_albums()
        diag = self._diagnose()
        entries = diag['album_index_entries']
        self.assertGreater(entries, 10, '相册索引条目太少，用例没有意义')
        scanned = float((diag.get('list_albums_stages_ms') or {}).get('build_scan_count', 0))
        self.assertLess(
            scanned, entries * 0.5,
            f'索引重建重扫了 {scanned:.0f} / {entries} 个目录 —— 缓存判定失效了'
            f'（历史事故：pixiv 标记不一致导致每次全量重扫）')

    def _wait(self, driver, script: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if driver.execute_script(script):
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def test_first_screen_renders_in_budget(self):
        """真浏览器里点进"图片相册"，到首屏相册卡片渲染出来的端到端耗时在预算内。"""
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        from tests.harness.browser_binary import browser_binary

        options = Options()
        binary = browser_binary()
        if binary:
            options.binary_location = binary
        # 有头模式：无头会跳过 GPU 合成与光栅化，与真实 pywebview / WebView2 差得远
        options.add_argument('--window-size=1400,900')
        options.add_argument('--window-position=0,0')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        try:
            driver = webdriver.Chrome(options=options)
        except Exception as exc:      # 驱动下载失败 / 浏览器不匹配：跳过而不是判失败
            raise unittest.SkipTest(f'无法启动 WebDriver（{type(exc).__name__}: {exc}）') from exc

        try:
            driver.set_page_load_timeout(60)
            driver.get(self.server.base)
            self._wait(driver, "return document.querySelectorAll('.nav-item').length > 1;", 30)
            clicked = driver.execute_script(
                "var items = Array.from(document.querySelectorAll('.nav-item'));"
                "var hit = items.find(function (e) {"
                "  return (e.textContent || '').indexOf('相册') >= 0; });"
                "if (!hit) return false; hit.click(); return true;")
            self.assertTrue(clicked, '壳里没有"图片相册"导航项')
            started = time.time()
            deadline = started + FIRST_PAINT_BUDGET_S
            painted = False
            while time.time() < deadline:
                for frame in driver.find_elements('tag name', 'iframe'):
                    try:
                        driver.switch_to.frame(frame)
                        if driver.execute_script(
                                "return !!document.querySelector('.iv-album');"):
                            painted = True
                            break
                        driver.switch_to.default_content()
                    except Exception:
                        driver.switch_to.default_content()
                if painted:
                    break
                time.sleep(0.1)
            elapsed = time.time() - started
            self.assertTrue(painted, f'首屏相册卡片在 {FIRST_PAINT_BUDGET_S}s 内没出现')
            self.assertLess(
                elapsed, FIRST_PAINT_BUDGET_S,
                f'首屏渲染用了 {elapsed:.2f}s，超出预算 {FIRST_PAINT_BUDGET_S}s')
        finally:
            driver.quit()


if __name__ == '__main__':
    unittest.main()
