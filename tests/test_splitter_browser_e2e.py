"""分隔条（可变边栏）的真实浏览器端到端用例（本地手动运行，不进 CI）。

为什么需要它
------------
`/shell/splitter.js` 这套内核只做一件事：把用户的拖动写成 `<html>` 上的一个 CSS
变量。它有三类缺陷只有真实浏览器量得出来，而且都不会报错：

  - **长在 `:root` 上的别名读不到元素内联值**。group-mesh 曾写
    `--gm-side-width: var(--sub-sidebar-width)`（在 `:root` 上声明），于是
    `var()` 在 `:root` 上替换一次就固定成默认宽度，分隔条写在 `.gm-side` 内联上的
    用户值永远轮不到生效 —— 表现是**拖动全程宽度不动、松手才跳一下**（松手时壳写
    的是 `:root`，那一条才会被替换）。五个插件里只有它这么写，改完必须逐帧量，
    只看"拖动前后"两次取值是抓不到的（首尾值都对）。
  - **把手的占位**：`.obx-splitter-x` 的 5px 热区靠负外边距压在交界处，净占位
    1px 时那一列谁都没上色，透出的是祖先的背景（玻璃模式下就是背景图本身）——
    用户看到的"1px 竖缝"。
  - **落盘的时机**：拖动过程去抖、松手立刻落盘、`pagehide` 补发，任何一处写错都
    表现为"松手后宽度弹回上一次的值"，同样只有量出来才看得见。

做法
----
起隔离实例（`OMNIBOX_HOME` 指向临时目录，不碰开发机的偏好）+ 无头 Chrome，用
CDP 的 `Input.dispatchMouseEvent` 投递**一次连续手势**：WebDriver 的 Actions 分批
`perform()` 就不是一次手势了，无法在手势中间读值。页面侧在拖动期间用 rAF 逐帧
采样，手势结束后读回整条宽度序列。

本地运行
--------
    venv/Scripts/pip install -r requirements-e2e.txt
    venv/Scripts/python -m unittest tests.test_splitter_browser_e2e -v

依赖：本机已安装 Chrome（Edge 不算，理由见 `tests/harness/browser_binary.py`）与
selenium 4.6+。
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

#: 首屏（插件列表 + 壳挂载）的等待上限
STARTUP_TIMEOUT = 40.0
#: 一次拖动结束到壳落盘的时间上限（去抖 300ms + 一次后端写入）
FLUSH_TIMEOUT = 8.0
#: 拖动经过的像素数与步数：步与步之间要留出渲染时间，否则量到的是同一帧
DRAG_PX = 120
DRAG_STEPS = 6

BOOT_OK, BOOT_REASON = boot_prerequisites()


def _have_selenium() -> bool:
    try:
        from selenium import webdriver  # noqa: F401
    except Exception:
        return False
    return True


SKIP_REASON = None if (_have_selenium() and browser_binary() is not None and BOOT_OK) else (
    '跳过：' + ('未安装 selenium（见 requirements-e2e.txt）' if not _have_selenium()
               else '未检测到 Chrome' if browser_binary() is None else BOOT_REASON))


# ===== 五个插件的内边栏（都接入同一支共享变量） =====
#: 路由 → (iframe 的 data-plugin-name, 侧栏选择器, 变量名)
PLUGIN_SIDEBARS = {
    'document-reader': ('.nr-sidebar', '--sub-sidebar-width'),
    'group-mesh': ('.gm-side', '--sub-sidebar-width'),
    'image-viewer': ('.iv-sidebar', '--sub-sidebar-width'),
    'manga-library': ('.ml-sidebar', '--sub-sidebar-width'),
    'media-player': ('.mp-sidebar', '--sub-sidebar-width'),
}


def _sampler_js(frame_name: str, selector: str, var_name: str) -> str:
    """在被拖元素上开一个逐帧采样器，返回把手中心（顶层视口坐标）与起始宽度。

    采样写在 iframe 自己的文档里：把手的指针事件由那个文档接收，宽度也在那里变。
    """
    return """
    const name = arguments[0], sel = arguments[1], varName = arguments[2];
    const f = document.querySelector('iframe[data-plugin-name="' + name + '"]');
    if (!f) return {err: '没有 ' + name + ' 的 iframe'};
    const d = f.contentDocument;
    const side = d.querySelector(sel);
    const handle = d.querySelector('[data-obx-splitter-handle]');
    if (!side || !handle) return {err: '侧栏或把手还没渲染', hasSide: !!side, hasHandle: !!handle};
    const cs = d.defaultView.getComputedStyle(side);
    window.__samples = [];
    const tick = () => {
      window.__samples.push({
        w: +side.getBoundingClientRect().width.toFixed(1),
        elVar: side.style.getPropertyValue(varName) || null,
        rootVar: d.documentElement.style.getPropertyValue(varName) || null,
        live: side.classList.contains('obx-splitter-live'),
        transition: cs.transitionDuration,
        dragging: d.documentElement.getAttribute('data-obx-dragging'),
      });
      if (window.__samples.length < 600) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    const r = handle.getBoundingClientRect(), a = f.getBoundingClientRect();
    return {x: a.left + r.left + r.width / 2, y: a.top + r.top + r.height / 2,
            width: +side.getBoundingClientRect().width.toFixed(1),
            handleWidth: +r.width.toFixed(2), handleLeft: +r.left.toFixed(2)};
    """


@unittest.skipIf(SKIP_REASON, SKIP_REASON or '')
class SplitterBrowserE2ETests(unittest.TestCase):
    """真实 Chrome + 隔离实例，核对拖动过程、落盘与占位。"""

    driver = None
    instance = None

    @classmethod
    def setUpClass(cls):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        cls._tmp = tempfile.TemporaryDirectory()
        cls.instance = AppInstance('splitter', Path(cls._tmp.name)).start()

        options = Options()
        binary = browser_binary()
        if binary:
            options.binary_location = binary
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1400,900')
        options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
        try:
            cls.driver = webdriver.Chrome(options=options)
        except Exception as exc:  # pragma: no cover - 取决于本机驱动
            cls._cleanup()
            raise unittest.SkipTest(f'无法启动 WebDriver（{type(exc).__name__}: {exc}）') from exc
        cls.driver.set_page_load_timeout(30)

        cls.driver.get(cls.instance.base_url)
        if not wait_until(lambda: bool(cls._js(
                "return document.querySelectorAll('.nav-item').length > 0;")), STARTUP_TIMEOUT):
            cls._cleanup()
            raise unittest.SkipTest('壳首屏未在超时内渲染出插件导航')
        time.sleep(1.0)

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    @classmethod
    def _cleanup(cls):
        if cls.driver is not None:
            try:
                cls.driver.quit()
            except Exception:
                pass
            cls.driver = None
        if cls.instance is not None:
            try:
                cls.instance.stop()
            except Exception:
                pass
            cls.instance = None
        tmp = getattr(cls, '_tmp', None)
        if tmp is not None:
            tmp.cleanup()

    # ------------------------------------------------------------------ 工具

    @classmethod
    def _js(cls, script: str, *args):
        return cls.driver.execute_script(script, *args)

    @classmethod
    def _saved_prefs(cls) -> dict:
        file = cls.instance.config_dir / 'shell.json'
        if not file.exists():
            return {}
        return json.loads(file.read_text(encoding='utf-8')).get('prefs', {})

    @classmethod
    def _saved_colors(cls) -> dict:
        return cls._saved_prefs().get('colors') or {}

    @classmethod
    def _wait_colors(cls, key: str, value: str) -> bool:
        """等壳把一次拖动落盘（去抖 + 一次后端写入）。"""
        return wait_until(lambda: cls._saved_colors().get(key) == value, FLUSH_TIMEOUT)

    @classmethod
    def _active_plugin(cls) -> str | None:
        """当前**可见**的插件 iframe 名。

        不能取 `querySelector('iframe[data-plugin-name]')`：保活的插件切走后仍留在
        DOM 里（`v-show` 隐藏），先挂载过的那个会永远排在前面，取到的就不是当前页。
        """
        return cls._js("""
            const frames = Array.from(document.querySelectorAll('iframe[data-plugin-name]'));
            const shown = frames.filter(f => f.offsetParent !== null || f.getBoundingClientRect().width > 0);
            const pick = (shown.length ? shown : frames)[0];
            return pick ? pick.getAttribute('data-plugin-name') : null;
        """)

    @classmethod
    def _wait_handle(cls, frame_name: str, selector: str) -> bool:
        """等插件 iframe 里的把手就位。

        必须等：iframe 的 `load` 处理会递增文档代次并可能重载一次 src，刚点完导航时
        文档还可能被换掉 —— 那一刻拿到的 `contentDocument` 是旧文档，页面侧挂的
        采样器会跟着旧文档一起消失（表现为"拖动期间一帧都没采到"）。
        """
        return wait_until(lambda: bool(cls._js("""
            const f = document.querySelector('iframe[data-plugin-name="' + arguments[0] + '"]');
            if (!f || !f.contentDocument) return false;
            const d = f.contentDocument;
            return !!d.querySelector(arguments[1]) && !!d.querySelector('[data-obx-splitter-handle]');
        """, frame_name, selector)), STARTUP_TIMEOUT)

    @classmethod
    def _wait_handle_top(cls) -> bool:
        """等壳页面自己的导航栏把手就位。"""
        return wait_until(lambda: bool(cls._js(
            "return !!document.querySelector('.nav-sidebar')"
            " && !!document.querySelector('[data-obx-splitter-handle]');")), STARTUP_TIMEOUT)

    @classmethod
    def _sidebar_width(cls, frame_name: str, selector: str) -> float | None:
        """被拖内边栏的当前渲染宽度（整数像素；没渲染出来时 None）。"""
        value = cls._js("""
            const f = document.querySelector('iframe[data-plugin-name="' + arguments[0] + '"]');
            const side = f && f.contentDocument && f.contentDocument.querySelector(arguments[1]);
            return side ? Math.round(side.getBoundingClientRect().width) : null;
        """, frame_name, selector)
        return value if value is None else int(value)

    @classmethod
    def _wait_settled(cls, frame_name: str, selector: str, expected: int) -> bool:
        """等内边栏的宽度过渡走完（取值不再变化）。

        必须等：`.view-sub-sidebar` 有 120ms 的宽度过渡（`--obx-splitter-motion`），
        松手或改设置后立刻读 `getBoundingClientRect()` 拿到的是**过渡中间**的值，
        于是"界面上的宽度"与"落盘的值"看起来对不上 —— 那是量早了，不是没落盘。
        """
        seen: list[int] = []

        def stable() -> bool:
            current = cls._sidebar_width(frame_name, selector)
            if current is None:
                return False
            seen.append(current)
            del seen[:-3]
            return len(seen) == 3 and len(set(seen)) == 1 and seen[-1] == expected

        return wait_until(stable, FLUSH_TIMEOUT)

    @classmethod
    def _nav_label(cls, plugin: str) -> str | None:
        """导航里对应某个插件的入口文案。

        每个候选入口都点一次、等它的 iframe 挂上来，再读那个 iframe 的
        `data-plugin-name` —— 这条链正是 `_open_plugin` 用的那条，因此对上的就是
        真实入口。刻意不按文案猜：displayName（"漫画阅读"）与目录名
        （manga-library）不是同一个字符串，写死文案会在改显示名时红掉，而那与
        分隔条无关。
        """
        for label in cls._js("return Array.from(document.querySelectorAll('.nav-item'))"
                             ".map(el => (el.textContent || '').trim())") or []:
            if not label or label == '设置':
                continue
            cls._js("Array.from(document.querySelectorAll('.nav-item'))"
                    f".find(el => el.textContent.trim() === {label!r}).click();")
            if not wait_until(lambda: cls._js("""
                    const frames = Array.from(document.querySelectorAll('iframe[data-plugin-name]'));
                    const shown = frames.filter(f => f.getBoundingClientRect().width > 0);
                    return shown.length > 0;
                    """), 8.0):
                continue
            current = cls._js("""
                const frames = Array.from(document.querySelectorAll('iframe[data-plugin-name]'));
                const shown = frames.filter(f => f.getBoundingClientRect().width > 0);
                return shown.length ? shown[0].getAttribute('data-plugin-name') : null;
            """)
            if current == plugin:
                return label
        return None

    def _open_plugin(self, label: str) -> str:
        """点左侧导航进入插件，等它的把手渲染出来，返回 iframe 的 data-plugin-name。"""
        self._js("Array.from(document.querySelectorAll('.nav-item'))"
                 f".find(e => e.textContent.includes('{label}')).click();")
        self.assertTrue(wait_until(lambda: self._active_plugin() is not None, STARTUP_TIMEOUT),
                        '点击导航后没有挂载任何插件 iframe')
        target = self._active_plugin()
        self.assertIn(target, PLUGIN_SIDEBARS, f'导航里的插件不在已知清单里：{target}')
        selector = PLUGIN_SIDEBARS[target][0]
        self.assertTrue(self._wait_handle(target, selector), f'{target} 的把手未在超时内渲染')
        # 交接完成：上一步的 iframe 可能还在（保活插件只是 v-show），但把手与采样器
        # 都挂在新文档上了才允许开始拖动
        time.sleep(0.4)
        return target

    def _dispatch_mouse(self, kind: str, x: float | None = None, y: float | None = None,
                        buttons: int = 0, clicks: int = 0) -> None:
        params = {'type': kind, 'button': 'left', 'buttons': buttons, 'clickCount': clicks}
        if x is not None:
            params['x'] = x
            params['y'] = y
        self.driver.execute_cdp_cmd('Input.dispatchMouseEvent', params)

    def _drag(self, start: dict, px: int = DRAG_PX, steps: int = DRAG_STEPS) -> list:
        """一次连续手势：按下 → 多步移动 → 抬起，返回拖动期间的逐帧采样。"""
        x, y = start['x'], start['y']
        step = px // steps
        self._dispatch_mouse('mouseMoved', x, y)
        time.sleep(0.1)
        # 采样器是在页面侧用 rAF 起的：按下之前先确认它已经在跑，否则拖动结束才
        # 发现"一帧都没采到"，而那与"宽度没跟随"是两回事。
        self.assertTrue(wait_until(lambda: self._js('return window.__samples.length;') > 2, 5.0),
                        '采样器没有启动（页面侧 rAF 没跑起来）')
        self._dispatch_mouse('mousePressed', x, y, buttons=1, clicks=1)
        for index in range(1, steps + 1):
            self._dispatch_mouse('mouseMoved', x + step * index, y, buttons=1)
            time.sleep(0.09)
        self._dispatch_mouse('mouseReleased', x + step * steps, y, clicks=1)
        time.sleep(0.6)
        return self._js('return window.__samples;') or []

    # ------------------------------------------------------------------ 用例

    def test_shell_nav_sidebar_drag_persists(self):
        """壳自己的导航栏：拖动改的是 --nav-width，松手落盘。"""
        self.driver.get(self.instance.base_url)
        self.assertTrue(self._wait_handle_top(), '壳导航栏的把手未在超时内渲染')
        info = self._js("""
            const side = document.querySelector('.nav-sidebar');
            const handle = document.querySelector('[data-obx-splitter-handle]');
            const r = handle.getBoundingClientRect();
            window.__samples = [];
            const tick = () => {
              window.__samples.push({w: +side.getBoundingClientRect().width.toFixed(1)});
              if (window.__samples.length < 600) requestAnimationFrame(tick);
            };
            requestAnimationFrame(tick);
            return {width: side.getBoundingClientRect().width,
                    x: r.left + r.width / 2, y: r.top + r.height / 2,
                    flex: getComputedStyle(side).flex,
                    transition: getComputedStyle(side).transitionDuration};
        """)
        self.assertTrue(info and info['width'] > 0, '壳导航栏没有渲染出来')
        # 不收缩：与 .settings-nav / .view-sub-sidebar 一致。缺了它，窗口变窄时渲染
        # 宽度会小于 --nav-width，拖动结果与设置页滑块就都对不上。
        self.assertIn('0', info['flex'].split()[1],
                      f'.nav-sidebar 未禁止收缩: flex={info["flex"]}')
        # 宽度过渡走 splitter.css 的共享 token：拖动期间被 .obx-splitter-live 停掉，
        # 静止时是 120ms（松手、复位、设置页改值都滑到位）
        self.assertEqual(info['transition'], '0.12s',
                         f'导航栏宽度过渡未接上 --obx-splitter-motion: {info["transition"]}')

        samples = self._drag(info, px=60, steps=6)
        widths = [s['w'] for s in samples]
        self.assertGreaterEqual(
            len(set(widths)), 3,
            f'拖动期间导航栏宽度没有逐步跟随（采样 {widths}）；'
            f'壳导航栏的 max 是 320px，起手值必须留出足够余量')

        final = self._js("return document.querySelector('.nav-sidebar')"
                         ".getBoundingClientRect().width;")
        self.assertGreater(final, info['width'], '松手后导航栏宽度没有变宽')
        key, expect = '--nav-width', f'{round(final)}px'
        self.assertTrue(self._wait_colors(key, expect),
                        f'{key} 未落盘为 {expect}，实际 {self._saved_colors().get(key)!r}')

    def test_plugin_sidebar_follows_pointer_every_step(self):
        """插件内边栏必须**逐帧**跟随指针，而不是松手才跳一次。

        这条锁的是 group-mesh 那个缺陷：`--gm-side-width` 在 `:root` 上取别名，
        `var()` 在那里就替换完了，分隔条写在元素内联上的值读不到 —— 拖动全程宽度
        不动、松手（壳写 `:root`）才跳一下。首尾两次取值都"正确"，所以只能数
        拖动过程中的变化次数。
        """
        labels = {'document-reader': '文档阅读', 'group-mesh': '团体组网'}
        for name, label in labels.items():
            with self.subTest(plugin=name):
                selector, var_name = PLUGIN_SIDEBARS[name]
                opened = self._open_plugin(label)
                self.assertEqual(opened, name, f'点「{label}」进入的是 {opened}')
                start = self._js(_sampler_js(name, selector, var_name), name, selector, var_name)
                self.assertNotIn('err', start, f'{name}: {start}')
                samples = self._drag(start)
                widths = [s['w'] for s in samples]
                self.assertGreaterEqual(
                    len(set(widths)), 3,
                    f'{name} 拖动期间宽度没有逐步跟随（{len(widths)} 帧里只有 '
                    f'{sorted(set(widths))}）；这是"别名写在 :root 上"那类缺陷的形状')
                # 拖动期间内核会挂 live 标记把过渡停掉：留着过渡会落后指针半拍，
                # 而且是"松开鼠标才追上"的那种手感（高度过渡的插件另有约定）
                self.assertTrue(any(s['live'] for s in samples),
                                f'{name} 拖动期间没有挂 .obx-splitter-live')
                # 停掉过渡的判据是"时长归零"：transition-property 会变成 none，
                # 但真正决定滞后与否的是 duration —— 只看属性名会漏掉 duration 仍在的情况
                self.assertEqual({s['transition'] for s in samples if s['live']}, {'0s'},
                                 f'{name} 拖动期间过渡时长没有归零')
                self.assertTrue(any(s['dragging'] for s in samples),
                                f'{name} 拖动期间 html 上没有 data-obx-dragging')

    def test_plugin_sidebar_drag_persists_shared_width(self):
        """插件内边栏的拖动落盘，并且五个插件共用同一支变量。

        同步开着（默认）时，任何一个插件拖出来的宽度都写进共享的
        `--sub-sidebar-width`，因此切到另一个插件应该拿到同一个宽度。
        """
        self._open_plugin('团体组网')
        selector, var_name = PLUGIN_SIDEBARS['group-mesh']
        start = self._js(_sampler_js('group-mesh', selector, var_name),
                         'group-mesh', selector, var_name)
        self.assertNotIn('err', start, f'group-mesh: {start}')
        self._drag(start)
        expect = f'{self._sidebar_width("group-mesh", selector)}px'
        self.assertTrue(self._wait_colors(var_name, expect),
                        f'{var_name} 未落盘为 {expect}，实际 {self._saved_colors().get(var_name)!r}')

        # 同步默认开着：另一个插件拿到的是同一个值，且没有写分插件变量
        self._open_plugin('文档阅读')
        other = self._js("""
            const d = document.querySelector('iframe[data-plugin-name="document-reader"]').contentDocument;
            const side = d.querySelector('.nr-sidebar');
            return {rootVar: d.documentElement.style.getPropertyValue('--sub-sidebar-width'),
                    width: +side.getBoundingClientRect().width.toFixed(0)};
        """)
        self.assertEqual(other['rootVar'], expect,
                         f'另一个插件没有同步到共享宽度：{other["rootVar"]!r} != {expect}')
        self.assertNotIn('--sub-sidebar-width--group-mesh', self._saved_colors(),
                         '同步开着时不该写下分插件变量')

    def test_splitter_handle_takes_no_layout_space(self):
        """把手净占位不得超过 0：否则被拖元素与下一个兄弟之间会露出祖先背景。

        量的是"被拖元素的右缘"与"下一个兄弟的左缘"：1px 的正差就是用户看到的竖缝
        （玻璃模式下透出的是背景图）。差值为 0 或略负都可以 —— 负值说明热区压在
        交界内侧，邻居是直接相接的；只有正差才是缝。取未取整的 rect：取整会把
        0.x 的错位抹平。
        """
        opened = self._open_plugin('团体组网')
        gaps = self._js("""
            const f = document.querySelector('iframe[data-plugin-name="' + arguments[0] + '"]');
            const d = f.contentDocument;
            const side = d.querySelector(arguments[1]);
            const handle = d.querySelector('[data-obx-splitter-handle]');
            const body = side.nextElementSibling === handle
              ? handle.nextElementSibling : side.nextElementSibling;
            const s = side.getBoundingClientRect(), b = body.getBoundingClientRect();
            return {gap: +(b.left - s.right).toFixed(3),
                    handleWidth: +handle.getBoundingClientRect().width.toFixed(2),
                    handleMargin: d.defaultView.getComputedStyle(handle).marginLeft,
                    handle: handle.className};
        """, opened, PLUGIN_SIDEBARS[opened][0])
        self.assertLessEqual(gaps['gap'], 0.0,
                             f'{opened} 的被拖元素与下一个兄弟之间有 {gaps["gap"]}px 的缝'
                             f'（把手 {gaps["handle"]} margin-left={gaps["handleMargin"]}）')

    def test_sidebar_width_setting_drives_all_plugins(self):
        """设置页的宽度滑块与「恢复默认宽度」写的是同一支共享变量。

        这条锁的是"设置项必须真的落到界面上"：滑块改的是 `--sub-sidebar-width`，
        而插件内边栏宽度由壳的 `.view-sub-sidebar` 读它 —— 两处一旦脱钩，界面会停在
        旧宽度而设置页显示新值。
        """
        self.driver.get(self.instance.base_url + '/settings')
        time.sleep(1.0)
        setter = self._js("""
            const el = document.getElementById('sub-sidebar-width');
            return el ? {min: el.min, max: el.max, value: el.value} : null;
        """)
        self.assertIsNotNone(setter, '设置页没有渲染「各插件内边栏宽度」滑块')
        self.assertEqual(int(setter['min']), 180)
        self.assertEqual(int(setter['max']), 480)

        # 用原生事件触发 v-model：直接改 value 不会通知 Vue
        self._js("""
            const el = document.getElementById('sub-sidebar-width');
            el.value = '300';
            el.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        self.assertTrue(self._wait_colors('--sub-sidebar-width', '300px'),
                        f'滑块未落盘：{self._saved_colors().get("--sub-sidebar-width")!r}')

        # 恢复默认：共享值回到 240，并清掉分插件偏离
        self._js("document.getElementById('sub-sidebar-reset').click();")
        self.assertTrue(self._wait_colors('--sub-sidebar-width', '240px'),
                        f'复位后不是默认 240：{self._saved_colors().get("--sub-sidebar-width")!r}')

    def test_sidebar_width_setting_applies_to_every_plugin(self):
        """设置页改一次宽度，其余插件的内边栏都要跟着变。

        共享变量只有一个取值，因此"改了设置但某个插件没跟上"只可能是那条广播链断了：
        壳偏好 -> data-custom-colors -> 插件页根元素 -> .view-sub-sidebar。
        """
        self.driver.get(self.instance.base_url + '/settings')
        self.assertTrue(wait_until(lambda: bool(self._js(
            "return !!document.getElementById('sub-sidebar-width');")), STARTUP_TIMEOUT),
            '设置页没有渲染「各插件内边栏宽度」滑块')
        self._js("""
            const el = document.getElementById('sub-sidebar-width');
            el.value = '320';
            el.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        self.assertTrue(self._wait_colors('--sub-sidebar-width', '320px'),
                        f'滑块未落盘：{self._saved_colors().get("--sub-sidebar-width")!r}')

        # 逐个插件确认：宽度过渡要走完，因此用 _wait_settled 而不是读一次。
        # 标签从导航里读实际文案，不写死 —— 插件的 displayName 与目录名不是一回事。
        labels = {name: self._nav_label(name) for name in PLUGIN_SIDEBARS
                  if name != 'group-mesh'}
        for name, label in labels.items():
            with self.subTest(plugin=name):
                self.assertIsNotNone(label, f'导航里找不到 {name} 对应的入口')
                self.driver.get(self.instance.base_url)
                self.assertTrue(self._wait_handle_top(), '壳导航栏未在超时内就绪')
                opened = self._open_plugin(label)
                self.assertEqual(opened, name)
                selector = PLUGIN_SIDEBARS[opened][0]
                self.assertTrue(self._wait_settled(opened, selector, 320),
                                f'{opened} 的内边栏没有跟到 320px（实际 '
                                f'{self._sidebar_width(opened, selector)}px）')

    def test_empty_pagination_bar_takes_no_space(self):
        """没有页码时 `.pagination-bar` 不占位。

        `createPagination.render()` 在 `totalPages <= 1` 时直接返回，容器留在文档里是
        空的；而 `.pagination-bar` 自己有 48px 高、一条上边框与面板底色，于是只够
        一页的目录会在页面底部多出一条 48px 的深色横带（用户报的"未知黑边"）。
        用 `:empty` 消掉，插件的 `innerHTML = ''` 也一并覆盖到。
        """
        opened = self._open_plugin(self._nav_label('image-viewer') or '图片相册')
        self.assertEqual(opened, 'image-viewer')
        info = self._js("""
            const f = document.querySelector('iframe[data-plugin-name="image-viewer"]');
            const d = f.contentDocument;
            const bar = d.getElementById('pagination');
            if (!bar) return {err: '没有分页栏'};
            const cs = d.defaultView.getComputedStyle(bar);
            return {children: bar.childElementCount,
                    height: +bar.getBoundingClientRect().height.toFixed(1),
                    border: cs.borderTopWidth, bg: cs.backgroundColor};
        """)
        self.assertNotIn('err', info, f'{info}')
        if info['children'] == 0:
            self.assertEqual(info['height'], 0.0,
                             f'空分页栏仍占 {info["height"]}px（底色 {info["bg"]}'
                             f'，上边框 {info["border"]}）')

    def test_sidebar_width_is_set_before_first_paint(self):
        """插件页的侧栏宽度写在服务端返回的 HTML 里，不靠加载后的异步广播。

        靠广播时首帧一定先按 `variables.css` 的默认值画一遍，再跳到用户值；叠上
        120ms 宽度过渡就是"先是一个固定值、再滑到设置的值"，切换插件时反复出现。
        这里直接问 `serve_plugin_frontend` 要一次 HTML 确认它带了变量，再从浏览器侧
        量首帧宽度。
        """
        self.driver.get(self.instance.base_url + '/settings')
        self.assertTrue(wait_until(lambda: bool(self._js(
            "return !!document.getElementById('sub-sidebar-width');")), STARTUP_TIMEOUT),
            '设置页没有渲染「各插件内边栏宽度」滑块')
        self._js("""
            const el = document.getElementById('sub-sidebar-width');
            el.value = '360';
            el.dispatchEvent(new Event('input', {bubbles: true}));
        """)
        self.assertTrue(self._wait_colors('--sub-sidebar-width', '360px'), '宽度未落盘')

        served = self._js("""
            const xhr = new XMLHttpRequest();
            xhr.open('GET', '/plugins/group-mesh/frontend/index.html?cb=' + Date.now(), false);
            xhr.send();
            const m = /<html[^>]*>/.exec(xhr.responseText);
            return JSON.stringify({tag: m ? m[0] : null,
                                   cache: xhr.getResponseHeader('Cache-Control')});
        """)
        self.assertIn('--sub-sidebar-width:360px', served,
                      f'服务端返回的 HTML 未带外观变量：{served}')
        # 必须 no-store：这份 HTML 带着**当时**的变量，缓存住会让改动延迟生效
        self.assertIn('no-store', served, f'插件 HTML 不应该被缓存：{served}')

        self.driver.get(self.instance.base_url)
        self.assertTrue(self._wait_handle_top(), '壳导航栏未在超时内就绪')
        label = self._nav_label('group-mesh') or '团体组网'
        opened = self._open_plugin(label)
        self.assertEqual(opened, 'group-mesh')
        selector = PLUGIN_SIDEBARS['group-mesh'][0]
        # 一挂上就该是 360，不该出现"先 240 再滑过去"的中间值
        seen = set()
        for _ in range(6):
            seen.add(self._sidebar_width('group-mesh', selector))
            time.sleep(0.05)
        self.assertEqual(seen, {360}, f'首帧后出现过中间宽度：{sorted(seen)}')

    def test_no_console_errors_while_dragging(self):
        """拖动全程不应出现 console.error / 未捕获异常。"""
        self._open_plugin('团体组网')
        selector, var_name = PLUGIN_SIDEBARS['group-mesh']
        start = self._js(_sampler_js('group-mesh', selector, var_name),
                         'group-mesh', selector, var_name)
        self._drag(start)
        logs = self.driver.get_log('browser')
        severe = [entry for entry in logs if entry.get('level') == 'SEVERE']
        self.assertEqual(severe, [], f'拖动期间出现浏览器错误：{severe}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
