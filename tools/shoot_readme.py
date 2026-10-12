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

# 按文字找控件时的候选集合：按钮、链接、导航项、工具栏按钮与扩展入口。
CLICKABLE = ('button, a, [role="button"], .obx-nav-item, .obx-toolbar-btn, '
             '.obx-extension, .iv-nav-item, .ml-nav-item, .mp-nav-item, .nr-nav-item, .gm-nav-item')

# 复制基线时剔除的东西：WebView2 profile（几十 MB）、日志、访问令牌与主体表。
# 令牌与主体表由实例启动时自己重建，复制过去只会把上一轮的凭据带进新一轮。
EXCLUDE_NAMES = frozenset({'webview', 'logs', 'auth_token.txt', 'principals.json'})


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

    def call(self, method: str, *args: Any) -> Any:
        """调 `/api/<method>`（壳方法与插件方法同一张表：`<插件>__<方法>`）。"""
        ensure_import_path()
        from shell.backend.auth import TOKEN_HEADER
        body = json.dumps({'args': list(args), 'kwargs': {}}, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(
            f'{self.base_url}/api/{method}', data=body, method='POST',
            headers={'Content-Type': 'application/json', TOKEN_HEADER: self.token})
        with urllib.request.urlopen(request, timeout=60) as response:
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
        return str(self.driver.execute_script('return document.body.innerText') or '')

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

        插件里的扩展入口（相册清理 / Pixiv 同步）与不少按钮由共享组件渲染，类名会变，
        文字是它们对用户唯一稳定的标识；因此点击优先走这里，选择器只在有 id 时用。

        **必须轮询**：扩展入口是 `renderExtensions()` 异步请求
        `system_get_plugin_extensions` 之后才插进 DOM 的。早先这里只查一次，于是"入口还没
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
            raise ShootError(f'等不到文案：{needle}')

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

    def click_selector(self, selector: str) -> None:
        """点一个选择器：等它出现 → 点 → 失败就重取一次。

        列表类视图会在数据到达后**整体重渲染**，先前 find 到的元素随即失效
        （StaleElementReferenceException）。重取一次成本极低，却能挡住这类竞态；
        其余异常（元素不可交互等）统一包成可读错误，不再抛一屏 chromedriver 堆栈。
        """
        last: Optional[str] = None
        for attempt in range(3):
            element = self.find(selector, timeout=UI_TIMEOUT if attempt == 0 else 5.0)
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
        if 'goto' in step:
            self.goto(str(step['goto']))
        elif 'frame' in step:
            self.frame(step['frame'])
        elif 'frame_element' in step:
            self.frame_element(str(step['frame_element']))
        elif 'click' in step:
            self.click_selector(str(step['click']))
        elif 'click_text' in step:
            self.click_text(str(step['click_text']), str(step.get('in', CLICKABLE)))
        elif 'wait' in step:
            self.find(str(step['wait']))
        elif 'wait_text' in step:
            self.wait_text(str(step['wait_text']))
        elif 'wait_gone' in step:
            self.wait_gone(str(step['wait_gone']))
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
        server.call(str(call['method']), *list(call.get('args', [])))


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
    for step in scene.get('steps', []) or []:
        browser.run_step(step)

    expect = [str(item) for item in scene.get('expect', []) or []]
    text = browser.body_text()
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
    parser.add_argument('--library', default='', help='--blank 时的图片根目录')
    parser.add_argument('--only', default='', help='只跑这些槽位（逗号分隔）')
    parser.add_argument('--list', action='store_true', help='列出场景后退出')
    parser.add_argument('--check', action='store_true', help='只断言文案，不出图')
    parser.add_argument('--out', default=str(IMAGES_DIR), help='WebP 输出目录')
    parser.add_argument('--port', type=int, default=0, help='实例端口（默认取空闲端口）')
    parser.add_argument('--keep', action='store_true', help='结束后保留临时目录，便于排查')
    return parser.parse_args(argv)


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
            write_minimal_home(home, args.library or r'G:\图片')
        else:
            source = Path(args.home) if args.home else PREPARED_HOME
            if not source.is_dir():
                raise ShootError(
                    f'找不到基线目录：{source}\n'
                    f'先跑 `python tools/shoot_readme_setup.py --library <示例库根> '
                    f'--manga <漫画根>` 起配置实例，在里面配好背景 / 主题 / 各插件根目录；'
                    f'或改用 --blank 现场生成一份最小配置。')
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
