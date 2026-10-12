"""壳「背景」面板的真实浏览器端到端用例（本地手动运行，不进 CI）。

为什么需要它
------------
背景图不再是 `.app-shell` 自己的 background-image，而是画在 `.app-shell::before`
上的一层：显示方式、自由位置、缩放与翻转全都落成这条伪元素上的 CSS。
"变量写了但界面没变"这类缺陷在 Python 单测与 DOM 桩里看不出来，实证的两种形态：

  - `.app-shell` 少了 `isolation`，`z-index: -1` 的背景层会跑到 html/body 底色之后，
    整张背景图消失，而控制台不报任何错；
  - 图片库若不是 `/file` 的允许根，网格里的 `<img>` 只是空白，接口与设置值全都正常。

因此本用例驱动真实 Chrome，断言「导入图片 → 网格渲染 → 计算样式 → 切换显示方式 →
透过玻璃面板看到背景」这条链路。像素断言单独一条：它同时覆盖"背景层画在了正确的位置"
与"玻璃把它透了出来"。

本地运行
--------
    venv/Scripts/pip install -r requirements-e2e.txt
    venv/Scripts/python -m unittest tests.test_shell_background_browser_e2e -v

依赖：本机已安装 Chrome（Edge 不算，理由见 `tests/harness/browser_binary.py`）与
selenium 4.6+（自带 driver 管理，自动下载驱动）。
"""

from __future__ import annotations

import io
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

BOOT_OK, BOOT_REASON = boot_prerequisites()


def _have_selenium() -> bool:
    try:
        import selenium  # noqa: F401
        from selenium import webdriver  # noqa: F401
    except Exception:
        return False
    return True


def _write_pattern_png(path: Path, size: int = 64) -> None:
    """写一张"外圈绿、中心红"的 PNG。

    图案而不是纯色：缩放是否缩放图片本身，只能靠"画面里还能不能看到图片的外圈"来
    判断 —— 把已经裁好的那一块整体缩小，外圈早在裁切时就丢掉了。
    中心是纯红，玻璃的像素断言因此仍然只看红蓝差。
    """
    from PIL import Image, ImageDraw

    image = Image.new('RGB', (size, size), (0, 200, 0))
    quarter = size // 4
    ImageDraw.Draw(image).rectangle(
        [quarter, quarter, size - quarter - 1, size - quarter - 1], fill=(255, 0, 0))
    image.save(path)


@unittest.skipUnless(_have_selenium(), '未安装 selenium（见 requirements-e2e.txt）')
@unittest.skipUnless(browser_binary() is not None, '未检测到 Chrome，跳过真实浏览器端到端用例')
@unittest.skipUnless(BOOT_OK, BOOT_REASON)
class ShellBackgroundBrowserE2ETests(unittest.TestCase):
    """起一个隔离实例 + 无头 Chrome，验证「背景」面板的完整链路。"""

    driver = None
    instance = None

    @classmethod
    def setUpClass(cls):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        # `OMNIBOX_HOME` 隔离（见 tests/harness/app_instance.py）：导入的图片会写进
        # `<user_data>/.config/backgrounds`，不隔离就会往开发机的真实配置目录里复制。
        cls.instance = AppInstance('background', root).start()
        cls.source_image = root / 'pattern.png'
        _write_pattern_png(cls.source_image)

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

        # 打开首页时壳会种下 HttpOnly 的令牌 Cookie（file_server._attach_token_cookie），
        # 因此页面内的 fetch / <img> 都自动带凭据，不需要手工塞令牌。
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

    @classmethod
    def _style_var(cls, name: str) -> str:
        """html 内联上的 CSS 变量值（background.ts 写的就是它）。"""
        return str(cls._js(
            "return document.documentElement.style.getPropertyValue(arguments[0]);", name)).strip()

    @classmethod
    def _pseudo_style(cls) -> dict:
        return cls._js("""
            const el = document.querySelector('.app-shell');
            const s = getComputedStyle(el, '::before');
            return { backgroundImage: s.backgroundImage, backgroundSize: s.backgroundSize,
                     backgroundPosition: s.backgroundPosition, transform: s.transform,
                     zIndex: s.zIndex, content: s.content };
        """)

    @classmethod
    def _computed_bg_size(cls) -> tuple:
        """背景层实际渲染出的尺寸（浏览器已把关键字解析成像素）。"""
        value = str(cls._js("""
            const s = getComputedStyle(document.querySelector('.app-shell'), '::before');
            return s.backgroundSize;
        """)).replace('px', '')
        parts = value.split()
        return (float(parts[0]), float(parts[1]))

    @classmethod
    def _viewport(cls) -> dict:
        return cls._js("return { w: window.innerWidth, h: window.innerHeight };")

    def _natural_size(self) -> tuple:
        from PIL import Image

        with Image.open(self.source_image) as image:
            return image.size

    def _screenshot(self):
        from PIL import Image

        return Image.open(io.BytesIO(self.driver.get_screenshot_as_png())).convert('RGB')

    def _panel_title(self) -> str:
        return str(self._js(
            "const n = document.querySelector('.settings-panel .panel-title');"
            "return n ? n.textContent.trim() : '';"))

    def _open_settings(self):
        clicked = self._js("""
            const hit = Array.from(document.querySelectorAll('.nav-item'))
              .find(e => e.textContent.includes('设置'));
            if (!hit) return false; hit.click(); return true;
        """)
        self.assertTrue(clicked, '找不到「设置」导航项')
        self.assertTrue(self._wait_for(
            lambda: bool(self._js("return !!document.querySelector('.settings-panel');"))),
            '设置页没有渲染出面板')

    def _open_section(self, label: str) -> str:
        clicked = self._js("""
            const hit = Array.from(document.querySelectorAll('.settings-nav-item'))
              .find(e => e.textContent.includes(arguments[0]));
            if (!hit) return false; hit.click(); return true;
        """, label)
        self.assertTrue(clicked, f'设置页左栏没有「{label}」分区')
        self.assertTrue(self._wait_for(lambda: self._panel_title() == label),
                        f'切到「{label}」失败，当前面板：{self._panel_title()}')
        return self._panel_title()

    def _import_image(self, path: Path) -> dict:
        """页面内调导入接口（网页模式没有系统文件对话框，选图只能这样模拟）。"""
        result = self.driver.execute_async_script("""
            const path = arguments[0]; const done = arguments[arguments.length - 1];
            fetch('/api/system_import_background_image', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ args: [path] }),
            }).then(r => r.json()).then(j => done(j))
              .catch(e => done({ error: String(e) }));
        """, str(path))
        self.assertNotIn('error', result or {}, f'导入接口返回错误: {result}')
        self.assertTrue((result or {}).get('result', {}).get('success'), f'导入失败: {result}')
        return result['result']['image']

    def _click_thumb(self, index: int = 0):
        clicked = self._js("""
            const items = Array.from(document.querySelectorAll('.bg-item .bg-thumb'));
            if (items.length <= arguments[0]) return false;
            items[arguments[0]].click(); return true;
        """, index)
        self.assertTrue(clicked, f'没有第 {index + 1} 张缩略图')

    def _click_active_thumb(self) -> bool:
        return bool(self._js("""
            const hit = document.querySelector('.bg-item.active .bg-thumb');
            if (!hit) return false; hit.click(); return true;
        """))

    def _active_image_name(self):
        return self._js("""
            const hit = document.querySelector('.bg-item.active .bg-thumb span');
            return hit ? hit.textContent.trim() : null;
        """)

    def _set_select(self, selector: str, value: str):
        self._js("""
            const el = document.querySelector(arguments[0]);
            el.value = arguments[1];
            el.dispatchEvent(new Event('change', { bubbles: true }));
        """, selector, value)

    def _set_range(self, selector: str, value: int):
        self._js("""
            const el = document.querySelector(arguments[0]);
            el.value = String(arguments[1]);
            el.dispatchEvent(new Event('input', { bubbles: true }));
        """, selector, value)

    def _ensure_background_selected(self) -> str:
        """保证页面上已有一张被选中的背景图，返回它的路径。

        用例之间共享同一个浏览器会话，因此这里必须幂等：已经选中就直接返回，
        否则导入一张并刷新（刷新会重新挂载设置页，从而重新列出图片库）。
        """
        current = self._style_var('--shell-bg-image')
        if current:
            return current
        image = self._import_image(self.source_image)
        self.driver.refresh()
        self.assertTrue(self._wait_for(lambda: bool(self._js(
            "return document.querySelectorAll('.nav-item').length > 0;")), STARTUP_TIMEOUT),
            '刷新后壳前端未就绪')
        self._open_settings()
        self._open_section('背景')
        path = self._js("""
            const items = Array.from(document.querySelectorAll('.bg-item .bg-thumb'));
            const hit = items.find(e => e.title === arguments[0]);
            if (!hit) return null; hit.click(); return hit.title;
        """, image['path'])
        self.assertIsNotNone(path, f'网格里没有刚导入的图片: {image}')
        self.assertTrue(self._wait_for(lambda: bool(self._style_var('--shell-bg-image'))),
                        '点缩略图后 html 上没有背景图 URL')
        return image['path']

    # ------------------------------------------------------------------ 用例

    def test_background_is_its_own_panel(self):
        """「背景」是独立分区（含背景图、显示方式与玻璃），「外观」不再包含这些字段。"""
        self._open_settings()
        self.assertEqual(self._open_section('背景'), '背景')
        body = self._js("return document.querySelector('.settings-panel-body').textContent;")
        for text in ('背景图片', '显示方式', '缩放', '翻转', '玻璃效果'):
            self.assertIn(text, body, f'「背景」面板缺少：{text}')

        self.assertEqual(self._open_section('外观'), '外观')
        appearance = self._js("return document.querySelector('.settings-panel-body').textContent;")
        self.assertNotIn('背景图片', appearance, '「外观」面板仍在渲染背景图字段')
        self.assertNotIn('玻璃效果', appearance, '「外观」面板仍在渲染玻璃效果字段')

    def test_library_entry_renders_and_switches(self):
        """导入的图片出现在网格里、缩略图真的解码出来（图片库是 `/file` 的允许根）。"""
        path = self._ensure_background_selected()
        self.assertTrue(self._wait_for(lambda: self._js("""
            const img = document.querySelector('.bg-item img');
            return !!img && img.complete && img.naturalWidth > 0;
        """)), '网格缩略图没有解码出内容（图片库可能不在 /file 的允许根里）')
        self.assertEqual(self._js(
            "return document.querySelectorAll('.bg-item.active').length;"), 1,
            '选中的图片没有被标记为 active')
        self.assertIn('path=', self._style_var('--shell-bg-image'))

        # 再导入一张，刷新后点它：背景必须切换过去（而不是原来那张的目录清单）
        second = self._import_image(self.source_image)
        self.assertNotEqual(second['path'], path, '同名图片应当落成另一个文件名')
        self.driver.refresh()
        self.assertTrue(self._wait_for(lambda: bool(self._js(
            "return document.querySelectorAll('.nav-item').length > 0;")), STARTUP_TIMEOUT))
        self._open_settings()
        self._open_section('背景')
        self.assertTrue(self._wait_for(lambda: self._js("""
            const items = Array.from(document.querySelectorAll('.bg-item .bg-thumb'));
            return items.some(e => e.title === arguments[0]);
        """, second['path'])), '刷新后图片库里看不到第二张图')
        self._js("""
            const hit = Array.from(document.querySelectorAll('.bg-item .bg-thumb'))
              .find(e => e.title === arguments[0]);
            hit.click();
        """, second['path'])
        self.assertTrue(self._wait_for(
            lambda: second['path'].split('\\')[-1].split('/')[-1]
            in self._style_var('--shell-bg-image')),
            f'切换后 html 上的背景 URL 还是旧的: {self._style_var("--shell-bg-image")}')

    def test_fit_modes_match_object_fit_semantics(self):
        """四种 object-fit 落到背景层的渲染尺寸：cover / contain 按窗口比例，fill 拉满，none 用原图尺寸。"""
        self._ensure_background_selected()
        self._open_section('背景')
        view = self._viewport()
        width, height = self._natural_size()
        cover = max(view['w'] / width, view['h'] / height)
        contain = min(view['w'] / width, view['h'] / height)
        expected = {
            'cover': (width * cover, height * cover),
            'contain': (width * contain, height * contain),
            'fill': (view['w'], view['h']),
            'none': (width, height),
        }
        for fit, (want_width, want_height) in expected.items():
            with self.subTest(fit=fit):
                self._set_select('#bg-fit', fit)
                self.assertTrue(self._wait_for(lambda w=want_width, h=want_height: (
                    abs(self._computed_bg_size()[0] - w) <= 2
                    and abs(self._computed_bg_size()[1] - h) <= 2)),
                    f'{fit} 的渲染尺寸不是 {want_width:.0f}×{want_height:.0f}: '
                    f'{self._computed_bg_size()}')
                self.assertEqual(self._style_var('--shell-bg-position'), 'center')

        self._set_select('#bg-fit', 'free')
        self.assertEqual(self._style_var('--shell-bg-position'), '50% 50%')

        # 背景层必须真的画在 .app-shell 之内（少了 isolation，负 z-index 会跑到
        # html/body 底色后面，整张图消失）
        shell = self._js("""
            const s = getComputedStyle(document.querySelector('.app-shell'));
            return { isolation: s.isolation, position: s.position };
        """)
        self.assertEqual(shell['isolation'], 'isolate', '.app-shell 没有形成独立层叠上下文')
        self.assertEqual(shell['position'], 'relative', '.app-shell 不是背景层的定位祖先')
        pseudo = self._pseudo_style()
        self.assertEqual(pseudo['zIndex'], '-1')
        self.assertNotEqual(pseudo['content'], 'none', '背景层伪元素没有生成')

    def test_free_mode_position_zoom_and_flip(self):
        """自由模式的位置滑块、缩放（重算图片尺寸）与翻转（走 transform）。"""
        self._ensure_background_selected()
        self._open_section('背景')
        self._set_select('#bg-fit', 'free')
        self.assertTrue(self._wait_for(
            lambda: self._js("return !!document.querySelector('#bg-pos-x');")),
            '自由模式没有出现位置滑块')

        self._set_range('#bg-pos-x', 100)
        self._set_range('#bg-pos-y', 0)
        self.assertTrue(self._wait_for(lambda: self._style_var('--shell-bg-position') == '100% 0%'),
                        f'位置没有生效: {self._style_var("--shell-bg-position")}')

        width, height = self._natural_size()
        self._set_range('#bg-zoom', 150)
        self.assertTrue(self._wait_for(lambda: (
            abs(self._computed_bg_size()[0] - width * 1.5) <= 2
            and abs(self._computed_bg_size()[1] - height * 1.5) <= 2)),
            f'自由模式的缩放没有按原图尺寸 ×1.5 重算: {self._computed_bg_size()}')
        self.assertNotIn('scale(', self._style_var('--shell-bg-transform'),
                         '缩放仍在走 transform：那缩放的是裁切结果，不是图片本身')

        self._js("document.querySelector('#bg-flip-x').click();")
        self.assertTrue(self._wait_for(
            lambda: 'scaleX(-1)' in self._style_var('--shell-bg-transform')),
            f'水平翻转没有生效: {self._style_var("--shell-bg-transform")}')
        self._js("document.querySelector('#bg-flip-y').click();")
        self.assertTrue(self._wait_for(
            lambda: 'scaleY(-1)' in self._style_var('--shell-bg-transform')),
            f'垂直翻转没有生效: {self._style_var("--shell-bg-transform")}')
        # 镜像绕容器中心，位置要取镜像点：100%/0% 翻转后是 0%/100%，否则画面会跳到
        # 图片的另一处
        self.assertTrue(self._wait_for(lambda: self._style_var('--shell-bg-position') == '0% 100%'),
                        f'翻转后位置没有取镜像点: {self._style_var("--shell-bg-position")}')

        # 翻转在「填充」这类非自由模式下同样可用（不是自由模式专属）
        self._set_select('#bg-fit', 'cover')
        self.assertIn('scaleX(-1)', self._style_var('--shell-bg-transform'))

        self._js("document.querySelector('#bg-reset').click();")
        self.assertTrue(self._wait_for(
            lambda: self._style_var('--shell-bg-transform') == 'none'),
            f'恢复默认显示方式没有清掉缩放/翻转: {self._style_var("--shell-bg-transform")}')
        self.assertEqual(self._style_var('--shell-bg-position'), 'center')

    def test_zoom_keeps_the_whole_image_visible(self):
        """缩放必须缩放图片本身：缩到 50% 时图片的外圈仍在画面里。

        "把裁切结果整体缩小"与"缩放图片"在数值上都会让渲染框变小，区别只在画面里
        还有没有图片的外圈：裁切发生在缩放之前，外圈一旦被裁掉就再也回不来。
        测试图的外圈是绿色、中心是红色，因此这一条能直接分开两种实现。
        """
        self._ensure_background_selected()
        self._open_section('背景')
        self._set_select('#bg-fit', 'cover')
        # 面板要足够透，否则采到的是面板底色而不是背景
        self._set_range('#glass-blur', 0)
        self._set_range('#glass-alpha', 30)

        view = self._viewport()
        width, height = self._natural_size()
        fit_scale = max(view['w'] / width, view['h'] / height)
        self._set_range('#bg-zoom', 50)
        self.assertTrue(self._wait_for(lambda: (
            abs(self._computed_bg_size()[0] - width * fit_scale / 2) <= 2)),
            f'缩放 50% 后渲染尺寸不是 cover 的一半: {self._computed_bg_size()}')
        time.sleep(0.5)

        rendered_width, rendered_height = self._computed_bg_size()
        image_left = (view['w'] - rendered_width) / 2
        image_top = (view['h'] - rendered_height) / 2
        # 外圈占图片的四分之一，取它中线上的一点：这里必须是绿色
        sample_x = int(image_left + rendered_width * 0.125)
        sample_y = int(image_top + rendered_height * 0.125)
        color = self._screenshot().getpixel((sample_x, sample_y))
        red, green, blue = color
        self.assertGreater(green, red + 60,
                           f'缩到 50% 后图片外圈不见了（{sample_x},{sample_y} 采到 {color}）：'
                           f'说明缩放作用在裁切结果上，而不是图片本身')
        self.assertGreater(green, blue + 60, f'采样点不是图片外圈的颜色: {color}')

    def _panel_average_color(self, rect: dict) -> tuple:
        """截屏并取设置面板中心区域的平均色。"""
        image = self._screenshot()
        left = int(rect['x'] + rect['width'] * 0.35)
        top = int(rect['y'] + rect['height'] * 0.35)
        right = int(rect['x'] + rect['width'] * 0.65)
        bottom = int(rect['y'] + rect['height'] * 0.65)
        pixels = list(image.crop((left, top, right, bottom)).getdata())
        return tuple(sum(channel) / len(pixels) for channel in zip(*pixels, strict=True))

    def test_wallpaper_shows_through_glass(self):
        """像素断言：设置了背景图，面板下面的红色背景必须透出来。

        这条同时证明三件事：背景层画在了面板**之下**（否则没有"透出"一说）、
        玻璃的不透明度真的作用在面板上、以及玻璃随背景图自动生效（不设开关）。
        采样区域取设置面板的中心，纯色背景下的期望是明显的偏红。
        """
        self._ensure_background_selected()
        self._open_section('背景')
        self._set_select('#bg-fit', 'cover')
        self._set_range('#glass-blur', 0)
        self._set_range('#glass-alpha', 30)

        self.assertTrue(self._wait_for(lambda: self._js(
            "return document.documentElement.getAttribute('data-shell-glass') === 'on';")),
            '设置了背景图却没有自动开启玻璃：data-shell-glass 不是 on')
        time.sleep(0.5)
        rect = self._js("""
            const r = document.querySelector('.settings-panel').getBoundingClientRect();
            return { x: r.x, y: r.y, width: r.width, height: r.height };
        """)
        average = self._panel_average_color(rect)
        self.assertGreater(average[0], average[2] + 60,
                           f'面板下没有透出红色背景（采样平均色 {average}）：'
                           f'背景层的层叠位置或玻璃不透明度没生效')

        # 取消背景图（再点一次选中的缩略图）：玻璃随之关闭，红色不再透出来
        self.assertTrue(self._click_active_thumb(), '没有选中态的缩略图可供取消')
        self.assertTrue(self._wait_for(lambda: self._js(
            "return document.documentElement.getAttribute('data-shell-glass') === 'off';")),
            '取消背景图后玻璃没有关闭')
        time.sleep(0.5)
        average = self._panel_average_color(rect)
        self.assertLess(average[0], average[2] + 60,
                        f'取消背景图后面板仍然透出背景：{average}')

    def test_clicking_active_image_again_clears_background(self):
        """选中态是开关：再点一次同一张图即取消，不需要单独的"清除背景图"按钮。"""
        self._ensure_background_selected()
        self._open_section('背景')
        self.assertTrue(self._wait_for(lambda: self._js(
            "return document.querySelectorAll('.bg-item.active').length === 1;")),
            '当前背景没有对应的选中态条目')
        self.assertTrue(self._js("return !document.querySelector('#bg-clear');"),
                        '「清除背景图」按钮仍在页面上')
        self.assertTrue(self._click_active_thumb(), '点不到选中的缩略图')
        self.assertTrue(self._wait_for(lambda: not self._style_var('--shell-bg-image')),
                        '再点一次选中的图片没有取消背景')
        self.assertEqual(self._js("return document.querySelectorAll('.bg-item.active').length;"), 0,
                         '取消后仍有条目显示为选中')

    def test_delete_removes_entry_and_clears_selection(self):
        """删除按钮把库内条目删掉；删掉的正是当前背景时一并清空选择。"""
        self._ensure_background_selected()
        self._open_section('背景')
        self.assertTrue(self._wait_for(lambda: self._active_image_name() is not None),
                        '没有选中态的条目，无法验证"删掉当前背景"')
        before = self._js("return document.querySelectorAll('.bg-item').length;")
        name = self._active_image_name()

        self._js("document.querySelector('.bg-item.active .bg-remove').click();")
        self.assertTrue(self._wait_for(
            lambda: self._js("return document.querySelectorAll('.bg-item').length;") == before - 1),
            f'删除后网格条目数没有从 {before} 减少')
        self.assertIsNone(self._js("""
            const items = Array.from(document.querySelectorAll('.bg-item .bg-thumb'));
            return items.some(e => e.textContent.includes(arguments[0])) ? true : null;
        """, name), f'被删掉的 {name} 仍在网格里')
        self.assertTrue(self._wait_for(lambda: not self._style_var('--shell-bg-image')),
                        '删掉当前背景后 html 上仍留着背景图 URL')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
