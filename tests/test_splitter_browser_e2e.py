"""分隔条（可变边栏）的真实浏览器端到端用例（本地手动运行，不进 CI）。

为什么需要它
-----------
插件页面骨架已迁到壳：侧栏是壳文档里的 `.plugin-chrome-sidebar`，分隔条、拖动落盘与
`--sub-sidebar-width` 都由壳持有；内容 iframe 里不再有 `.iv-sidebar` / `.gm-side` /
`.nr-sidebar` 一类的插件自带侧栏。`tools/plugin_ui_audit.py` 覆盖"壳渲染了骨架、内容页
没有 chrome 标记、hover 指示线"；本文件补它不覆盖的部分：

  - **拖动是否逐帧跟随指针**：把别名写在 `:root` 上的实现会"拖动全程宽度不动、松手才
    跳一下"，只看拖动前后两次取值抓不到。
  - **把手占位**：`.obx-splitter-x` 的 5px 热区靠负外边距压在交界处，净占位为正时那一列
    谁都没上色，透出祖先背景（玻璃模式下就是背景图本身）——用户看到的"1px 竖缝"。
  - **落盘时机**：拖动过程去抖、松手立刻落盘，写错时表现为"松手后宽度弹回上一次的值"。
  - **设置页滑块与拖动共用同一支变量**，改一次要在所有插件页面上生效。

做法
----
起隔离实例（`OMNIBOX_HOME` 指向临时目录，不碰开发机的偏好）+ 无头 Chrome，用 CDP 的
`Input.dispatchMouseEvent` 投递**一次连续手势**：WebDriver 的 Actions 分批 `perform()`
就不是一次手势了，无法在手势中间读值。页面侧在拖动期间用 rAF 逐帧采样，手势结束后
读回整条宽度序列。

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


def _sampler_js(frame_name: str, var_name: str) -> str:
    """在壳文档里给插件侧栏开逐帧采样器，返回把手中心（顶层视口坐标）与起始宽度。

    侧栏现在是壳渲染的 `.plugin-chrome-sidebar`，与把手同在壳文档；采样因此不需要
    跨 iframe 读 `contentDocument`。
    """
    return """
    const name = arguments[0], varName = arguments[1];
    const frame = document.querySelector('iframe[data-plugin-name="' + name + '"]');
    const container = frame && (frame.closest('.plugin-frame-container') || frame.parentElement);
    const page = container && container.querySelector('.plugin-page');
    const side = page && page.querySelector('.plugin-chrome-sidebar');
    const handle = page && Array.from(page.querySelectorAll('[data-obx-splitter-handle]'))
      .find(h => h.previousElementSibling === side);
    if (!side || !handle) return {err: '侧栏或把手还没渲染', hasSide: !!side, hasHandle: !!handle};
    const cs = getComputedStyle(side);
    window.__samples = [];
    const tick = () => {
      window.__samples.push({
        w: +side.getBoundingClientRect().width.toFixed(1),
        elVar: side.style.getPropertyValue(varName) || null,
        rootVar: document.documentElement.style.getPropertyValue(varName) || null,
        live: side.classList.contains('obx-splitter-live'),
        transition: cs.transitionDuration,
        dragging: document.documentElement.getAttribute('data-obx-dragging'),
      });
      if (window.__samples.length < 600) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    const r = handle.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2,
            width: +side.getBoundingClientRect().width.toFixed(1),
            handleWidth: +r.width.toFixed(2), handleLeft: +r.left.toFixed(2)};
    """


@unittest.skipIf(SKIP_REASON, SKIP_REASON or '')
class SplitterBrowserE2ETests(unittest.TestCase):
    """真实 Chrome + 隔离实例，核对壳侧栏拖动、落盘与占位。"""

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
    def _wait_handle(cls, frame_name: str) -> bool:
        """等壳在插件页面里挂出侧栏与把手。"""
        return wait_until(lambda: bool(cls._js("""
            const frame = document.querySelector('iframe[data-plugin-name="' + arguments[0] + '"]');
            const container = frame && (frame.closest('.plugin-frame-container') || frame.parentElement);
            const page = container && container.querySelector('.plugin-page');
            const side = page && page.querySelector('.plugin-chrome-sidebar');
            return !!side && Array.from(page.querySelectorAll('[data-obx-splitter-handle]'))
              .some(h => h.previousElementSibling === side);
        """, frame_name)), STARTUP_TIMEOUT)

    @classmethod
    def _wait_handle_top(cls) -> bool:
        """等壳页面自己的导航栏把手就位。"""
        return wait_until(lambda: bool(cls._js(
            "return !!document.querySelector('.nav-sidebar')"
            " && !!document.querySelector('[data-obx-splitter-handle]');")), STARTUP_TIMEOUT)

    @classmethod
    def _sidebar_width(cls, frame_name: str) -> float | None:
        """插件侧栏（壳渲染）的 CSS 宽度；没渲染出来时 None。

        用计算样式的 `width` 而不是 `getBoundingClientRect().width`：侧栏带 1px
        右边框，rect 比 `--sub-sidebar-width` 多 1px，而分隔条落盘的是 CSS 宽度。
        """
        value = cls._js("""
            const frame = document.querySelector('iframe[data-plugin-name="' + arguments[0] + '"]');
            const container = frame && (frame.closest('.plugin-frame-container') || frame.parentElement);
            const side = container && container.querySelector('.plugin-chrome-sidebar');
            return side ? Math.round(parseFloat(getComputedStyle(side).width)) : null;
        """, frame_name)
        return value if value is None else int(value)

    @classmethod
    def _wait_settled(cls, frame_name: str, expected: int) -> bool:
        """等插件侧栏宽度落到期望值（取值连续三次一致）。"""
        seen: list[int] = []

        def stable() -> bool:
            current = cls._sidebar_width(frame_name)
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
        真实入口。刻意不按文案猜：displayName 与目录名不是同一个字符串，写死文案会在
        改显示名时红掉，而那与分隔条无关。
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
        """点左侧导航进入插件，等壳给它渲染出侧栏把手，返回 iframe 的 data-plugin-name。"""
        self._js("Array.from(document.querySelectorAll('.nav-item'))"
                 f".find(e => e.textContent.includes('{label}')).click();")
        self.assertTrue(wait_until(lambda: self._active_plugin() is not None, STARTUP_TIMEOUT),
                        '点击导航后没有挂载任何插件 iframe')
        target = self._active_plugin()
        self.assertTrue(self._wait_handle(target), f'{target} 的壳侧栏把手未在超时内渲染')
        # 交接完成：上一步的 iframe 可能还在（保活插件只是 v-show），但把手与采样器
        # 都挂在新页面上了才允许开始拖动
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
        # 不收缩：缺了它，窗口变窄时渲染宽度会小于 --nav-width，拖动结果与设置页
        # 滑块就都对不上。
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

    def test_plugin_chrome_sidebar_follows_pointer_every_step(self):
        """壳渲染的插件侧栏必须**逐帧**跟随指针，而不是松手才跳一次。

        这条锁的是"别名写在 :root 上"那类缺陷：`var()` 在 `:root` 上就替换完了，
        分隔条写在元素内联上的值读不到 —— 拖动全程宽度不动、松手才跳一下。首尾两次
        取值都"正确"，所以只能数拖动过程中的变化次数。
        """
        labels = {}
        for name in ('document-reader', 'group-mesh'):
            label = self._nav_label(name)
            self.assertIsNotNone(label, f'导航里找不到 {name} 对应的入口')
            labels[name] = label
        for name, label in labels.items():
            with self.subTest(plugin=name):
                opened = self._open_plugin(label)
                self.assertEqual(opened, name, f'点「{label}」进入的是 {opened}')
                start = self._js(_sampler_js(name, '--sub-sidebar-width'), name,
                                 '--sub-sidebar-width')
                self.assertNotIn('err', start, f'{name}: {start}')
                samples = self._drag(start)
                widths = [s['w'] for s in samples]
                self.assertGreaterEqual(
                    len(set(widths)), 3,
                    f'{name} 拖动期间宽度没有逐步跟随（{len(widths)} 帧里只有 '
                    f'{sorted(set(widths))}）；这是"别名写在 :root 上"那类缺陷的形状')
                # 拖动期间内核会挂 live 标记把过渡停掉：留着过渡会落后指针半拍
                self.assertTrue(any(s['live'] for s in samples),
                                f'{name} 拖动期间没有挂 .obx-splitter-live')
                self.assertTrue(any(s['dragging'] for s in samples),
                                f'{name} 拖动期间 html 上没有 data-obx-dragging')

    def test_plugin_sidebar_drag_persists_shared_width(self):
        """插件侧栏的拖动落盘，并且所有插件共用同一支变量。

        同步开着（默认）时，任何一个插件拖出来的宽度都写进共享的
        `--sub-sidebar-width`，因此切到另一个插件应该拿到同一个宽度。
        """
        label = self._nav_label('group-mesh')
        self.assertIsNotNone(label, '导航里找不到 group-mesh 对应的入口')
        opened = self._open_plugin(label)
        self.assertEqual(opened, 'group-mesh')
        start = self._js(_sampler_js('group-mesh', '--sub-sidebar-width'),
                         'group-mesh', '--sub-sidebar-width')
        self.assertNotIn('err', start, f'group-mesh: {start}')
        self._drag(start)
        expect = f'{self._sidebar_width("group-mesh")}px'
        self.assertTrue(self._wait_colors('--sub-sidebar-width', expect),
                        f'--sub-sidebar-width 未落盘为 {expect}，'
                        f'实际 {self._saved_colors().get("--sub-sidebar-width")!r}')

        # 同步默认开着：另一个插件拿到的是同一个值，且没有写分插件变量
        other_label = self._nav_label('document-reader')
        self.assertIsNotNone(other_label, '导航里找不到 document-reader 对应的入口')
        self.assertEqual(self._open_plugin(other_label), 'document-reader')
        self.assertTrue(self._wait_settled('document-reader', int(expect.removesuffix('px'))),
                        f'另一个插件没有同步到共享宽度：'
                        f'{self._sidebar_width("document-reader")}px != {expect}')
        self.assertNotIn('--sub-sidebar-width--group-mesh', self._saved_colors(),
                         '同步开着时不该写下分插件变量')

    def test_splitter_handle_takes_no_layout_space(self):
        """把手净占位不得超过 0：否则被拖元素与下一个兄弟之间会露出祖先背景。

        量的是"侧栏右缘"与"下一个兄弟（.plugin-main）的左缘"：1px 的正差就是用户看到
        的竖缝（玻璃模式下透出的是背景图）。差值为 0 或略负都可以 —— 负值说明热区压在
        交界内侧，邻居是直接相接的；只有正差才是缝。
        """
        label = self._nav_label('group-mesh')
        self.assertIsNotNone(label, '导航里找不到 group-mesh 对应的入口')
        opened = self._open_plugin(label)
        gaps = self._js("""
            const frame = document.querySelector('iframe[data-plugin-name="' + arguments[0] + '"]');
            const container = frame.closest('.plugin-frame-container') || frame.parentElement;
            const page = container.querySelector('.plugin-page');
            const side = page.querySelector('.plugin-chrome-sidebar');
            const handle = Array.from(page.querySelectorAll('[data-obx-splitter-handle]'))
              .find(h => h.previousElementSibling === side);
            const body = handle.nextElementSibling || side.nextElementSibling;
            const s = side.getBoundingClientRect(), b = body.getBoundingClientRect();
            return {gap: +(b.left - s.right).toFixed(3),
                    handleWidth: +handle.getBoundingClientRect().width.toFixed(2),
                    handleMargin: getComputedStyle(handle).marginLeft,
                    handle: handle.className};
        """, opened)
        self.assertLessEqual(gaps['gap'], 0.0,
                             f'{opened} 的侧栏与主区之间有 {gaps["gap"]}px 的缝'
                             f'（把手 {gaps["handle"]} margin-left={gaps["handleMargin"]}）')

    def test_sidebar_width_setting_drives_all_plugins(self):
        """设置页的宽度滑块与「恢复默认宽度」写的是同一支共享变量。

        这条锁的是"设置项必须真的落到界面上"：滑块改的是 `--sub-sidebar-width`，
        而插件侧栏宽度由壳的 `.plugin-chrome-sidebar` 读它 —— 两处一旦脱钩，界面会停在
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
        """设置页改一次宽度，所有插件的壳侧栏都要跟着变。

        共享变量只有一个取值，因此"改了设置但某个插件没跟上"只可能是那条广播链断了：
        壳偏好 -> data-custom-colors -> .plugin-chrome-sidebar。
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

        # 标签从导航里读实际文案，不写死 —— 插件的 displayName 与目录名不是一回事。
        labels = {name: self._nav_label(name) for name in (
            'document-reader', 'group-mesh', 'image-viewer', 'manga-library', 'media-player')}
        for name, label in labels.items():
            with self.subTest(plugin=name):
                self.assertIsNotNone(label, f'导航里找不到 {name} 对应的入口')
                self.driver.get(self.instance.base_url)
                self.assertTrue(self._wait_handle_top(), '壳导航栏未在超时内就绪')
                opened = self._open_plugin(label)
                self.assertEqual(opened, name)
                self.assertTrue(self._wait_settled(opened, 320),
                                f'{opened} 的壳侧栏没有跟到 320px（实际 '
                                f'{self._sidebar_width(opened)}px）')

    def test_no_console_errors_while_dragging(self):
        """拖动全程不应出现 console.error / 未捕获异常。"""
        label = self._nav_label('group-mesh')
        self.assertIsNotNone(label, '导航里找不到 group-mesh 对应的入口')
        self._open_plugin(label)
        start = self._js(_sampler_js('group-mesh', '--sub-sidebar-width'),
                         'group-mesh', '--sub-sidebar-width')
        self._drag(start)
        logs = self.driver.get_log('browser')
        severe = [entry for entry in logs if entry.get('level') == 'SEVERE']
        self.assertEqual(severe, [], f'拖动期间出现浏览器错误：{severe}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
