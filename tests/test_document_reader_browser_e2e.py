"""document-reader 前端的真实浏览器用例（本地手动运行，不进 CI）。

为什么需要它（都是实测踩到的，node 桩与 Python 单测都看不见）：

  - **侧栏被书名顶宽**：`.nr-sidebar` 是 flex 项，`min-width: auto` 等于内容的
    min-content 宽度，一行不换行的长书名就是整个书名的宽度；而且壳的 base.css 是在
    插件样式**之后**注入的，同特异性的 `.view-sub-sidebar{width:240px}` 会把插件写的
    252px 与窄屏媒体查询变成死代码。这两件事只有真实布局引擎能量出来。
  - **连续滚动模式滑不动**：EPUB / Markdown 的小章节三章都填不满一屏时，容器根本
    滚不动 → 浏览器不产生滚动事件 → 既不会加载下一章，也没有任何"补满视图"的动作，
    用户看到的就是"只显示这一章、再也滑不下去，只能点目录跳章"。
  - **滚动换章后目录高亮/工具栏标题不动**：当前章只在"加载相邻章"时才同步，
    章内滚动与滑到下一章都不会更新界面。

做法：起一个本地 HTTP 服务，把插件真实的 index.html + 真实的壳样式喂给 Chrome，
只有 Bridge 换成打到该服务的假实现，因此测的是真布局、真滚动事件。

本地运行：
    venv/Scripts/python -m unittest tests.test_document_reader_browser_e2e -v

依赖：本机有 Chrome 或 Edge，以及 selenium（requirements-e2e.txt）；缺任一项则跳过。
"""

import json
import shutil
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_FRONTEND = PROJECT_ROOT / 'plugins' / 'document-reader' / 'frontend'
SHELL_PUBLIC = PROJECT_ROOT / 'shell' / 'frontend' / 'public' / 'shell'

LONG_TITLE = '长书名测试：这是一本名字特别特别长的电子书用来验证侧栏宽度不会被顶开（第二版）'
SHORT_TITLE = '短'
SHORT_BOOK = 'short-chapters.epub'


def _have_selenium() -> bool:
    try:
        import selenium  # noqa: F401
    except Exception:
        return False
    return True


def _have_browser() -> bool:
    for exe in ('chrome', 'chrome.exe', 'msedge', 'msedge.exe', 'chromium', 'chromium-browser'):
        if shutil.which(exe):
            return True
    for path in (r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                 r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
                 r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                 r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'):
        if Path(path).is_file():
            return True
    return False


STUB_BRIDGE = """
<script src="/shell/base.js"></script>
<script>
window.__CALLS__ = [];
window.Bridge = {
  setPrefix: function () {},
  call: function (method) {
    var args = Array.prototype.slice.call(arguments, 1);
    window.__CALLS__.push([method].concat(args));
    return fetch('/api', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({method: method, args: args})
    }).then(function (r) { return r.json(); });
  },
  callSystem: function (method) {
    var args = Array.prototype.slice.call(arguments, 1);
    return fetch('/api', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({method: method, args: args})
    }).then(function (r) { return r.json(); });
  },
  originalUrl: function (p) { return '/file?path=' + encodeURIComponent(p) + '&plugin=document-reader'; }
};
// 朗读在本用例里不参与：初始化不跑（否则每个用例都会去打 tts_status），
// 相关方法留空实现，保证 app.js 里的调用点不炸。
window.__TTS_STUB__ = true;
try { localStorage.setItem('document-reader-tts', 'off'); } catch (e) {}
</script>
"""

SHELL_STYLES = ('<link rel="stylesheet" href="/shell/variables.css">'
                '<link rel="stylesheet" href="/shell/base.css">'
                '<link rel="stylesheet" href="/shell/effects.css">')


def _document(document_id: str, title: str, count: int = 12, file_size: int = 0,
           directory: str = '', kind: str = 'epub') -> dict:
    return {'id': document_id, 'title': title, 'author': '', 'kind': kind, 'dir': directory,
            'root': 'D:/docs', 'file_path': f'D:/docs/{document_id}', 'file_size': file_size,
            'chapter_count': count, 'last_read_chapter': 0, 'progress': 0.0,
            'scroll_position': 0.0, 'encoding': 'auto'}


class _Handler(BaseHTTPRequestHandler):
    """插件前端 + 壳样式的静态服务，以及一个可被用例改写的假后端。"""

    state: ClassVar[dict] = {}

    def log_message(self, *args):  # 静音：默认会往 stderr 刷每一行请求
        pass

    def _send(self, code: int, body, ctype: str) -> None:
        data = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ('/', '/bare'):
            html = (PLUGIN_FRONTEND / 'index.html').read_text(encoding='utf-8')
            boot = (SHELL_STYLES if path == '/' else '') + STUB_BRIDGE
            self._send(200, html.replace('</head>', boot + '</head>'),
                       'text/html; charset=utf-8')
            return
        for prefix, root in (('/shell/', SHELL_PUBLIC), ('/', PLUGIN_FRONTEND)):
            if path.startswith(prefix):
                candidate = (root / path[len(prefix):]).resolve()
                if candidate.is_file() and str(candidate).startswith(str(root.resolve())):
                    suffix = candidate.suffix
                    ctype = {'.css': 'text/css', '.js': 'text/javascript',
                             '.html': 'text/html'}.get(suffix, 'application/octet-stream')
                    self._send(200, candidate.read_bytes(), f'{ctype}; charset=utf-8')
                    return
        self._send(404, 'nope', 'text/plain')

    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length') or 0)
        payload = json.loads(self.rfile.read(length) or b'{}')
        if urlparse(self.path).path == '/__set':
            self.state.update(payload)
            self._send(200, '{}', 'application/json')
            return
        method, args = payload.get('method'), payload.get('args') or []
        if method == 'document_list':
            body = json.dumps({'documents': self.state.get('documents', [])})
        elif method == 'document_get_chapters':
            body = json.dumps({'chapters': self.state.get('chapters', [])})
        elif method == 'document_get_content':
            content = (self.state.get('bodies') or {}).get(args[0], {}).get(str(args[1]), '')
            body = json.dumps({'content': content, 'format': 'html'})
        elif method == 'document_update_progress':
            body = json.dumps({'success': True})
        elif method == 'get_settings':
            body = json.dumps(dict(self.state.get('settings') or {}))
        elif method == 'save_settings':
            # 阅读偏好存在后端设置里（跨设备持久化）：桩必须真的记住，
            # 否则"用户改过的选择要能记住"这条永远测不出来
            stored = dict(self.state.get('settings') or {})
            stored.update((args[0] if args and isinstance(args[0], dict) else {}))
            self.state['settings'] = stored
            body = json.dumps({'success': True})
        elif method == 'marks_list':
            body = json.dumps({'marks': []})
        elif method == 'document_cover':
            body = json.dumps({'cover': ''})
        elif method == 'tts_status':
            body = json.dumps({'engines': [], 'order': [], 'mode': 'auto', 'voice': '', 'rate': 0})
        else:
            body = '{}'
        self._send(200, body, 'application/json')


@unittest.skipUnless(_have_selenium() and _have_browser(),
                     '需要 selenium 与 Chrome/Edge（见 requirements-e2e.txt）')
class DocumentReaderBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

        options = Options()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        cls.driver = webdriver.Chrome(options=options)
        cls.driver.set_window_size(1280, 900)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown()

    # ===== 工具 =====

    def set_state(self, **fields):
        """重设假后端数据（每个用例自己写，避免互相污染）。"""
        _Handler.state.clear()
        _Handler.state.update(fields)

    def open_app(self, route: str = '/'):
        self.driver.get(self.base + route)
        self.wait('window.documentReader && window.documentReader.documents.length > 0')

    def wait(self, condition: str, timeout: float = 10.0):
        from selenium.webdriver.support.ui import WebDriverWait
        WebDriverWait(self.driver, timeout, poll_frequency=0.05).until(
            lambda d: d.execute_script(f'return !!({condition});'))

    # ===== 封面卡网格 =====

    def test_cover_grid_survives_long_titles(self):
        """长书名不许把卡片撑出横向溢出，也不许把侧栏顶宽。

        重构前这条测的是"书架那行不把侧栏撑宽"（书架当时在侧栏里）；现在书架是主区的
        封面网格，同样的隐患换了个位置：书名在卡片里必须省略号截断，不能把网格撑破。
        """
        geometry = {}
        for label, route in (('带壳样式', '/'), ('无壳样式', '/bare')):
            for tag, title in (('long', LONG_TITLE), ('short', SHORT_TITLE)):
                self.set_state(documents=[_document('probe.epub', title)],
                               chapters=[{'index': 0, 'title': '第一章', 'word_count': 10}],
                               bodies={})
                self.open_app(route)
                geometry[(label, tag)] = self.driver.execute_script("""
                    const sidebar = document.querySelector('.nr-sidebar');
                    const grid = document.querySelector('.nr-grid');
                    const card = document.querySelector('.nr-card');
                    const title = document.querySelector('.nr-card-title');
                    return {
                        sidebar: sidebar.getBoundingClientRect().width,
                        gridClient: grid.clientWidth, gridScroll: grid.scrollWidth,
                        cardWidth: card.getBoundingClientRect().width,
                        titleClient: title.clientWidth, titleScroll: title.scrollWidth,
                    };
                """)
            long_geo, short_geo = geometry[(label, 'long')], geometry[(label, 'short')]
            details = json.dumps([long_geo, short_geo], ensure_ascii=False)
            self.assertLess(abs(long_geo['sidebar'] - short_geo['sidebar']), 1,
                            f'{label}：侧栏宽度不该随书名长度变化 {details}')
            self.assertLessEqual(long_geo['gridScroll'], long_geo['gridClient'] + 1,
                                 f'{label}：长书名把封面网格撑出了横向溢出 {details}')
            # 长书名的标题必须真的被截断（scroll > client），否则就是"把卡片撑宽了"
            self.assertGreater(long_geo['titleScroll'], long_geo['titleClient'],
                               f'{label}：长书名没有被截断 {details}')
            self.assertGreater(long_geo['cardWidth'], 80,
                               f'{label}：封面卡被挤没了 {details}')

        # 窄屏：封面网格要跟着变小，不能横向溢出
        self.driver.set_window_size(800, 900)
        try:
            self.open_app('/')
            narrow = self.driver.execute_script("""
                const grid = document.querySelector('.nr-grid');
                return {client: grid.clientWidth, scroll: grid.scrollWidth};
            """)
        finally:
            self.driver.set_window_size(1280, 900)
        self.assertLessEqual(narrow['scroll'], narrow['client'] + 1,
                             f'窄屏下封面网格横向溢出了: {narrow}')

    # ===== 卡片副标题显示什么 =====

    def test_card_subtitle_shows_size_instead_of_question_mark(self):
        """章节数还没解析出来时显示体积；有章节数就显示章数，任何情况下都不显示 "?"。"""
        self.set_state(
            documents=[_document('never-opened.epub', '没打开过', count=0, file_size=1572864),
                    _document('opened.epub', '打开过', count=12),
                    _document('sub/nested.epub', '子目录里的', count=3, directory='文档/武侠')],
            chapters=[{'index': 0, 'title': '第一章', 'word_count': 1}], bodies={})
        self.open_app()
        cards = self.driver.execute_script("""
            return [...document.querySelectorAll('.nr-card')].map(card => ({
                title: card.querySelector('.nr-card-title').textContent,
                sub: card.querySelector('.nr-card-sub').textContent,
            }));
        """)
        joined = ' | '.join(f"{c['title']}={c['sub']}" for c in cards)
        self.assertNotIn('?', joined, f'卡片上不该出现 "?"：{joined}')
        self.assertIn('1.5 MB', joined, f'没解析过的书应当显示体积：{joined}')
        self.assertIn('12 章', joined, f'解析过的书应当显示章数：{joined}')

    # ===== 连续滚动 =====

    def _open_scroll_mode(self, chapters: int = 12):
        """打开一本"每章只有三行"的书并切到连续滚动模式。"""
        items = [{'index': i, 'title': f'第{i + 1}章', 'word_count': 6} for i in range(chapters)]
        bodies = {str(i): f'<p>第{i + 1}章正文</p>' * 3 for i in range(chapters)}
        self.set_state(documents=[_document(SHORT_BOOK, SHORT_TITLE, count=chapters)],
                       chapters=items, bodies={SHORT_BOOK: bodies})
        self.open_app()
        self.driver.execute_script("""
            const app = window.documentReader;
            app.mode = 'scroll';
            app.engine.setMode('scroll');
            document.getElementById('document-mode-select').value = 'scroll';
            return app.openDocument(arguments[0]);
        """, SHORT_BOOK)
        time.sleep(0.6)

    def test_short_chapters_fill_the_viewport(self):
        """短文打开后必须补满一屏，否则容器滚不动、浏览器不会再给滚动事件。"""
        self._open_scroll_mode()
        fill = self.driver.execute_script("""
            const el = document.getElementById('document-content-area');
            return {scroll: el.scrollHeight, client: el.clientHeight,
                    loaded: [window.documentReader.engine.loadedStart,
                             window.documentReader.engine.loadedEnd]};
        """)
        self.assertGreater(fill['scroll'], fill['client'] + 100,
                           f'短文打开后没有补满一屏（会永远滑不动）: {json.dumps(fill)}')
        self.assertEqual(fill['loaded'][0], 0, f'补满时把开头的章节删掉了: {json.dumps(fill)}')

    def test_keep_loading_after_reaching_the_bottom(self):
        """滑到底停住（不再有滚动事件）之后，下一章仍然要加载。"""
        self._open_scroll_mode()
        before = self.driver.execute_script("return window.documentReader.engine.loadedEnd")
        self.driver.execute_script("""
            const el = document.getElementById('document-content-area');
            el.scrollTop = el.scrollHeight;
            el.dispatchEvent(new Event('scroll'));
        """)
        time.sleep(0.7)
        after = self.driver.execute_script("return window.documentReader.engine.loadedEnd")
        self.assertGreater(after, before, f'滑到底后没有再加载下一章: {before} -> {after}')

    def test_toc_modal_highlights_current_chapter(self):
        """滚动换章后，目录弹窗里高亮的必须是新章。

        重构前这里测的是"侧栏目录的高亮跟着滚动走"（目录当时常驻侧栏，靠
        `_syncReaderChrome` 那套节流刷新同步）。现在目录改成弹窗、打开时按
        `engine.currentChapterIndex` 现刷 —— 所以校验点跟着搬到"打开弹窗后看到什么"，
        这也正是用户能看见的那部分。
        """
        self._open_scroll_mode()
        self.driver.execute_script("""
            const el = document.getElementById('document-content-area');
            const node = el.querySelector('.chapter-content[data-chapter-index="3"]');
            if (node) el.scrollTop = node.offsetTop + 10;
            el.dispatchEvent(new Event('scroll'));
        """)
        time.sleep(0.6)
        opened = self.driver.execute_script("""
            const app = window.documentReader;
            document.getElementById('nr-open-toc').click();
            const active = document.querySelector('#nr-toc-list .document-chapter-item.active');
            return {
                index: app.engine.currentChapterIndex,
                modalOpen: document.getElementById('nr-toc-modal').classList.contains('active'),
                activeIndex: active ? parseInt(active.dataset.index, 10) : null,
                title: document.getElementById('nr-view-title').textContent,
                book: (app.currentDocument || {}).title,
            };
        """)
        details = json.dumps(opened, ensure_ascii=False)
        self.assertTrue(opened['modalOpen'], f'目录弹窗没打开 {details}')
        self.assertEqual(opened['activeIndex'], opened['index'],
                         f'目录弹窗里高亮的不是当前章 {details}')
        # 新形态下工具栏放的是书名（章节名在目录弹窗与正文里，不再挤工具栏）
        self.assertEqual(opened['title'], opened['book'],
                         f'工具栏没显示当前书名 {details}')

    def test_click_does_not_turn_page_in_scroll_mode(self):
        """连续滚动模式下点内容区不该翻页（整章重建会让阅读位置跳走）。"""
        self._open_scroll_mode()
        index_before = self.driver.execute_script(
            "return window.documentReader.engine.currentChapterIndex")
        self.driver.execute_script("""
            const el = document.getElementById('document-content-area');
            const r = el.getBoundingClientRect();
            el.dispatchEvent(new MouseEvent('click',
                {clientX: r.left + r.width * 0.9, clientY: r.top + 40, bubbles: true}));
        """)
        time.sleep(0.4)
        index_after = self.driver.execute_script(
            "return window.documentReader.engine.currentChapterIndex")
        self.assertEqual(index_after, index_before, '滚动模式下点击内容区不该换章')

    def test_click_still_turns_page_in_page_mode(self):
        """翻页模式的行为不能被上面的改动带坏。"""
        self._open_scroll_mode()
        self.driver.execute_script("""
            const app = window.documentReader;
            app.mode = 'page';
            app.engine.setMode('page');
            document.getElementById('document-mode-select').value = 'page';
            return app.openDocument(arguments[0], {chapter: 0});
        """, SHORT_BOOK)
        time.sleep(0.6)
        self.driver.execute_script("""
            const el = document.getElementById('document-content-area');
            const r = el.getBoundingClientRect();
            el.dispatchEvent(new MouseEvent('click',
                {clientX: r.left + r.width * 0.9, clientY: r.top + 40, bubbles: true}));
        """)
        time.sleep(0.5)
        index = self.driver.execute_script(
            "return window.documentReader.engine.currentChapterIndex")
        self.assertGreaterEqual(index, 1, '翻页模式下点击右半区应当翻到下一章')


    # ===== 设置与编码 =====

    def _open_book(self, book: str = 'book.txt') -> None:
        self.set_state(documents=[_document(book, '书', count=1, kind='txt')],
                       chapters=[{'index': 0, 'title': '第1章', 'word_count': 3}],
                       bodies={book: {'0': '正文'}})
        self.open_app()
        self.driver.execute_script('window.documentReader.openDocument(arguments[0]);', book)
        self.wait('window.documentReader._isReaderMode')

    def test_encoding_is_fully_automatic(self):
        """编码不再由用户选：界面上没有那个下拉框，重读也永远用 auto。

        手选的选项在后端已经被解码链完全覆盖（UTF-8 自证 → 中文编码 → gb18030），
        手选唯一能做到的"额外效果"是把本来能读的书解成乱码；前端那份还曾经因为回调里
        的 `e` 未定义而完全无效、修好后又被 currentDocument 的旧值盖回去。整项删掉。
        """
        self._open_book()
        self.assertIsNone(
            self.driver.execute_script("return document.getElementById('document-encoding');"),
            '编码下拉框应该已经删掉')

        self.driver.execute_script('window.documentReader.reloadDocument();')
        time.sleep(0.6)
        state = self.driver.execute_script("""
            const calls = window.__CALLS__.filter(c => c[0] === 'document_get_chapters');
            return {encoding: window.documentReader.encoding,
                    lastEncoding: calls.length ? calls[calls.length - 1][2] : null,
                    saved: JSON.parse(localStorage.getItem('document-reader-settings') || '{}')};
        """)
        details = json.dumps(state, ensure_ascii=False)
        self.assertEqual(state['encoding'], 'auto', f'应用内的编码不是自动检测 {details}')
        self.assertEqual(state['lastEncoding'], 'auto', f'重读没有用自动检测 {details}')
        self.assertNotIn('encoding', state['saved'], f'设置里还留着已经删掉的编码项 {details}')

    def test_defaults_are_follow_theme_and_continuous_scroll(self):
        """默认是「跟随主题 + 连续滚动」，且旧设置文件不会把用户锁在旧默认上。

        旧设置文件里存着的主题/模式是每次加载时自动 save 进去的旧默认值，不是用户的
        选择：升到 v2 时按新默认走一次，字号这些真偏好原样保留；之后用户在界面上选的
        仍然记得住。

        偏好存在**后端设置**里（跨设备持久化），写入带 400ms 防抖 —— 改完要等落盘
        再 reload，否则读到的是旧值（这条以前是 localStorage 同步写，不需要等）。
        """
        self._open_book()                      # 先拿到同源页面，才能写 localStorage
        self.driver.execute_script("""
            localStorage.setItem('document-reader-settings', JSON.stringify(
                {fontSize: 22, theme: 'sepia', mode: 'page', encoding: 'auto'}));
        """)
        self.open_app()
        self.wait('window.documentReader.settings && window.documentReader.fontSize === 22')
        state = self.driver.execute_script("""
            const app = window.documentReader;
            return {theme: app.theme, mode: app.mode, fontSize: app.fontSize,
                    themeSelect: document.getElementById('document-theme-select').value,
                    modeSelect: document.getElementById('document-mode-select').value,
                    engineMode: app.engine.mode};
        """)
        details = json.dumps(state, ensure_ascii=False)
        self.assertEqual(state['theme'], 'auto', f'默认主题不是跟随主题 {details}')
        self.assertEqual(state['themeSelect'], 'auto', f'主题下拉框没跟着默认值 {details}')
        self.assertEqual(state['mode'], 'scroll', f'默认阅读模式不是连续滚动 {details}')
        self.assertEqual(state['modeSelect'], 'scroll', f'阅读模式下拉框没跟着默认值 {details}')
        self.assertEqual(state['engineMode'], 'scroll', f'引擎没跟着默认值 {details}')
        self.assertEqual(state['fontSize'], 22, f'旧设置里的字号被改掉了 {details}')

        # 用户自己改过之后必须记住（带 version 的那次迁移不该覆盖用户的新选择）
        self.driver.execute_script("""
            const select = document.getElementById('document-mode-select');
            select.value = 'page';
            select.dispatchEvent(new Event('change'));
        """)
        time.sleep(1.0)                        # 等 400ms 防抖落盘
        self.open_app()
        self.wait('window.documentReader.settings')
        self.assertEqual(self.driver.execute_script('return window.documentReader.mode'), 'page',
                         '用户自己选的阅读模式没有被记住')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
