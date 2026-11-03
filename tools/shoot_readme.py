"""README 配图的抓取工具：起一个隔离实例 + 有头 Chrome，按场景表截视口，再转 WebP。

为什么要有它
------------
`docs/images/readme/` 下 11 张配图是 2026-10-09 一次性手工拍的，之后壳与插件的工具栏、
设置页都改过一轮，**11 张全部过期**，而这件事没有任何机制会发现。本工具把"拍图"从
手工动作变成可重跑的动作，并把"这张图应该长什么样"写进场景表（`tools/readme_scenes.json`）：

- **可重跑**：`--home` 指向一份已配置好的用户数据目录（默认 `.build/shoot/home`，由
  `tools/shoot_readme_setup.py` 生成 —— 那个脚本只负责起实例让你手工配置，本脚本只负责拍）。
  每次运行把它复制到临时目录，基线不被改写，跑完即弃 —— 拍图不会污染你自己的 `.config/`。
- **可校验**：每个场景声明**必须出现的文案**（`expect`）。`--check` 只断言、不出图，
  界面一改立刻指出哪一张过期，而不是等人肉眼发现 11 张全过期。
- **一致**：视口、缩放、主题、转换参数都由工具给，不靠"这次记得调成多大"。

拍摄约定（与 `docs/images/README.md` 一致）
------------------------------------------
- **只抓视口**：用 CDP 固定视口（默认 1692×946，与既有配图同尺寸），图里没有浏览器
  地址栏；也因此**没有**桌面窗口的标题栏 —— 这是本工具的有意取舍（既有配图带标题栏）。
- **有头浏览器**：无头会跳过 GPU 合成与光栅化，与 WebView2 差距过大
  （`tests/test_image_viewer_browser_e2e.py` 的模块注释记着这条实测结论）。
- **WebP**：优先用 ffmpeg 的 `libwebp -quality 90 -preset text`（与既有配图同一参数），
  没有 ffmpeg 时退回 Pillow（拿不到 `preset text`，画质略有差异）。

依赖：selenium 与 Chrome 只在本地需要（`requirements-e2e.txt`，刻意不进 CI），
因此脚本里的 selenium 是按需导入的 —— `python tools/check_*.py` 那类门禁不依赖它。

用法：

    python tools/shoot_readme.py --list                  # 看槽位与状态
    python tools/shoot_readme.py --check                 # 只断言文案，不出图
    python tools/shoot_readme.py --only 01-hero          # 拍某一张
    python tools/shoot_readme.py                         # 拍全部（按场景表顺序）
    python tools/shoot_readme.py --blank --only 01-hero  # 不用基线，现场生成最小配置
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENES_FILE = PROJECT_ROOT / 'tools' / 'readme_scenes.json'
PREPARED_HOME = PROJECT_ROOT / '.build' / 'shoot' / 'home'
PNG_CACHE = PROJECT_ROOT / '.build' / 'shoot' / 'png'
IMAGES_DIR = PROJECT_ROOT / 'docs' / 'images' / 'readme'

# 抓图视口：与既有配图同尺寸（见 docs/images/README.md 的拍摄约定）。
VIEWPORT = (1692, 946)
STARTUP_TIMEOUT = 90.0
UI_TIMEOUT = 30.0
POLL = 0.2

INSTALL_HINT = (
    '抓图需要 selenium 与 Chrome。安装：venv/Scripts/pip install -r requirements-e2e.txt'
    '（selenium 刻意不在 requirements-dev 里，CI 不装；理由见该文件抬头）。'
)

# 按文字找控件时的候选集合：壳侧骨架（PluginChrome 的导航项 / 顶栏动作 / 扩展入口）、
# 原生面板里的动作，以及内容页自己的按钮、链接与工具栏按钮。
CLICKABLE = ('button, a, [role="button"], .obx-nav-item, .plugin-nav-item, '
             '.plugin-extension-item, .plugin-actions button, .plugin-leading button, '
             '.native-panel button, .obx-toolbar-btn')

# 复制基线时剔除的东西：WebView2 profile（几十 MB）、日志、访问令牌与主体表。
# 令牌与主体表由实例启动时自己重建，复制过去只会把上一轮的凭据带进新一轮。
EXCLUDE_NAMES = frozenset({'webview', 'logs', 'auth_token.txt', 'principals.json'})


def flatten_text(text: str) -> str:
    """把 DOM 里取到的可见文字压成单行（连续空白 → 一个空格）。

    断言的是"这段文字在画面上看得见"，不是"它在同一个文本节点里"：`innerText` 会在块级
    元素之间插换行，于是"已下载"与"1,466 张"分处两个 span 时就断成两行。不归一化的话，
    断言会因为版式细节（而不是内容）红。
    """
    return ' '.join(str(text or '').split())


class ShootError(RuntimeError):
    """场景没跑成：文案断言没过、浏览器步骤失败、实例没起来。"""


# --------------------------------------------------------------------------- 通用

def ensure_import_path() -> None:
    """把项目根放进 sys.path。

    必须在**函数内**调用：ruff 对 `tools/**` 没有豁免 E402，模块级 import 之前做
    `sys.path.insert` 会被判为"导入不在文件顶部"。`tools/bench_gallery.py` 同一手法。
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def wait_until(predicate: Any, timeout: float, interval: float = POLL) -> bool:
    """轮询直到 `predicate()` 为真；超时返回 False（不抛异常）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def load_scenes(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ShootError(f'找不到场景表：{path}')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('scenes'), list):
        raise ShootError(f'场景表格式不对（顶层应为 {{"scenes": [...]}}）：{path}')
    return payload


def select_scenes(table: Dict[str, Any], only: str) -> List[Dict[str, Any]]:
    scenes: List[Dict[str, Any]] = list(table['scenes'])
    if not only:
        return scenes
    wanted = {item.strip() for item in only.split(',') if item.strip()}
    picked = [scene for scene in scenes if str(scene.get('slot', '')) in wanted]
    missing = wanted - {str(scene.get('slot', '')) for scene in picked}
    if missing:
        raise ShootError(f'场景表里没有这些槽位：{", ".join(sorted(missing))}')
    return picked


# --------------------------------------------------------------------------- 实例

class Server:
    """一个 `--web-only` 实例，用户数据目录指向给定的 home。"""

    def __init__(self, home: Path, port: int, log_path: Path) -> None:
        self.home = home
        self.port = port
        self.log_path = log_path
        self.base_url = f'http://127.0.0.1:{port}'
        self.token = ''
        self._proc: Optional[subprocess.Popen[bytes]] = None

    def start(self) -> 'Server':
        env = {**os.environ, 'OMNIBOX_HOME': str(self.home),
               'PYTHONUNBUFFERED': '1', 'PYTHONIOENCODING': 'utf-8'}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, 'w', encoding='utf-8') as log:
            self._proc = subprocess.Popen(
                [sys.executable, 'main.py', '--web-only', '--port', str(self.port)],
                cwd=str(PROJECT_ROOT), env=env,
                stdout=log, stderr=subprocess.STDOUT)
        if not wait_until(self._healthy, STARTUP_TIMEOUT):
            tail = self._log_tail()
            self.stop()
            raise ShootError(f'实例未在 {STARTUP_TIMEOUT:.0f}s 内就绪（{self.log_path}）\n{tail}')
        self.token = self._read_token()
        return self

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            # Windows 上 `python.exe` 是启动器，它会再起一个真解释器跑 main.py：
            # 只 terminate 启动器会留下孤儿服务占着端口与 SQLite（见
            # tests/harness/app_instance.py 的同类注释）。
            if os.name == 'nt':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                               capture_output=True, check=False)
            else:
                proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()

    def call(self, method: str, *args: Any, timeout: float = 60) -> Any:
        """调 `/api/<method>`（壳方法与插件方法同一张表：`<插件>__<方法>`）。

        `timeout` 可调：某些 setup 是**同步**全库扫描（image-cleaner 的相似分组要逐图算
        dHash），默认 60s 不够。
        """
        ensure_import_path()
        from shell.backend.auth import TOKEN_HEADER
        body = json.dumps({'args': list(args), 'kwargs': {}}, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(
            f'{self.base_url}/api/{method}', data=body, method='POST',
            headers={'Content-Type': 'application/json', TOKEN_HEADER: self.token})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode('utf-8'))
        if isinstance(payload, dict) and payload.get('error'):
            raise ShootError(f'{method} 调用失败：{payload["error"]}')
        return payload.get('result') if isinstance(payload, dict) else payload

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f'{self.base_url}/health', timeout=2) as response:
                return response.status == 200
        except Exception:
            return False

    def _read_token(self) -> str:
        token_file = self.home / '.config' / 'auth_token.txt'
        found = wait_until(lambda: token_file.is_file()
                           and bool(token_file.read_text(encoding='utf-8').strip()), 10.0)
        if not found:
            raise ShootError(f'实例没有生成访问令牌：{token_file}')
        return token_file.read_text(encoding='utf-8').strip()

    def _log_tail(self, lines: int = 25) -> str:
        try:
            content = self.log_path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return '(没有启动日志)'
        return '\n'.join(content.strip().splitlines()[-lines:])


def copy_baseline(source: Path, target: Path) -> None:
    """把基线 home 复制成一份可写的运行副本（剔除 profile、日志与凭据）。

    复制后要**改写配置里指向基线 home 的绝对路径**：`prefs.background.path` 是
    `<config>/backgrounds/<文件名>` 的绝对路径，而 `/file` 只放行当前实例自己的
    `<config>/backgrounds`。不改写的话新实例取背景图得 404 —— 表现是"背景没了、
    玻璃也没了"，而配置里看着一切正常。
    """
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name in EXCLUDE_NAMES:
            continue
        destination = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination,
                            ignore=shutil.ignore_patterns(*sorted(EXCLUDE_NAMES)))
        else:
            shutil.copy2(entry, destination)
    repoint_home_paths(target, source)


def repoint_home_paths(home: Path, old_home: Path) -> None:
    """把 `shell.json` 里指向 `old_home` 的绝对路径改写到 `home`。"""
    shell_file = home / '.config' / 'shell.json'
    if not shell_file.is_file():
        return
    try:
        payload = json.loads(shell_file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return
    prefs = payload.get('prefs') if isinstance(payload, dict) else None
    background = prefs.get('background') if isinstance(prefs, dict) else None
    if not isinstance(background, dict):
        return
    current = background.get('path')
    if not isinstance(current, str) or not current:
        return
    old_prefix = str(old_home.resolve())
    try:
        resolved = str(Path(current).resolve())
    except OSError:
        return
    if not resolved.startswith(old_prefix):
        return
    background['path'] = str(home.resolve()) + resolved[len(old_prefix):]
    shell_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'背景路径已改写为新实例：{background["path"]}')



def write_minimal_home(home: Path, library: str) -> None:
    """没有基线时现场写一份最小配置：够首页与图片相册出内容。"""
    config = home / '.config'
    plugins = config / 'plugins'
    plugins.mkdir(parents=True, exist_ok=True)
    (config / 'app.yaml').write_text('server:\n  host: 127.0.0.1\n  port: 18080\n'
                                     'trusted_hosts: []\ndirectories:\n  data_root: ./data\n',
                                     encoding='utf-8')
    (config / 'shell.json').write_text(json.dumps({
        'prefs': {
            'theme': 'light',
            'colors': {'--nav-width': '200px', '--sub-sidebar-width': '240px'},
            'background': {'path': '', 'version': 0, 'fit': 'cover', 'posX': 50, 'posY': 50,
                           'zoom': 100, 'flipX': False, 'flipY': False},
            'startup': {'page': 'plugin', 'route': '/image-viewer', 'fullscreen': False},
            'last_route': '/image-viewer',
        },
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (plugins / 'image-viewer.json').write_text(json.dumps({
        'row_height': 201, 'per_page': 110, 'sort_by': 'mtime', 'sort_order': 'desc',
        'root_dir': library, 'extra_roots': '', 'folders': {},
        'album_sort_by': 'mtime', 'album_sort_order': 'desc',
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


# --------------------------------------------------------------------------- 浏览器

def load_webdriver() -> Any:
    """按需导入 selenium（不在 requirements-dev 里，见模块抬头）。"""
    try:
        return importlib.import_module('selenium.webdriver')
    except ImportError as exc:  # pragma: no cover - 取决于本机环境
        raise ShootError(INSTALL_HINT) from exc


def load_keys_module() -> Any:
    """按需导入 selenium 的按键常量；同样不进 requirements-dev，CI 没有这个包。"""
    try:
        return importlib.import_module('selenium.webdriver.common.keys')
    except ImportError as exc:  # pragma: no cover - 取决于本机环境
        raise ShootError(INSTALL_HINT) from exc


def chrome_binary() -> Optional[str]:
    """复用 e2e 用例的 Chrome 定位。

    刻意不在这里另写一份：那份实现记着"为什么不回落到 Edge"（`webdriver.Chrome` 驱动
    不了 Edge，只有 Edge 的机器应当跳过而不是失败），抄一遍就会两处漂移。
    """
    ensure_import_path()
    from tests.harness.browser_binary import browser_binary
    return browser_binary()


class Browser:
    """有头 Chrome + 场景步骤执行器。"""

    def __init__(self, driver: Any, base_url: str, viewport: Tuple[int, int]) -> None:
        self.driver = driver
        self.base_url = base_url
        self.viewport = viewport
        # 壳骨架动作（导航项 / 顶栏动作 / 扩展入口）默认作用于哪个插件页；
        # `shell_page()` 在每一步之前把它设成目标插件。
        self._plugin = ''

    # ── 基础动作 ─────────────────────────────────────────────────────────

    def set_viewport(self, viewport: Tuple[int, int]) -> None:
        """CDP 固定视口：截出来的图正好是这些像素，与窗口边框、地址栏无关。"""
        width, height = viewport
        self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
            'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': False})

    def goto(self, path: str) -> None:
        self.driver.get(f'{self.base_url}{path}')

    def navigate(self, path: str) -> None:
        """切到某个路由，走壳暴露的 `window.__omniboxNavigate`。

        **不能**直接 `goto(插件路由)`：插件路由是 `App.vue` 在 `loadPlugins()` 之后才
        `router.addRoute` 注册的，深链接会在注册之前就被 vue-router 解析掉 —— 结果是
        "没有匹配的路由"，主区空白、插件 iframe 根本不挂（实测日志：`GET /image-viewer`
        之后浏览器再没请求过 `/plugins/.../index.html`）。`__omniboxNavigate` 在
        `addRoute` 之后才挂上 window，等到它就说明路由表已就绪。
        """
        ready = wait_until(lambda: bool(self.driver.execute_script(
            "return typeof window.__omniboxNavigate === 'function'")), UI_TIMEOUT)
        if not ready:
            raise ShootError('壳没有暴露 __omniboxNavigate，插件路由可能没注册成功')
        self.driver.execute_script('window.__omniboxNavigate(arguments[0]);', path)
        if not wait_until(lambda: self.driver.current_url.rstrip('/').endswith(path.rstrip('/')),
                          UI_TIMEOUT):
            raise ShootError(f'路由没有切到 {path}（当前 {self.driver.current_url}）')

    def wait_shell(self, timeout: float = UI_TIMEOUT) -> None:
        if not wait_until(lambda: self.find_all('.nav-item'), timeout):
            raise ShootError('壳前端未在超时内就绪（没等到 .nav-item）')

    def find(self, selector: str, timeout: float = UI_TIMEOUT) -> Any:
        found = wait_until(lambda: self._one(selector), timeout)
        if not found:
            raise ShootError(f'等不到元素：{selector}')
        return self._one(selector)

    def _one(self, selector: str) -> Any:
        elements = self.driver.find_elements('css selector', selector)
        return elements[0] if elements else None

    def find_all(self, selector: str) -> List[Any]:
        return list(self.driver.find_elements('css selector', selector))

    def frame(self, plugin: Optional[str]) -> None:
        """切进插件 iframe；`null` 回到壳文档（设置页不是 iframe）。"""
        if plugin is None:
            self.driver.switch_to.default_content()
            return
        box = self.find(f'iframe.plugin-iframe[data-plugin-name="{plugin}"]')
        self.driver.switch_to.frame(box)

    def frame_element(self, selector: str) -> None:
        """按选择器切进 iframe，可嵌套。

        **必须等到 iframe 真的有 `src`**：扩展视图的 `<iframe id="extension-frame">` 从首屏
        就存在于 DOM 里，`src` 是打开扩展时由 JS 写上去的。只等"元素出现"会切进一个空文档,
        表现是"切进去了、却读不到任何文案"，而错误信息看不出原因。
        """
        if not wait_until(lambda: self._has_src(selector), UI_TIMEOUT):
            raise ShootError(f'等不到带 src 的 iframe：{selector}（扩展视图可能没打开）')
        self.driver.switch_to.frame(self._one(selector))

    def _has_src(self, selector: str) -> bool:
        element = self._one(selector)
        if element is None:
            return False
        return bool((element.get_attribute('src') or '').strip())

    def body_text(self) -> str:
        return flatten_text(str(self.driver.execute_script('return document.body.innerText') or ''))

    def visible_text(self) -> str:
        """截图里能看到的全部文字：壳文档 + 当前**可见**插件 iframe 的内容文档。

        页架构是"壳渲染 chrome、内容 iframe 只装内容"：标题 / 导航 / 状态 / 校验控件在壳
        文档里，专辑卡、播放条、阅读正文在 iframe 里，两者都在同一张截图上。只读当前 frame
        会漏掉另一半 —— 旧场景的断言是 iframe 文案，新架构下标题与导航搬到了壳里。

        只取**可见**的那个 `.plugin-frame-container`（keep-alive 的插件 iframe 用 v-show
        隐藏，仍留在 DOM 里）：否则断言可能命中另一个插件页里的同名词。
        """
        self.driver.switch_to.default_content()
        shell = self.body_text()
        content = str(self.driver.execute_script(
            "const frames = Array.from("
            "  document.querySelectorAll('.plugin-frame-container iframe.plugin-iframe'));"
            "const frame = frames.find(f => f.offsetWidth || f.offsetHeight) || null;"
            "if (!frame) return '';"
            "try {"
            "  const doc = frame.contentDocument;"
            "  return doc && doc.body ? (doc.body.innerText || '') : '';"
            "} catch (e) { return ''; }") or '')
        return flatten_text(shell + '\n' + content)

    def visible_text_any(self) -> str:
        """同 `visible_text`，但**不切换 frame**：从任意上下文都能读到两边的文字。

        步骤里常出现"点内容页里的卡片 → 等壳侧副标题更新"（例如相册数报在壳的
        `.plugin-sub` 里），这时若用会切 frame 的 `visible_text`，后续在 iframe 里的点击
        就跑到了壳文档。`window.top` 同源可读，所以这里直接拼两侧的 `innerText`。
        """
        return flatten_text(str(self.driver.execute_script(
            "const top = window.top;"
            "const shell = top.document.body.innerText || '';"
            "const frames = Array.from("
            "  top.document.querySelectorAll('.plugin-frame-container iframe.plugin-iframe'));"
            "const frame = frames.find(f => f.offsetWidth || f.offsetHeight) || null;"
            "let content = '';"
            "try {"
            "  if (frame && frame.contentDocument && frame.contentDocument.body) {"
            "    content = frame.contentDocument.body.innerText || '';"
            "  }"
            "} catch (e) { content = ''; }"
            "return shell + '\\n' + content;") or ''))

    def wait_visible_text(self, needle: str, timeout: float = UI_TIMEOUT) -> None:
        """等一段文案出现在"截图里看得见的地方"（壳或内容 iframe），不切换 frame。"""
        if not wait_until(lambda: needle in self.visible_text_any(), timeout):
            raise ShootError(f'等不到文案（壳与内容页都没有）：{needle}')

    # ── 壳骨架（PluginChrome）的动作：导航项 / 顶栏动作 / 扩展入口 ────────────
    #
    # 这三类控件由壳渲染在**壳文档**里、按插件各占一个 `.plugin-frame-container`；
    # 多个插件页同时存在（keep-alive），所以选择器必须先定位到那个容器再找控件 ——
    # 直接在壳文档里点 `.plugin-nav-item` 会命中另一个插件的同 id 导航项。

    def shell_page(self, plugin: str) -> None:
        """切回壳文档，并把后续壳骨架动作的目标插件记为 `plugin`。"""
        self.driver.switch_to.default_content()
        self._plugin = plugin

    def _nav_item_js(self) -> str:
        return (
            "const name = arguments[0], view = arguments[1];"
            "const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))"
            "  .find(f => f.getAttribute('data-plugin-name') === name) || null;"
            "const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;"
            "const page = container ? container.querySelector('.plugin-page') : null;"
            "const item = page ? page.querySelector("
            "  '.plugin-nav-item[data-view-id=\"' + view + '\"]') : null;"
            "if (!item) return false;"
            "item.click(); return true;"
        )

    def click_view(self, plugin: str, view: str) -> None:
        """按 `data-view-id` 点壳侧导航项（例如 image-viewer 的 albums / document-reader 的 toc）。

        导航项可能**后来才出现**（插件在阅读态才把「目录」组报成可见），所以先等它出现，
        再点一次 —— 反复点会触发重复的视图切换，状态反而更难收敛。
        """
        self.shell_page(plugin)
        js = self._nav_item_js()
        if not wait_until(lambda: self.driver.execute_script(
                js.replace('item.click(); return true;', 'return true;'), plugin, view), UI_TIMEOUT):
            available = self.driver.execute_script(
                "const name = arguments[0];"
                "const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))"
                "  .find(f => f.getAttribute('data-plugin-name') === name) || null;"
                "const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;"
                "const page = container ? container.querySelector('.plugin-page') : null;"
                "return page ? Array.from(page.querySelectorAll('.plugin-nav-item'))"
                "  .map(b => b.getAttribute('data-view-id') || '') : [];", plugin)
            raise ShootError(f'壳侧导航项不存在：{plugin} / {view}（当前导航项 {available}）')
        self.driver.execute_script(js, plugin, view)

    def wait_view(self, plugin: str, view: str, timeout: float = UI_TIMEOUT) -> None:
        """等壳骨架把活动视图切到 `view`（`.plugin-page[data-active-view]`）。"""
        self.driver.switch_to.default_content()
        js = (
            "const name = arguments[0];"
            "const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))"
            "  .find(f => f.getAttribute('data-plugin-name') === name) || null;"
            "const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;"
            "const page = container ? container.querySelector('.plugin-page') : null;"
            "return page ? (page.getAttribute('data-active-view') || '') : null;"
        )
        if not wait_until(lambda: self.driver.execute_script(js, plugin) == view, timeout):
            current = self.driver.execute_script(js, plugin)
            raise ShootError(f'壳骨架没有切到视图 {plugin} / {view}（当前 {current!r}）')

    def click_action(self, plugin: str, label: str) -> None:
        """按文案点壳侧顶栏动作（含 leading 区与「更多」菜单之外的动作区）。

        `.plugin-actions-measure` 是同一排按钮的**量宽副本**（aria-hidden，用于计算溢出），
        文案与真按钮相同；不过滤它就会点在不可见的副本上 —— 看着点到了、界面没反应。
        """
        self.shell_page(plugin)
        js = (
            "const name = arguments[0], label = arguments[1];"
            "const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))"
            "  .find(f => f.getAttribute('data-plugin-name') === name) || null;"
            "const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;"
            "if (!container) return false;"
            "const groups = [container.querySelector('.plugin-leading'),"
            "  container.querySelector('.plugin-actions:not(.plugin-actions-measure)')];"
            "const buttons = groups.filter(Boolean).flatMap(g => Array.from(g.querySelectorAll('button')));"
            "const textOf = b => ((b.innerText || '') + ' ' + (b.title || '') + ' '"
            "  + (b.getAttribute('aria-label') || '')).trim();"
            "const hit = buttons.find(b => textOf(b).includes(label));"
            "if (!hit) return false;"
            "hit.click(); return true;"
        )
        if not wait_until(lambda: self.driver.execute_script(
                js.replace('hit.click(); return true;', 'return true;'), plugin, label), UI_TIMEOUT):
            available = self.driver.execute_script(
                "const name = arguments[0];"
                "const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))"
                "  .find(f => f.getAttribute('data-plugin-name') === name) || null;"
                "const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;"
                "const actions = container ? container.querySelector('.plugin-actions:not(.plugin-actions-measure)') : null;"
                "return actions ? Array.from(actions.querySelectorAll('button'))"
                "  .map(b => ((b.innerText || '') + '|' + (b.title || '')).trim()).filter(Boolean) : [];", plugin)
            raise ShootError(f'壳侧顶栏没有这个动作：{plugin} / {label}（当前 {available}）')
        self.driver.execute_script(js, plugin, label)

    def click_extension(self, plugin: str, label: str) -> None:
        """点壳侧扩展入口（`PluginExtensions` 渲染的 `.plugin-extension-item`）。

        扩展条目来自 `system_get_plugin_extensions` 的异步返回，必须轮询等它出现。
        """
        self.shell_page(plugin)
        js = (
            "const name = arguments[0], label = arguments[1];"
            "const frame = Array.from(document.querySelectorAll('iframe.plugin-iframe'))"
            "  .find(f => f.getAttribute('data-plugin-name') === name) || null;"
            "const container = frame ? (frame.closest('.plugin-frame-container') || frame.parentElement) : null;"
            "if (!container) return false;"
            "const items = Array.from(container.querySelectorAll('.plugin-extension-item'));"
            "const hit = items.find(b => ((b.innerText || b.textContent || '').trim()).includes(label));"
            "if (!hit) return false;"
            "hit.click(); return true;"
        )
        if not wait_until(lambda: self.driver.execute_script(
                js.replace('hit.click(); return true;', 'return true;'), plugin, label), UI_TIMEOUT):
            raise ShootError(f'壳侧扩展入口不存在：{plugin} / {label}')
        self.driver.execute_script(js, plugin, label)

    def run_script_file(self, relative: str) -> None:
        """执行仓库内的一个 JS 文件（场景里的"补画面"脚本）。

        脚本放在文件里而不是塞进场景 JSON 的一行字符串：这些补丁要逐字段对齐插件的
        渲染代码，可读性和可评审性比"少一个文件"重要。
        """
        path = (PROJECT_ROOT / relative).resolve()
        if not path.is_file():
            raise ShootError(f'找不到脚本文件：{path}')
        self.driver.execute_script(path.read_text(encoding='utf-8'))

    def find_by_text(self, needle: str, selector: str = CLICKABLE) -> Any:
        """按可见文字找控件（轮询到出现为止）。

        插件里的扩展入口（相册清理 / Pixiv 同步）由壳的 `PluginExtensions.vue` 渲染，不少按钮
        由共享组件渲染；类名会变，文字是它们对用户唯一稳定的标识，因此点击优先走这里，
        选择器只在有 id 时用。

        **必须轮询**：扩展入口要等壳的 `PluginExtensions.vue` 异步请求
        `system_get_plugin_extensions` 返回后才插进 DOM。早先这里只查一次，于是"入口还没
        渲染"和"入口不存在"表现成同一个错误 —— 同一批场景里 08 会挂、11 却能过，取决于
        当次渲染快慢。
        """
        deadline = time.time() + UI_TIMEOUT
        while time.time() < deadline:
            element = self._by_text(needle, selector)
            if element is not None:
                return element
            time.sleep(POLL)
        raise ShootError(f'等不到含这段文字的控件：{needle}')

    def _by_text(self, needle: str, selector: str) -> Any:
        # 优先真正可点的元素：扩展入口是「容器 div 套按钮」，在容器上派发的 click 不会
        # 触发挂在按钮上的监听 —— 点上去看着像点了、实际没反应。
        # 候选集把命中元素的子孙按钮/链接一并纳入：`in` 通常指向容器（如 #iv-extensions），
        # 只按选择器取元素会拿到容器本身。
        return self.driver.execute_script(
            'const needle = arguments[0], sel = arguments[1];'
            'const roots = Array.from(document.querySelectorAll(sel));'
            'const inner = roots.flatMap(r => Array.from('
            "  r.querySelectorAll('button, a, [role=\"button\"]')));"
            'const nodes = roots.concat(inner);'
            "const hits = nodes.filter(el => ((el.innerText || el.textContent || '').trim()).includes(needle));"
            'if (!hits.length) return null;'
            "return hits.find(el => el.tagName === 'BUTTON' || el.tagName === 'A') || hits[hits.length - 1];",
            needle, selector)

    def wait_text(self, needle: str, timeout: float = UI_TIMEOUT) -> None:
        if not wait_until(lambda: needle in self.body_text(), timeout):
            raise ShootError(f'等不到文案：{needle}（当前文档片段：{self.body_text()[:400]}）')

    def wait_gone(self, selector: str, timeout: float = UI_TIMEOUT) -> None:
        """等一个元素消失。

        壳的浮层提示（`.toast`）是自动消失的，但入场/退场各有一段时间；截图前不等它走完，
        图上就会多一条"原背景图已导入图片库"这类瞬时文案。
        """
        if not wait_until(lambda: self._one(selector) is None, timeout):
            raise ShootError(f'元素一直没消失：{selector}')

    def scroll_into_view(self, selector: str) -> None:
        element = self.find(selector)
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element)

    def capture(self) -> bytes:
        time.sleep(0.5)  # 等重排、图片解码与过渡收敛
        return bytes(self.driver.get_screenshot_as_png())

    # ── 场景步骤 ─────────────────────────────────────────────────────────

    def wait_freshness_idle(self, timeout: float = 60.0) -> None:
        """等壳侧「同步 / 校验」控件的**自动同步**跑完。

        为什么必须等：`freshness.js` 在挂载时被动跑一次增量同步，完成时回调会向内容页发
        `omnibox:action refresh` —— 插件据此重载自己的视图（document-reader 会退回书架）。
        拍摄途中撞上这次重载，画面会在中途被换掉，现象却是"点了没反应 / 自己退回去了"，
        从场景表上完全看不出来。

        先等控件挂上（没有 `chrome.freshness` 的页面直接放行），再等它空闲；最后留一小段
        时间让"完成 → 内容页重载"这条动作落地。
        """
        presence = (
            "const containers = Array.from(document.querySelectorAll('.plugin-frame-container'));"
            "const visible = containers.find(c => c.offsetWidth || c.offsetHeight) || null;"
            "return !!(visible && visible.querySelector('.plugin-freshness .obx-fresh'));"
        )
        if not wait_until(lambda: bool(self.driver.execute_script(presence)), 8.0):
            return
        idle = (
            "const containers = Array.from(document.querySelectorAll('.plugin-frame-container'));"
            "const visible = containers.find(c => c.offsetWidth || c.offsetHeight) || null;"
            "const fresh = visible ? visible.querySelector('.plugin-freshness') : null;"
            "if (!fresh) return true;"
            "const card = fresh.querySelector('.obx-fresh-card');"
            "const hidden = !card || card.classList.contains('hidden');"
            "const status = (fresh.querySelector('.obx-fresh-status') || {}).textContent || '';"
            "return hidden && status.indexOf('正在') < 0 && status.indexOf('同步中') < 0;"
        )
        if not wait_until(lambda: bool(self.driver.execute_script(idle)), timeout):
            raise ShootError('壳侧的自动同步 / 校验一直没结束，拍摄会在中途被打断')
        time.sleep(0.8)

    def scroll_to(self, selector: str, text: Optional[str] = None, margin: float = 6.0) -> None:
        """把匹配元素滚到它所在**滚动容器**的顶部（留 `margin`），并把页面级滚动清零。

        为什么不用 `scrollIntoView`：它会把**所有**可滚祖先（含 iframe 的文档本身）一起滚，
        弹窗标题因此被顶出视口 —— 截图里「标题消失、顶部一条被裁掉一半的滑杆行」就是这么来的，
        而且是否发生取决于当时的滚动量，不可复现。这里只动最近的那一层滚动容器。
        """
        js = (
            "const sel = arguments[0], needle = arguments[1] || '', margin = arguments[2];"
            "const el = Array.from(document.querySelectorAll(sel))"
            "  .find(e => !needle || ((e.innerText || e.textContent || '').includes(needle)));"
            "if (!el) return false;"
            "let node = el.parentElement, scroller = null;"
            "while (node && node !== document.body) {"
            "  const st = getComputedStyle(node);"
            "  if (/auto|scroll/.test(st.overflowY) && node.scrollHeight - node.clientHeight > 4) {"
            "    scroller = node; break;"
            "  }"
            "  node = node.parentElement;"
            "}"
            "if (scroller) {"
            "  const delta = el.getBoundingClientRect().top - scroller.getBoundingClientRect().top;"
            "  scroller.scrollTop = Math.max(0, scroller.scrollTop + delta - margin);"
            "}"
            "if (document.scrollingElement) document.scrollingElement.scrollTop = 0;"
            "window.scrollTo(0, 0);"
            "return true;")
        if not self.driver.execute_script(js, selector, text or '', margin):
            raise ShootError(f'要滚到顶部的元素不存在：{selector} / {text}')

    def require_in_viewport(self, selector: str, text: Optional[str] = None,
                            timeout: float = 10.0) -> None:
        """断言元素**完整落在视口内**，且没有被滚动容器裁切、页面本身也没被滚动。

        为什么 `expect` 挡不住这类问题：`expect` 比的是"文字在不在 DOM 里"，
        `getBoundingClientRect()` 之外的可见性（滚出视口、被 `overflow:hidden` 的祖先裁掉一半）
        它一概看不见 —— 标题滚出画面、露出半截滑杆的行因此能通过 `--check`。
        """
        js = (
            "const sel = arguments[0], needle = arguments[1] || '';"
            "const el = Array.from(document.querySelectorAll(sel))"
            "  .find(e => !needle || ((e.innerText || e.textContent || '').includes(needle)));"
            "if (!el) return { ok: false, why: 'missing' };"
            "const r = el.getBoundingClientRect();"
            "const vw = window.innerWidth, vh = window.innerHeight;"
            "const rect = { top: Math.round(r.top), bottom: Math.round(r.bottom),"
            "  left: Math.round(r.left), right: Math.round(r.right) };"
            "if (!(r.top >= -0.5 && r.left >= -0.5 && r.bottom <= vh + 0.5 && r.right <= vw + 0.5)) {"
            "  return { ok: false, why: 'out-of-viewport', rect: rect, vw: vw, vh: vh };"
            "}"
            "let node = el.parentElement;"
            "while (node && node !== document.body) {"
            "  const st = getComputedStyle(node);"
            "  if (/auto|scroll/.test(st.overflowY) && node.scrollHeight - node.clientHeight > 4) {"
            "    const sr = node.getBoundingClientRect();"
            "    if (r.top < sr.top - 0.5 || r.bottom > sr.bottom + 0.5) {"
            "      return { ok: false, why: 'clipped-by-scroller', rect: rect,"
            "        scroller: { top: Math.round(sr.top), bottom: Math.round(sr.bottom),"
            "          scrollTop: node.scrollTop } };"
            "    }"
            "  }"
            "  node = node.parentElement;"
            "}"
            "const pageTop = document.scrollingElement ? document.scrollingElement.scrollTop : 0;"
            "if (pageTop !== 0) return { ok: false, why: 'page-scrolled', rect: rect, pageTop: pageTop };"
            "return { ok: true, rect: rect };")
        def probe() -> Dict[str, Any]:
            return dict(self.driver.execute_script(js, selector, text or '') or {})
        if not wait_until(lambda: bool(probe().get('ok')), timeout):
            detail = probe()
            raise ShootError(f'构图断言失败：{selector} {text or ""} -> '
                             f'{json.dumps(detail, ensure_ascii=False)}')

    def press_key(self, key: str) -> None:
        """在当前文档里按一个键（场景用它收起可能残留的浮层）。

        元素级 `send_keys` 不需要真实焦点，投给 `body` 即可；`Escape` 是多数浮层/弹窗
        既有的关闭键，但**不能假设它一定能关**——调用方仍要用 `wait_gone` 确认收起了。
        """
        keys = load_keys_module().Keys
        named = {'escape': keys.ESCAPE, 'esc': keys.ESCAPE, 'enter': keys.ENTER,
                 'tab': keys.TAB, 'space': keys.SPACE}
        body = self.driver.find_element('tag name', 'body')
        body.send_keys(named.get(key.strip().lower(), key))

    def click_until(self, selector: str, until_text: str, timeout: float = 25.0) -> None:
        """点一个元素直到某段文案出现；没出现就重点。

        卡片类入口在数据/设置到达后会整体重渲染，第一次 `click` 可能落在被替换前的节点上
        （事件处理器还没重新绑定），表现为"点了没反应"，而且不会报错。重试一次成本极低，
        比在场景里堆 sleep 稳。
        """
        deadline = time.time() + timeout
        attempts = 0
        while time.time() < deadline:
            attempts += 1
            try:
                self.click_js(selector, timeout=min(5.0, max(1.0, deadline - time.time())))
            except ShootError:
                pass
            if wait_until(lambda: until_text in self.visible_text_any(),
                          min(8.0, max(0.5, deadline - time.time()))):
                return
        raise ShootError(f'点了 {attempts} 次 {selector} 也没等到文案：{until_text}')

    def click_js(self, selector: str, timeout: float = UI_TIMEOUT) -> None:
        """在当前文档里用元素自身的 `click()` 点它。

        为什么不用 Selenium 的原生 click：列表视图会在数据到达后**整体重渲染**，原生 click
        按坐标派发 —— 元素在这一瞬间被替换就会点到别的位置（而且不报错），表现为"点了没
        反应"或"进了另一个相册"。`el.click()` 打在当前节点上，事件处理器要么收到、要么
        元素已不在（这里先等它出现再点一次）。
        """
        if not wait_until(lambda: bool(self.driver.execute_script(
                'return !!document.querySelector(arguments[0]);', selector)), timeout):
            raise ShootError(f'等不到元素：{selector}')
        clicked = self.driver.execute_script(
            'const el = document.querySelector(arguments[0]);'
            'if (!el) return false; el.click(); return true;', selector)
        if not clicked:
            raise ShootError(f'点击失败（元素在点击前消失）：{selector}')

    def click_selector(self, selector: str, timeout: float = UI_TIMEOUT) -> None:
        """点一个选择器：等它出现 → 点 → 失败就重取一次。

        列表类视图会在数据到达后**整体重渲染**，先前 find 到的元素随即失效
        （StaleElementReferenceException）。重取一次成本极低，却能挡住这类竞态；
        其余异常（元素不可交互等）统一包成可读错误，不再抛一屏 chromedriver 堆栈。
        """
        last: Optional[str] = None
        for attempt in range(3):
            element = self.find(selector, timeout=timeout if attempt == 0 else 5.0)
            try:
                element.click()
                return
            except Exception as exc:
                last = f'{type(exc).__name__}: {exc}'
                if 'Stale' not in type(exc).__name__:
                    break
                time.sleep(0.4)
        raise ShootError(f'点击失败：{selector}（{last}）')

    def click_text(self, needle: str, selector: Optional[str] = None) -> None:
        """按文字点：与 click_selector 同样处理重渲染导致的元素失效。"""
        last: Optional[str] = None
        for _attempt in range(3):
            element = self.find_by_text(needle, selector or CLICKABLE)
            try:
                element.click()
                return
            except Exception as exc:
                last = f'{type(exc).__name__}: {exc}'
                if 'Stale' not in type(exc).__name__:
                    break
                time.sleep(0.4)
        raise ShootError(f'点击失败：文字「{needle}」（{last}）')

    def run_step(self, step: Dict[str, Any]) -> None:
        timeout = float(step.get('timeout', UI_TIMEOUT))
        if 'goto' in step:
            self.goto(str(step['goto']))
        elif 'frame' in step:
            self.frame(step['frame'])
        elif 'frame_element' in step:
            self.frame_element(str(step['frame_element']))
        elif 'click' in step:
            self.click_selector(str(step['click']), timeout=timeout)
        elif 'click_js' in step:
            self.click_js(str(step['click_js']), timeout=timeout)
        elif 'click_until' in step:
            target = step['click_until']
            self.click_until(str(target['selector']), str(target['until_text']),
                             timeout=float(target.get('timeout', 25.0)))
        elif 'key' in step:
            self.press_key(str(step['key']))
        elif 'scroll_to' in step:
            target = step['scroll_to']
            self.scroll_to(str(target['selector']), target.get('text'),
                           float(target.get('margin', 6.0)))
        elif 'require_in_viewport' in step:
            target = step['require_in_viewport']
            self.require_in_viewport(str(target['selector']), target.get('text'),
                                     float(target.get('timeout', 10.0)))
        elif 'click_text' in step:
            self.click_text(str(step['click_text']), str(step.get('in', CLICKABLE)))
        elif 'click_view' in step:
            target = step['click_view']
            self.click_view(str(target['plugin']), str(target['view']))
        elif 'wait_view' in step:
            target = step['wait_view']
            self.wait_view(str(target['plugin']), str(target['view']), timeout=timeout)
        elif 'click_action' in step:
            target = step['click_action']
            self.click_action(str(target['plugin']), str(target['label']))
        elif 'click_extension' in step:
            target = step['click_extension']
            self.click_extension(str(target['plugin']), str(target['label']))
        elif 'wait_js' in step:
            # 等一段 JS 表达式为真：比固定 sleep 稳（原生面板的逻辑页要等 native-ready
            # 握手后才挂上 window.Bridge，早一步注入补丁就是静默失败）
            expression = str(step['wait_js'])
            if not wait_until(lambda: bool(self.driver.execute_script(expression)), timeout):
                raise ShootError(f'等不到条件成立：{expression}')
        elif 'wait' in step:
            self.find(str(step['wait']), timeout=timeout)
        elif 'wait_text' in step:
            self.wait_text(str(step['wait_text']), timeout=timeout)
        elif 'wait_visible_text' in step:
            self.wait_visible_text(str(step['wait_visible_text']), timeout=timeout)
        elif 'wait_gone' in step:
            self.wait_gone(str(step['wait_gone']), timeout=timeout)
        elif 'scroll_into_view' in step:
            self.scroll_into_view(str(step['scroll_into_view']))
        elif 'scroll_text' in step:
            element = self.find_by_text(str(step['scroll_text']), str(step.get('in', CLICKABLE)))
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", element)
        elif 'sleep' in step:
            time.sleep(float(step['sleep']))
        elif 'js' in step:
            self.driver.execute_script(str(step['js']))
        elif 'js_file' in step:
            self.run_script_file(str(step['js_file']))
        else:
            raise ShootError(f'未知步骤：{json.dumps(step, ensure_ascii=False)}')


# --------------------------------------------------------------------------- 出图

def to_webp(png_bytes: bytes, target: Path) -> str:
    """转 WebP：优先 ffmpeg（与既有配图同参数），否则退回 Pillow。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        PNG_CACHE.mkdir(parents=True, exist_ok=True)
        temporary = PNG_CACHE / f'{target.stem}.png'
        temporary.write_bytes(png_bytes)
        subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-i', str(temporary),
                        '-c:v', 'libwebp', '-quality', '90', '-compression_level', '6',
                        '-preset', 'text', str(target)], check=True)
        return 'ffmpeg'
    with Image.open(io.BytesIO(png_bytes)) as image:
        image.convert('RGB').save(target, 'WEBP', quality=90, method=6)
    return 'pillow'


def crop_png(png_bytes: bytes, rect: Dict[str, int]) -> bytes:
    """按场景声明裁出拍摄位置（x/y/width/height，视口坐标）。"""
    with Image.open(io.BytesIO(png_bytes)) as image:
        box = (int(rect['x']), int(rect['y']),
               int(rect['x']) + int(rect['width']), int(rect['y']) + int(rect['height']))
        cropped = image.crop(box)
        buffer = io.BytesIO()
        cropped.save(buffer, 'PNG')
        return buffer.getvalue()


# --------------------------------------------------------------------------- 场景

def substitute_home(value: Any, home: Path) -> Any:
    """把场景里的 `${HOME}` 换成运行实例的用户数据目录。

    背景图路径在配置里是**绝对路径**（`<config>/backgrounds/<文件名>`），而每次运行
    的实例都在新的临时目录里，所以场景表不能写死它 —— 写死就会指向一个不存在的文件。
    """
    if isinstance(value, str):
        return value.replace('${HOME}', str(home))
    if isinstance(value, list):
        return [substitute_home(item, home) for item in value]
    if isinstance(value, dict):
        return {key: substitute_home(item, home) for key, item in value.items()}
    return value


def _flatten(value: Any, prefix: str = '') -> Dict[str, Any]:
    """把嵌套的偏好扁平成 `colors.--glass-blur` 这样的键，便于逐项比对。"""
    if isinstance(value, dict):
        flat: Dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f'{prefix}{key}.'))
        return flat
    return {prefix.rstrip('.'): value}


def _baseline_value(prefs: Dict[str, Any], key: str) -> Any:
    current: Any = prefs
    for part in key.split('.'):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def read_baseline_prefs(home: Path) -> Dict[str, Any]:
    """读基线里的壳偏好（实例启动**之前**读，之后 API 写入会覆盖这个文件）。

    用途有二：一是每场开拍前把上一场留下的改动还原，二是拿它当"哪些项被场景覆盖了"的
    比较基准。没有它的话，比较基准会变成"上一场写完的值"——输出的差异看着对，其实基准在漂。
    """
    shell_file = home / '.config' / 'shell.json'
    try:
        payload = json.loads(shell_file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    prefs = payload.get('prefs') if isinstance(payload, dict) else None
    return prefs if isinstance(prefs, dict) else {}


def describe_overrides(baseline: Dict[str, Any], patch: Any) -> List[str]:
    """列出场景覆盖了基线的哪几项。

    为什么必须打出来：基线里的值是你**在配置窗口里调好的**，场景却可能为某一张图改它
    （典型是 12 为了让「位置」滑块出现而把显示方式改成自由）。不声不响地覆盖 = 你的调整
    白做，而且从图上看不出来是被覆盖了还是没生效。`background.path` 只比文件名：
    运行时会把路径前缀改写到临时目录，逐字比必然"不等"，那不是覆盖。
    """
    if not isinstance(patch, dict) or not patch or not baseline:
        return []
    lines: List[str] = []
    for key, value in sorted(_flatten(patch).items()):
        current = _baseline_value(baseline, key)
        if (key.endswith('path') and isinstance(value, str) and isinstance(current, str)
                and Path(value).name == Path(current).name):
            continue
        if current != value:
            lines.append(f'{key}: {current!r} → {value!r}')
    return lines


def apply_setup(server: Server, scene: Dict[str, Any], baseline: Dict[str, Any]) -> None:
    """场景前置状态：先把壳偏好还原成基线，再应用本场的覆盖。

    **还原这一步不能省**：13 张图跑在同一个实例里，壳偏好是持久状态 —— 12 为露出「位置」
    滑块把显示方式改成「自由」，13 不还原就会继承它，于是壁纸按原图尺寸摆放、只占窗口中央
    一小块（实测：整片内容区是纯底色，看着像"背景没生效"）。场景之间必须互不影响。
    插件设置不走这条（它们是各插件自己的文件，场景里没有改它们的步骤）。
    """
    if baseline:
        server.call('system_set_shell_prefs', baseline)
    prefs = scene.get('shell_prefs')
    if isinstance(prefs, dict) and prefs:
        server.call('system_set_shell_prefs', prefs)
    for call in scene.get('setup', []) or []:
        server.call(str(call['method']), *list(call.get('args', [])),
                    timeout=float(call.get('timeout', 60)))


def run_scene(server: Server, browser: Browser, scene: Dict[str, Any], *,
              check_only: bool, out_dir: Path, home: Path,
              baseline: Dict[str, Any]) -> Optional[Path]:
    scene = dict(substitute_home(scene, home))
    slot = str(scene.get('slot', '?'))
    print(f'[{slot}] {scene.get("title", "")}')
    # 覆盖必须是看得见的：基线里的值是在配置窗口里调好的，场景为某一张图改它时必须
    # 打出来，否则"我配了却没生效"和"场景覆盖了"从图上看不出区别。
    for line in describe_overrides(baseline, scene.get('shell_prefs')):
        print(f'[{slot}] 覆盖基线：{line}')
    apply_setup(server, scene, baseline)
    browser.set_viewport(tuple(scene.get('viewport', VIEWPORT)))  # type: ignore[arg-type]
    browser.frame(None)
    # 先落在壳首页，等插件路由注册完再切 —— 理由见 Browser.navigate 的注释。
    browser.goto('/')
    browser.wait_shell()
    route = str(scene.get('route', '/'))
    if route and route != '/':
        browser.navigate(route)
    # 壳侧 freshness 的自动同步会在挂载后几秒完成并让内容页重载视图；
    # 场景开始前先等它落定，否则点击与重载互相穿插（表现为"点了没反应"）。
    browser.wait_freshness_idle()
    for step in scene.get('steps', []) or []:
        browser.run_step(step)

    expect = [str(item) for item in scene.get('expect', []) or []]
    # 截的是整个视口：壳骨架与内容 iframe 都在画面里，断言必须两边都看（见 visible_text）
    text = browser.visible_text()
    missing = [item for item in expect if item not in text]
    if missing:
        raise ShootError(f'[{slot}] 界面里找不到这些文案，配图已过期：{missing}')
    if expect:
        print(f'[{slot}] 文案断言通过：{len(expect)} 项')
    if check_only:
        return None

    png = browser.capture()
    crop = scene.get('crop')
    if isinstance(crop, dict):
        png = crop_png(png, crop)
    target = out_dir / f'{slot}.webp'
    tool = to_webp(png, target)
    print(f'[{slot}] -> {target.relative_to(PROJECT_ROOT)}  {target.stat().st_size // 1024} KB（{tool}）')
    return target


# --------------------------------------------------------------------------- 入口

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='按场景表抓取 README 配图（视口截图）')
    parser.add_argument('--scenes', default=str(SCENES_FILE), help='场景表路径')
    parser.add_argument('--home', default='', help=f'已配置好的用户数据目录（默认 {PREPARED_HOME}）')
    parser.add_argument('--blank', action='store_true', help='不用基线，现场生成最小配置')
    parser.add_argument('--library', default='', help='--blank 必填：现场生成的最小配置要用的图片根目录')
    parser.add_argument('--only', default='', help='只跑这些槽位（逗号分隔）')
    parser.add_argument('--list', action='store_true', help='列出场景后退出')
    parser.add_argument('--check', action='store_true', help='只断言文案，不出图')
    parser.add_argument('--out', default=str(IMAGES_DIR), help='WebP 输出目录')
    parser.add_argument('--port', type=int, default=0, help='实例端口（默认取空闲端口）')
    parser.add_argument('--keep', action='store_true', help='结束后保留临时目录，便于排查')
    args = parser.parse_args(argv)
    # 不给默认图库：默认值只能是某台开发机的真实路径，写进仓库就是隐私门禁的漏点，
    # 而且那台机器上才存在的目录会让别人跑出"配好了但图库是空的"这种假成功。
    if args.blank and not args.library:
        parser.error('--blank 必须同时提供 --library：现场生成最小配置需要一个真实存在的图片根目录')
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    table = load_scenes(Path(args.scenes))
    scenes = select_scenes(table, args.only)
    viewport = tuple(table.get('viewport', VIEWPORT))  # type: ignore[arg-type]

    if args.list:
        for scene in table['scenes']:
            print(f"{scene.get('slot', '?'):<22} {scene.get('title', '')}")
        return 0

    temporary: Optional[str] = None
    server: Optional[Server] = None
    driver: Any = None
    try:
        # 用 mkdtemp 而不是 TemporaryDirectory：后者的析构器在进程退出时无条件删目录，
        # `--keep` 会因此变成空操作（保留目录是为了排查失败场景，那时最需要它）。
        temporary = tempfile.mkdtemp(prefix='omnibox-shoot-')
        home = Path(temporary) / 'home'
        if args.blank:
            write_minimal_home(home, args.library)
        else:
            source = Path(args.home) if args.home else PREPARED_HOME
            if not source.is_dir():
                raise ShootError(
                    f'找不到基线目录：{source}\n'
                    f'先起配置台把基线配一次：`python tools/shoot_readme_setup.py`，'
                    f'在里面配好各插件库根 / 主题 / 宽度 / 背景（壁纸可带 --wallpapers <目录> 预导入）；'
                    f'`--blank` 只写"够首页与图片相册出内容"的最小配置，'
                    f'03/04/05/06/07/10/12/13 会缺库根与背景，出的图与其余槽位不一致。')
            copy_baseline(source, home)
        baseline_prefs = read_baseline_prefs(home)
        server = Server(home, args.port or free_port(),
                        Path(temporary) / 'instance.log').start()
        print(f'实例就绪：{server.base_url}（home={home}）')

        webdriver = load_webdriver()
        options = importlib.import_module('selenium.webdriver.chrome.options').Options()
        binary = chrome_binary()
        if binary:
            options.binary_location = binary
        options.add_argument(f'--window-size={viewport[0]},{viewport[1]}')
        options.add_argument('--force-device-scale-factor=1')
        options.add_argument('--hide-scrollbars')
        options.add_argument('--no-first-run')
        options.add_argument('--no-default-browser-check')
        # 有头但不抢焦点：图里只有视口，窗口挪到屏幕外不影响渲染结果。
        options.add_argument('--window-position=-32000,-32000')
        try:
            driver = webdriver.Chrome(options=options)
        except Exception as exc:  # pragma: no cover - 取决于本机驱动
            raise ShootError(f'无法启动 Chrome：{type(exc).__name__}: {exc}') from exc

        browser = Browser(driver, server.base_url, viewport)
        browser.set_viewport(viewport)
        out_dir = Path(args.out).resolve()
        failures: List[str] = []
        for scene in scenes:
            try:
                run_scene(server, browser, scene, check_only=args.check,
                          out_dir=out_dir, home=home, baseline=baseline_prefs)
            except ShootError as exc:
                failures.append(str(exc))
                print(f'!! {exc}', file=sys.stderr)
        if failures:
            print(f'\n{len(failures)} 个场景未通过：', file=sys.stderr)
            for item in failures:
                print(f'  - {item.splitlines()[0]}', file=sys.stderr)
            return 1
        return 0
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if server is not None:
            server.stop()
        if temporary is not None:
            if args.keep:
                print(f'临时目录保留在：{temporary}')
            else:
                shutil.rmtree(temporary, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
