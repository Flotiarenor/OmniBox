"""媒体播放器「下一首 / 上一首落点与列表顺序一致」的真实浏览器回归用例（本地手动运行，不进 CI）。

缺陷背景
--------
实测（Windows）媒体播放器点击「下一首 / 上一首」时，播放条目与歌单顺序不符，且跳转
距离不固定。用真实浏览器复现后确认的成因是**加载失败后的自动跳转方向**：

  - 队列里的条目加载失败（如 Chrome 不支持的 `.wma/.ape/.wv/.avi/.wmv/.flv`，或
    文件已移动、URL 已失效）时，`player-core.js` 同曲重试一次，仍失败则跳下一首；
  - 该跳转过去固定**向后**（`index >= curIdx + 1`）。于是：
      * 点「下一首」落到失败条目 → 再向后跳一格，一次操作推进两格；
      * 点「上一首」落到失败条目 → 仍向后跳，净效果是停在原曲目（表观无效）或反向前进；
      * 连续失败条目越多跳得越远，表现为「无规律」。
  - 修复后跳转按本次导航方向进行：下一首向后、上一首向前，该方向无可播放条目则停止。

为什么用桩 Bridge 而不是真实媒体库
----------------------------------
缺陷位于插件前端（`player-core.js` 的队列索引与失败跳转），而真实媒体库依赖标签解析 /
ffmpeg，且无法稳定构造「部分条目加载失败」。本用例在页面脚本执行前注入
`window.pywebview.api`，只桩掉数据来源；页面、样式、音频元素与完整事件路径都是真实的
（入口 `/plugins/media-player/frontend/index.html`，与宿主 iframe 加载的是同一份文件），
可播放条目用页面内生成的 data: URL WAV，不可播放条目用伪造的 wma data: URL。

本地运行
--------
    venv/Scripts/pip install -r requirements-e2e.txt
    venv/Scripts/python -m unittest tests.test_media_player_browser_e2e -v

依赖：selenium 4.6+ 与本机 Chrome / Edge（selenium 自带 driver 管理）。
如需指定浏览器或驱动路径（例如 Linux 上使用解压后的 Chrome for Testing）：

    OMNIBOX_CHROME_BINARY=/opt/cft/chrome-linux64/chrome \
    OMNIBOX_CHROMEDRIVER=/opt/cft/chromedriver-linux64/chromedriver \
    venv/bin/python -m unittest tests.test_media_player_browser_e2e -v

未安装 selenium 或未检测到浏览器时用例自动跳过，不会让 `unittest discover` 变红。
"""

from __future__ import annotations

import os
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

# 断言用的等待上限：真实浏览器渲染与「重试 800ms + 跳转 600ms」的失败链路都是异步的
STARTUP_TIMEOUT = 40.0
NAV_TIMEOUT = 20.0
PLUGIN_ENTRY = '/plugins/media-player/frontend/index.html'

# 用例歌单的固定顺序：列表第 i 行、队列第 i 项、标题三者必须一致
TITLES = ('第一首', '第二首', '第三首', '第四首', '第五首')

# 注入到插件页面的桩：只替换 PyWebView API（数据来源），不碰页面与播放核心。
# 用例配置从 URL fragment 的单参数读取（`#cfg=键:值|键:值`），避免每个用例重复注入脚本。
# 用单参数而不是 `a=1&b=2`：多参数 fragment 在部分浏览器/驱动下会把 `&` 编码掉，取值会串。
#   broken:1,2        这些下标的条目不可播放（模拟 Chrome 不支持的格式）
#   playback:item3    启动时按该条目恢复播放
#   queue:1           恢复播放时带持久化队列（media_get_playback 返回 queue_ids）
#   shuffle:1         启动时处于随机播放模式
#   pls:2             侧栏提供两个歌单（第二个只有第 4、5 首），用于核对按行打开的是哪一份列表
STUB_SCRIPT = r"""
(function () {
  function makeWav(seconds, freq) {
    var rate = 8000, frames = Math.floor(rate * seconds);
    var bytes = new Uint8Array(44 + frames);
    function wr(off, str) { for (var i = 0; i < str.length; i++) bytes[off + i] = str.charCodeAt(i); }
    function u32(off, v) { bytes[off] = v & 255; bytes[off + 1] = (v >> 8) & 255; bytes[off + 2] = (v >> 16) & 255; bytes[off + 3] = (v >> 24) & 255; }
    function u16(off, v) { bytes[off] = v & 255; bytes[off + 1] = (v >> 8) & 255; }
    wr(0, 'RIFF'); u32(4, 36 + frames); wr(8, 'WAVEfmt ');
    u32(16, 16); u16(20, 1); u16(22, 1); u32(24, rate); u32(28, rate); u16(32, 1); u16(34, 8);
    wr(36, 'data'); u32(40, frames);
    for (var i = 0; i < frames; i++) {
      bytes[44 + i] = 128 + Math.round(100 * Math.sin(2 * Math.PI * freq * i / rate));
    }
    var raw = '';
    for (var j = 0; j < bytes.length; j++) raw += String.fromCharCode(bytes[j]);
    return 'data:audio/wav;base64,' + btoa(raw);
  }

  function cfg(name) {
    var hit = (location.hash || '').replace(/^#cfg=/, '').split('|')
      .map(function (pair) { return pair.split(':'); })
      .filter(function (pair) { return pair[0] === name; })[0];
    return hit ? hit.slice(1).join(':') : '';
  }
  var broken = cfg('broken').split(',').filter(function (s) { return s !== ''; })
    .map(function (s) { return parseInt(s, 10); });
  var playbackItem = cfg('playback');
  var withQueue = cfg('queue') === '1';
  var shuffle = cfg('shuffle') === '1';
  var twoPlaylists = cfg('pls') === '2';

  var good = makeWav(30, 440);                                     // 可播放：30s 单声道 WAV
  var bad = 'data:audio/x-ms-wma;base64,' + btoa('not a wma payload');  // 不可播放：伪造 wma
  var titles = ['第一首', '第二首', '第三首', '第四首', '第五首'];
  var items = titles.map(function (title, i) {
    return {
      id: 'item' + i, title: title, artist: 'artist', album: 'album', kind: 'audio',
      stream_url: broken.indexOf(i) >= 0 ? bad : good,
      path: '/nonexistent/' + i + '.wma', duration: 30.0, mtime: 1700000000 + i,
      has_cover: false, is_fav: false
    };
  });
  var playlist = {
    id: 'pl1', name: '测试歌单', created_at: '2026-01-01',
    item_ids: items.map(function (i) { return i.id; }), items: items
  };
  // 第二个歌单只有第 4、5 首：点到它时列表行数必须变成 2，才能说明「按行打开」生效
  var secondPlaylist = {
    id: 'pl2', name: '第二个歌单', created_at: '2026-01-02',
    item_ids: ['item3', 'item4'], items: items.slice(3, 5)
  };
  var playlists = twoPlaylists
    ? [{ id: 'pl1', name: '测试歌单', created_at: '2026-01-01', item_ids: playlist.item_ids },
       { id: 'pl2', name: '第二个歌单', created_at: '2026-01-02', item_ids: secondPlaylist.item_ids }]
    : [{ id: 'pl1', name: '测试歌单', created_at: '2026-01-01', item_ids: playlist.item_ids }];
  var playback = {
    item_id: playbackItem, loop_mode: 'all', shuffle: shuffle, volume: 1, video_mode: 'video',
    queue_ids: withQueue ? items.map(function (i) { return i.id; }) : [],
    queue_index: withQueue ? 1 : 0
  };

  var table = {
    'media-player__get_settings': function () { return {}; },
    'media-player__save_settings': function () { return { success: true }; },
    'media-player__get_settings_schema': function () { return []; },
    'media-player__media_get_state': function () {
      return { favorites: [], recent: items, playlists: [] };
    },
    'media-player__media_stats': function () {
      return { audio: 5, video: 0, total: 5, playlists: playlists.length, favorites: 0,
               audio_albums: 1, video_albums: 0 };
    },
    'media-player__media_scan_status': function () { return { state: 'done', processed: 5, total: 5 }; },
    'media-player__media_scan': function () { return { error: 'running' }; },
    'media-player__media_get_playback': function () { return playback; },
    'media-player__media_save_playback': function () { return { success: true }; },
    'media-player__media_update_recent': function () { return { success: true }; },
    'media-player__media_playlist_list': function () { return playlists; },
    'media-player__media_playlist_get': function (id) {
      if (id === 'pl2' && twoPlaylists) return secondPlaylist;
      return playlist;
    },
    'media-player__media_playlist_save': function () {
      return { success: true, playlist: { id: 'pl1', name: '测试歌单', item_ids: [] } };
    },
    'media-player__media_playlist_delete': function () { return { success: true }; },
    'media-player__media_all_audio': function () { return items; },
    'media-player__media_all_video': function () { return []; },
    'media-player__media_audio_albums': function () { return []; },
    'media-player__media_video_albums': function () { return []; },
    'media-player__media_album_items': function () { return items; },
    'media-player__media_search': function () { return items; },
    'media-player__media_get_item': function (id) {
      return items.filter(function (i) { return i.id === id; })[0] || items[0];
    },
    'media-player__media_get_items': function (ids) {
      var wanted = ids || [];
      return wanted
        .map(function (id) { return items.filter(function (i) { return i.id === id; })[0]; })
        .filter(Boolean);
    },
    'media-player__media_toggle_favorite': function () { return { is_fav: false }; },
    'media-player__media_get_lyrics': function () { return {}; },
    'media-player__media_thumb_missing': function () { return []; },
    'media-player__media_list_eq_presets': function () { return []; },
    'media-player__media_put_thumb': function () { return { success: false }; },
    'media-player__media_set_config': function () { return { success: true }; },
    'media-player__get_settings': function () { return {}; },
    'system_get_plugin_extensions': function () { return []; },
    'get_settings': function () { return {}; }
  };
  var api = {};
  Object.keys(table).forEach(function (key) {
    // 必须转发实参：桩方法按 id / id 列表取值，丢掉实参会让 media_get_item 一律返回首条
    api[key] = function () { return Promise.resolve(table[key].apply(null, arguments)); };
  });
  window.pywebview = { api: api };
})();
"""


def _have_selenium() -> bool:
    try:
        import selenium  # noqa: F401
    except Exception:
        return False
    return True


def _chrome_binary() -> str | None:
    """Chrome / Edge 可执行文件路径（selenium 4.6+ 会自行下载匹配的 driver）。"""
    override = (os.environ.get('OMNIBOX_CHROME_BINARY') or '').strip()
    if override and Path(override).is_file():
        return override
    for exe in ('chrome', 'chrome.exe', 'msedge', 'msedge.exe', 'chromium', 'chromium-browser'):
        found = shutil.which(exe)
        if found:
            return found
    for candidate in (
        Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'),
        Path(r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _chromedriver_path() -> str | None:
    override = (os.environ.get('OMNIBOX_CHROMEDRIVER') or '').strip()
    return override if override and Path(override).is_file() else None


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


@unittest.skipUnless(
    _have_selenium(),
    '未安装 selenium（本地端到端用例）：pip install -r requirements-e2e.txt',
)
@unittest.skipUnless(_chrome_binary(), '未检测到 Chrome/Edge，跳过媒体播放器浏览器用例')
class MediaPlayerBrowserE2ETests(unittest.TestCase):
    """真实 Chrome + 真实插件页面，核对下一首/上一首的落点。"""

    driver = None
    server = None
    base_url = ''
    _nav_seq = 0    # 每个用例的页面地址都不同：只改 fragment 不会重新加载文档

    @classmethod
    def setUpClass(cls):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

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
            raise unittest.SkipTest('服务未在超时内就绪，跳过媒体播放器浏览器用例')

        options = Options()
        binary = _chrome_binary()
        if binary:
            options.binary_location = binary
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--mute-audio')
        # 无头环境没有音频输出设备，自动播放策略也需显式放开，否则元素停在 paused
        options.add_argument('--autoplay-policy=no-user-gesture-required')
        # 固定窗口尺寸：默认无头视口只有约 1080×427，弹出面板（宽 460px、贴底定位）会盖住
        # 播放栏上的开关按钮，点按钮落在面板上，测的就不再是点击判定本身
        options.add_argument('--window-size=1400,900')
        driver_path = _chromedriver_path()
        try:
            if driver_path:
                cls.driver = webdriver.Chrome(options=options, service=Service(driver_path))
            else:
                cls.driver = webdriver.Chrome(options=options)
        except Exception as exc:   # 驱动下载失败 / 浏览器不匹配：跳过而不是判失败
            cls.server.terminate()
            cls.server = None
            raise unittest.SkipTest(f'无法启动 WebDriver（{type(exc).__name__}: {exc}）') from exc
        cls.driver.set_page_load_timeout(30)
        # 页面脚本执行前注入桩：插件页会从 window.pywebview.api 解析 Bridge
        cls.driver.execute_cdp_cmd(
            'Page.addScriptToEvaluateOnNewDocument', {'source': STUB_SCRIPT})

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

    # ------------------------------------------------------------------ 工具

    def _wait(self, predicate, message: str, timeout: float = NAV_TIMEOUT) -> None:
        start = time.time()
        last = None
        while time.time() - start < timeout:
            try:
                last = predicate()
                if last:
                    return
            except Exception as exc:
                last = exc
            time.sleep(0.2)
        self.fail(f'{message}（{timeout:.0f}s 超时，最后一次取值：{last!r}）')

    def _state(self) -> dict:
        return self.driver.execute_script("""
            const app = window.mediaPlayerApp;
            const core = app && app.core;
            const el = core ? core.mediaElement : null;
            const modeNames = ['顺序播放', '随机播放', '单曲循环'];
            return {
                index: core ? core.currentIndex : null,
                playing: core && core.currentItem ? core.currentItem.title : null,
                listOrder: app ? app._currentListData.map(i => i.title) : [],
                queueOrder: core ? core.queue.map(i => i.title) : [],
                playMode: core ? core.playMode : null,
                playModeName: core ? (modeNames[core.playMode] || String(core.playMode)) : null,
                ct: el ? +(el.currentTime || 0).toFixed(2) : 0,
                err: el && el.error ? el.error.code : null,
                activeRows: Array.from(document.querySelectorAll('#mp-detail-list .mp-row.active'))
                    .map(r => r.dataset.idx + ':' + r.querySelector('.mp-row-title').textContent),
            };
        """)

    def _rows(self) -> list:
        return self.driver.execute_script(
            "return Array.from(document.querySelectorAll('#mp-detail-list .mp-row'))"
            ".map(r => r.querySelector('.mp-row-title').textContent);")

    def _click(self, selector: str) -> None:
        clicked = self.driver.execute_script(
            "const el = document.querySelector(arguments[0]); if (el) { el.click(); return true; }"
            "return false;", selector)
        self.assertTrue(clicked, f'找不到可点击元素：{selector}')

    def _current_playlist(self) -> str:
        return self.driver.execute_script(
            "return (mediaPlayerApp.currentPlaylist && mediaPlayerApp.currentPlaylist.name) || '';")

    def _real_click_at(self, x: float, y: float) -> None:
        """真实鼠标点击视口坐标：走浏览器命中测试，事件链与用户点击一致。

        断言「点得开」必须用真实鼠标：`el.click()` 的 target 恒为元素本身，恰好绕开
        「按钮里的 `<svg>` / `<use>` 才是 target」这一类缺陷（见 test_real_click_* 用例）。
        """
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        pointer = PointerInput('mouse', 'e2e')
        builder = ActionBuilder(self.driver, mouse=pointer)
        builder.pointer_action.move_to_location(int(x), int(y)).click()
        builder.perform()

    def _center_of(self, selector: str) -> tuple:
        point = self.driver.execute_script(
            "const r = document.querySelector(arguments[0]).getBoundingClientRect();"
            "return [r.left + r.width / 2, r.top + r.height / 2];", selector)
        self.assertIsNotNone(point, f'找不到元素：{selector}')
        return float(point[0]), float(point[1])

    def _click_center(self, selector: str) -> str:
        """真实鼠标点元素中心，返回该点实际命中的元素描述（按钮里的图标是常态）。"""
        x, y = self._center_of(selector)
        hit = self.driver.execute_script(
            "const el = document.elementFromPoint(arguments[0], arguments[1]);"
            "return el ? el.tagName + (el.id ? '#' + el.id : '') : '（空）';", x, y)
        self._real_click_at(x, y)
        return hit

    def _panel_state(self, panel_id: str) -> str:
        return self.driver.execute_script(
            "const el = document.getElementById(arguments[0]);"
            "if (!el) return '不存在';"
            "if (getComputedStyle(el).display === 'none') return '未打开';"
            "const r = el.getBoundingClientRect();"
            "return (r.width > 0 && r.height > 0) ? '打开' : '打开了但不可见';", panel_id)

    def _modal_title(self, modal_id: str = 'modal-playlist') -> str:
        return self.driver.execute_script(
            "const modal = document.getElementById(arguments[0]);"
            "if (!modal || !modal.classList.contains('active')) return '（未打开）';"
            "return (document.getElementById('playlist-modal-title') || {}).textContent || '';",
            modal_id)

    def _click_row(self, index: int) -> None:
        self._click(f'#mp-detail-list .mp-row[data-idx="{index}"]')

    def _open_playlist(self, query: str = '') -> None:
        """按 fragment 配置加载插件页，进入歌单视图并等待列表渲染。

        地址带自增的 query：只改 fragment 时浏览器不会重新加载文档，插件页不会重新初始化。
        fragment 用单参数 `#cfg=键:值|键:值`（见 STUB_SCRIPT 注释）。
        """
        type(self)._nav_seq += 1
        self.driver.get(f'{self.base_url}{PLUGIN_ENTRY}?case={type(self)._nav_seq}#cfg={query}')
        self._wait(
            lambda: bool(self.driver.execute_script(
                "return !!(window.mediaPlayerApp && mediaPlayerApp.playlists"
                " && mediaPlayerApp.playlists.playlists.length);")),
            '插件页未初始化完成',
        )
        self._click('.mp-playlist-item[data-pl-id="pl1"]')
        self._wait(lambda: len(self._rows()) == len(TITLES), '歌单列表未渲染出全部条目')

    def _wait_playing(self, title: str, timeout: float = NAV_TIMEOUT) -> dict:
        start = time.time()
        state = {}
        while time.time() - start < timeout:
            state = self._state()
            if state.get('playing') == title and (state.get('ct') or 0) > 0.15:
                return state
            time.sleep(0.2)
        raise self.failureException(
            f'期望正在播放「{title}」，实际状态：{state}（{timeout:.0f}s 超时）')

    def _rewind(self) -> None:
        """`prev()` 在 currentTime > 3 时按「重播当前曲目」处理，点「上一首」前必须确保播放位置接近 0。

        实测（Windows）该断言曾偶发失败：慢机器上「读取状态 → 点击」之间的浏览器往返耗时
        会把播放位置推到 3s 之后，`prev()` 于是走重播分支而不换曲。这里改为无条件 seek 并
        复核位置，不依赖「刚好小于阈值」的时序。
        """
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if (self._state().get('ct') or 0) < 0.5:
                return
            self.driver.execute_script('window.mediaPlayerApp.core.seekTo(0);')
            time.sleep(0.2)
        self.fail(f'播放位置未回到开头，无法测试「上一首」：{self._state()}')

    def _step_and_wait(self, selector: str) -> str:
        """点一次切歌按钮并等到播放条目真的换掉，返回新条目标题。"""
        before = self._state()['playing']
        self._click(selector)
        deadline = time.time() + NAV_TIMEOUT
        while time.time() < deadline:
            now = self._state()['playing']
            if now != before:
                return now
            time.sleep(0.2)
        raise self.failureException(
            f'点击 {selector} 后播放条目未变化：{self._state()}（{NAV_TIMEOUT:.0f}s 超时）')

    # ------------------------------------------------------------------ 用例

    def test_list_order_matches_queue(self):
        """选中条目后，队列顺序与列表顺序、条目标题必须一致（后续用例断言落点的前提）。"""
        self._open_playlist()
        self.assertEqual(self._rows(), list(TITLES))

        self._click_row(0)
        state = self._wait_playing(TITLES[0])
        self.assertEqual(state['index'], 0)
        # 前提：本组用例断言的是顺序模式下的推进；随机 / 单曲循环模式不按列表顺序（见诊断脚本）
        self.assertEqual(state['playMode'], 0, f'播放模式不是顺序播放：{state}')
        self.assertEqual(state['queueOrder'], list(TITLES),
                         f'点选条目后队列顺序与列表不一致：{state}')

    def test_next_and_prev_follow_adjacent_items(self):
        """全部条目可播放时：点第 1 行 → 下一首到第 2 行 → 上一首回第 1 行。"""
        self._open_playlist()
        self._click_row(0)
        self.assertEqual(self._wait_playing(TITLES[0])['index'], 0)

        self._click('#btn-next')
        self.assertEqual(self._wait_playing(TITLES[1])['index'], 1)

        self._rewind()
        self._click('#btn-prev')
        self.assertEqual(self._wait_playing(TITLES[0])['index'], 0)

    def test_next_continues_forward_over_unplayable_item(self):
        """第 3 首不可播放：从第 2 首点下一首，落点为其后第一个可播放条目（第 4 首）。"""
        self._open_playlist('broken:2')
        self._click_row(1)
        self.assertEqual(self._wait_playing(TITLES[1])['index'], 1)

        self._click('#btn-next')
        state = self._wait_playing(TITLES[3])
        self.assertEqual(state['index'], 3, f'下一首落点异常：{state}')
        self.assertGreater(state['index'], 1, f'下一首未向后推进：{state}')

    def test_prev_continues_backward_over_unplayable_item(self):
        """第 2 首不可播放：从第 3 首点上一首，落点为其前第一个可播放条目（第 1 首）。

        回归点：修复前固定向后跳，上一首会停回第 3 首（表观无效）或跳到第 3 首之后。
        """
        self._open_playlist('broken:1')
        self._click_row(2)
        self.assertEqual(self._wait_playing(TITLES[2])['index'], 2)

        self._rewind()
        self._click('#btn-prev')
        state = self._wait_playing(TITLES[0])
        self.assertEqual(state['index'], 0, f'上一首落点异常：{state}')
        self.assertLess(state['index'], 2, f'上一首反向前进：{state}')

    def test_restore_follows_persisted_queue(self):
        """恢复播放时按持久化队列恢复整条队列：未点选条目直接点「下一首」也应跟随该队列。

        回归点：修复前 `restorePlayback` 只写 `queue = [item]`，此时「下一首」只是重播当前条目，
        与可见歌单无关（实测表现为重载后 next/prev 不跟列表）。
        """
        self._open_playlist('playback:item1|queue:1')
        state = self._state()
        self.assertEqual(state['playMode'], 0, f'播放模式异常：{state}')
        self.assertEqual(state['queueOrder'], list(TITLES), f'恢复后队列与持久化队列不一致：{state}')

        self._click('#btn-next')                       # 未点选任何条目
        state = self._wait_playing(TITLES[2])
        self.assertEqual(state['index'], 2, f'恢复后「下一首」未跟随队列：{state}')

    def test_shuffle_prev_returns_along_played_order(self):
        """随机模式下进退沿同一份排列：连续「下一首」的顺序与连续「上一首」的顺序互为逆序。

        回归点：修复前随机模式每次 `Math.random()` 取下标，进退互不相关，「上一首」可跳到队列任意位置。
        """
        self._open_playlist('shuffle:1')
        self._click_row(0)
        self.assertEqual(self._wait_playing(TITLES[0])['playMode'], 1, '未处于随机播放模式')

        forward = []
        for _ in range(3):
            title = self._step_and_wait('#btn-next')
            self._wait_playing(title)
            forward.append(title)
        self.assertEqual(len(set(forward)), 3, f'随机模式下连续前进出现重复条目：{forward}')

        back = []
        for _ in range(3):
            self._rewind()
            back.append(self._step_and_wait('#btn-prev'))

        # 播放路径为 [起始, ...forward]，回退 3 次应依次回到 forward[1]、forward[0]、起始条目
        expected = [*forward[:-1][::-1], TITLES[0]]
        self.assertEqual(back, expected,
                         f'上一首未按原路返回：forward={forward} back={back} 期望={expected}')

    def test_prev_stops_at_head_without_forward_jump(self):
        """第 1、2 首均不可播放：从第 3 首点上一首，前一方向无可播放条目时停在前方尽头。"""
        self._open_playlist('broken:0,1')
        self._click_row(2)
        self.assertEqual(self._wait_playing(TITLES[2])['index'], 2)

        self._rewind()
        self._click('#btn-prev')
        # 落点第 1 首同样不可播放，失败链路（重试 800ms + 跳转 600ms）跑完后不应再移动
        self._wait(
            lambda: bool(self.driver.execute_script(
                "const c = window.mediaPlayerApp.core;"
                "return c.currentIndex === 0 && c.currentItem"
                f" && c.currentItem.title === '{TITLES[0]}';")),
            '上一首未落到前方尽头条目',
        )
        time.sleep(2.0)
        state = self._state()
        self.assertEqual(state['index'], 0, f'上一首越过当前条目向后跳：{state}')
        self.assertNotEqual(state['playing'], TITLES[3], f'上一首跳到了当前条目之后：{state}')


    def test_real_click_on_icon_opens_eq_and_queue(self):
        """真实鼠标点按钮中心（命中的是图标 `<svg>` / `<use>`）必须打开面板。

        缺陷背景：文档级「点面板外面就关」按 `e.target !== document.getElementById('btn-eq')`
        判定，而真实鼠标点在图标字形上时 target 是 `<svg>` / `<use>` 而不是按钮，刚打开的
        面板被同一轮事件立即关掉。图标只占按钮中心约 15×15px，其余是内边距，因此表现成
        「有时点得开、有时点不开」。修复前本用例必然失败，`el.click()` 写的用例则必然通过。
        """
        self._open_playlist()
        for button_id, panel_id in (('btn-eq', 'eq-panel'), ('btn-queue', 'queue-popup')):
            self.driver.execute_script(
                "document.querySelectorAll('.mp-pop').forEach(p => p.classList.add('hidden'));")
            hit = self._click_center(f'#{button_id}')
            self.assertIn(hit.split('#')[0], ('svg', 'use'),
                          f'#{button_id} 中心点应命中按钮内的图标（实际 {hit}），'
                          f'否则本用例没有覆盖缺陷路径')
            state = self._panel_state(panel_id)
            self.assertEqual(state, '打开',
                             f'真实鼠标点 #{button_id} 中心（命中 {hit}）后 {panel_id} 为「{state}」')
            # 再点一次必须关掉：不能修成「只开不关」
            self._click_center(f'#{button_id}')
            self.assertEqual(self._panel_state(panel_id), '未打开',
                             f'再点一次 #{button_id} 未关闭 {panel_id}')

    def test_alternating_panel_clicks_keep_working(self):
        """交替连点「均衡器 → 播放队列」各 10 轮：每一轮都要打开队列，并且关掉均衡器。

        缺陷背景：文档级关闭处理必须在同一次点击里正确区分「点的是哪一个开关按钮」。
        判据放宽成 `target.closest('.mp-pop')` 时，点队列会被当成「点在面板内」，
        已打开的均衡器面板就关不掉（两个面板同时悬浮）。
        """
        self._open_playlist()
        for attempt in range(10):
            self.driver.execute_script(
                "document.querySelectorAll('.mp-pop').forEach(p => p.classList.add('hidden'));")
            self._click_center('#btn-eq')
            self.assertEqual(self._panel_state('eq-panel'), '打开', f'第 {attempt + 1} 轮：均衡器未打开')
            self._click_center('#btn-queue')
            self.assertEqual(self._panel_state('queue-popup'), '打开',
                             f'第 {attempt + 1} 轮：点队列未打开')
            self.assertEqual(self._panel_state('eq-panel'), '未打开',
                             f'第 {attempt + 1} 轮：点队列后均衡器面板仍开着')

    def test_click_outside_still_closes_panels(self):
        """点面板与开关按钮之外的区域仍要关掉面板（回归「修成关不掉」）。"""
        self._open_playlist()
        for button_id, panel_id in (('btn-eq', 'eq-panel'), ('btn-queue', 'queue-popup')):
            self._click_center(f'#{button_id}')
            self.assertEqual(self._panel_state(panel_id), '打开', f'前置条件失败：{panel_id} 未打开')
            self._click('#mp-view-title')
            self.assertEqual(self._panel_state(panel_id), '未打开',
                             f'点面板外（#mp-view-title）后 {panel_id} 仍开着')

    def test_eq_button_opens_after_view_and_mode_switching(self):
        """切换左侧视图与播放模式后再点均衡器仍要打开（覆盖刷新与模式变更后的状态）。"""
        self._open_playlist()
        for view in ('recent', 'all-audio', 'audio-albums', 'favorites'):
            self.driver.execute_script("mediaPlayerApp.switchView(arguments[0]);", view)
            self._wait(lambda view=view: self.driver.execute_script(
                "return mediaPlayerApp.currentView === arguments[0];", view),
                f'未切到视图 {view}')
            self.driver.execute_script(
                "document.getElementById('eq-panel').classList.add('hidden');")
            self._click_center('#btn-eq')
            self.assertEqual(self._panel_state('eq-panel'), '打开', f'视图 {view} 下均衡器未打开')

        for _ in range(3):                                  # 顺序 → 随机 → 单曲 → 顺序
            self._click('#btn-play-mode')
            self.driver.execute_script(
                "document.getElementById('eq-panel').classList.add('hidden');")
            mode = self.driver.execute_script("return mediaPlayerApp.core.playMode;")
            self._click_center('#btn-eq')
            self.assertEqual(self._panel_state('eq-panel'), '打开',
                             f'播放模式 {mode} 下均衡器未打开')

    def test_playlist_rows_open_their_own_items(self):
        """侧栏两行歌单各自打开自己那份列表：点第 2 行要看到第 2 个歌单的 2 个条目。"""
        self._open_playlist('pls:2')
        self._wait(lambda: self.driver.execute_script(
            "return document.querySelectorAll('.mp-playlist-item').length === 2;"),
            '侧栏未渲染出两个歌单')

        self._click('.mp-playlist-item[data-pl-id="pl1"] .pl-name')
        self._wait(lambda: self._current_playlist() == '测试歌单',
                   '点第 1 行未打开「测试歌单」')
        self._wait(lambda: len(self._rows()) == len(TITLES), '第 1 个歌单条目数不符')

        self._click('.mp-playlist-item[data-pl-id="pl2"] .pl-name')
        self._wait(lambda: self._current_playlist() == '第二个歌单',
                   '点第 2 行未打开「第二个歌单」')
        self._wait(lambda: [r for r in self._rows()] == list(TITLES[3:]),
                   '第 2 个歌单的条目与自己的 item_ids 不符')

    def test_playlist_menu_rename_by_icon_opens_modal(self):
        """歌单「⋯」菜单里点「重命名」的图标（真实鼠标落点）必须打开重命名弹窗。

        缺陷背景：菜单项处理器读的是 `ev.target.dataset.menuAct`，而菜单项里有图标，
        点到图标字形时 `ev.target` 是 `<svg>`（没有菜单项 data 属性），读到 undefined，
        结果是菜单关掉、什么都不做 —— 表现为「重命名 / 删除点了没反应」。
        """
        self._open_playlist()
        menu_opened = self.driver.execute_script("""
            const more = document.querySelector('.mp-playlist-item .pl-more');
            if (!more) return false;
            const r = more.getBoundingClientRect();
            mediaPlayerApp.showPlaylistMenu(more.dataset.plId, { clientX: r.left, clientY: r.top });
            return !!document.getElementById('mp-context-menu');
        """)
        self.assertTrue(menu_opened, '歌单「⋯」菜单未生成')

        hit = self._click_center('.mp-context-menu button[data-menu-act="rename"] svg')
        self.assertIn('svg', hit.lower(), f'菜单项中心点应命中图标（实际 {hit}）')
        self._wait(lambda: self._modal_title() == '重命名歌单',
                   '点「重命名」的图标后未打开重命名弹窗')


    def test_panels_do_not_cover_player_bar_controls(self):
        """窗口变矮时弹出面板仍不能压住播放栏按钮：压住的话点「均衡器」第一次只是关面板。

        缺陷背景：面板用 `bottom: calc(--mp-pb-height + 28px)` 贴底定位，等于假定播放栏贴
        着视口底边。播放栏其实在舞台底部、内容区在舞台下方，窗口变矮时舞台被压扁，面板就
        压到播放栏上（实测 1008×449 视口下均衡器面板盖住全部 10 个播放栏控件）。
        这里把窗口调矮后逐控件核对命中元素，并核对面板底边不越过播放栏顶边。
        """
        self._open_playlist()
        original = self.driver.get_window_size()
        try:
            for width, height in ((1024, 600), (960, 520), (1400, 900)):
                self.driver.set_window_size(width, height)
                time.sleep(0.4)
                for button_id, panel_id in (('btn-eq', 'eq-panel'), ('btn-queue', 'queue-popup')):
                    self.driver.execute_script(
                        "document.querySelectorAll('.mp-pop').forEach(p => p.classList.add('hidden'));")
                    self._click_center(f'#{button_id}')
                    self.assertEqual(self._panel_state(panel_id), '打开',
                                     f'{width}×{height}：#{button_id} 未打开 {panel_id}')
                    covered = self.driver.execute_script("""
                        const panel = document.getElementById(arguments[0]);
                        const p = panel.getBoundingClientRect();
                        const out = [];
                        document.querySelectorAll('#player-bar button').forEach((btn) => {
                            const r = btn.getBoundingClientRect();
                            if (!r.width || !r.height) return;
                            if (getComputedStyle(btn).display === 'none') return;
                            const hit = document.elementFromPoint(r.left + r.width / 2,
                                                                  r.top + r.height / 2);
                            if (hit && hit !== btn && !btn.contains(hit)) {
                                out.push(btn.id + '←' + hit.tagName
                                    + (hit.id ? '#' + hit.id : ''));
                            }
                        });
                        const bar = document.getElementById('player-bar').getBoundingClientRect();
                        return { covered: out,
                                 panelBottom: Math.round(p.bottom),
                                 barTop: Math.round(bar.top),
                                 panelTop: Math.round(p.top) };
                    """, panel_id)
                    self.assertEqual(
                        covered['covered'], [],
                        f'{width}×{height}：{panel_id} 盖住了播放栏控件 {covered["covered"]}')
                    self.assertLessEqual(
                        covered['panelBottom'], covered['barTop'] + 1,
                        f'{width}×{height}：{panel_id} 底边 {covered["panelBottom"]} '
                        f'越过播放栏顶边 {covered["barTop"]}')
                    self.assertGreater(
                        covered['panelTop'], -1,
                        f'{width}×{height}：{panel_id} 顶边 {covered["panelTop"]} 被视口裁掉')

                    # 面板开着时再点一次开关按钮：必须能关掉（按钮不能被自己打开的面板盖住）
                    self._click_center(f'#{button_id}')
                    self.assertEqual(self._panel_state(panel_id), '未打开',
                                     f'{width}×{height}：{panel_id} 打开后点 #{button_id} 关不掉')
        finally:
            self.driver.set_window_size(original['width'], original['height'])
            time.sleep(0.3)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
