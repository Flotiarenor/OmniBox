"""媒体播放器「下一首 / 上一首落点是否与列表顺序一致」的现场诊断脚本（手动运行，不是 unittest 用例）。

与 tests/test_media_player_browser_e2e.py 的区别：本脚本不注入任何桩，走真实壳页面 →
真实插件 iframe → 真实媒体库（当前设置里的主目录与额外目录），因此在 Windows 上跑出来的
结果就是本地媒体库的真实行为。

它做三件事
----------
1. 打开壳页面并切进媒体播放器插件，优先选中侧边栏第一个歌单，没有歌单则用当前视图；
2. 依次点「下一首」走到底、再依次点「上一首」走回开头，逐步比对
   「实际播放条目」与「列表里的相邻条目」；
3. 同时打印判断成因所需的三项事实：队列顺序是否与列表一致、播放模式、当前视图。

输出里若有 MISMATCH 行，把整段输出贴回来即可定位：
  - 实际条目不在列表里 → 队列与当前列表不是同一份列表（例如上次播放的是别的列表）；
  - 实际条目 = 上一步条目 → 未推进（单曲循环模式，或该条目加载失败）；
  - 实际条目跳过一格 → 用户操作与自动跳转/自动推进叠加。

运行
----
Windows:
    venv\\Scripts\\pip install -r requirements-e2e.txt
    venv\\Scripts\\python tests\\debug_media_player_nav.py

Linux / macOS:
    venv/bin/pip install -r requirements-e2e.txt
    venv/bin/python tests/debug_media_player_nav.py

可选环境变量：
    OMNIBOX_CHROME_BINARY / OMNIBOX_CHROMEDRIVER  指定 Chrome / chromedriver 路径
    OMNIBOX_DEBUG_STEPS                            每个方向最多走几步（默认 8）
    OMNIBOX_DEBUG_VIEW                             指定左侧导航项名称（默认「媒体播放器」）

注意：脚本会在真实媒体库上静音播放曲目，更新插件的「最近播放」与播放状态，并在播放模式不是
顺序播放时把它切到顺序播放（随机/单曲循环下列表顺序不是参照）；如需保留原模式，设置
OMNIBOX_DEBUG_KEEP_MODE=1。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 复用回归用例里的浏览器探测与端口/健康检查（同一套 OMNIBOX_* 环境变量）
from tests.test_media_player_browser_e2e import (
    STARTUP_TIMEOUT,
    _chrome_binary,
    _chromedriver_path,
    _free_port,
    _wait_health,
)

PLUGIN_ENTRY = '/plugins/media-player/frontend/index.html'
STEP_TIMEOUT = 15.0
MAX_STEPS = int(os.environ.get('OMNIBOX_DEBUG_STEPS') or 8)


def _log(message: str) -> None:
    print(message, flush=True)


def main() -> int:
    if _chrome_binary() is None:
        _log('未检测到 Chrome/Edge：可用 OMNIBOX_CHROME_BINARY 指定可执行文件路径')
        return 2
    try:
        import selenium  # noqa: F401
    except Exception:
        _log('未安装 selenium：先执行 venv\\Scripts\\pip install -r requirements-e2e.txt')
        return 2

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    port = _free_port()
    base = f'http://127.0.0.1:{port}'
    server = subprocess.Popen(
        [sys.executable, 'main.py', '--web-only', '--port', str(port)],
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not _wait_health(base, STARTUP_TIMEOUT):
        server.terminate()
        _log('服务未在超时内就绪：先确认 `python main.py --web-only` 能正常启动')
        return 2

    options = Options()
    binary = _chrome_binary()
    if binary:
        options.binary_location = binary
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-gpu')
    options.add_argument('--mute-audio')
    options.add_argument('--autoplay-policy=no-user-gesture-required')
    options.add_argument('--window-size=1400,900')
    driver_path = _chromedriver_path()
    try:
        if driver_path:
            driver = webdriver.Chrome(options=options, service=Service(driver_path))
        else:
            driver = webdriver.Chrome(options=options)
    except Exception as exc:
        server.terminate()
        _log(f'无法启动 WebDriver：{type(exc).__name__}: {exc}')
        return 2
    driver.set_page_load_timeout(30)

    mismatches: list[str] = []
    try:
        driver.get(base)
        _wait(driver, "return document.querySelectorAll('.nav-item').length > 0;", '壳导航未渲染')
        target = (os.environ.get('OMNIBOX_DEBUG_VIEW') or '媒体播放器').strip()
        clicked = driver.execute_script(
            "const t = arguments[0];"
            "const hit = Array.from(document.querySelectorAll('.nav-item'))"
            ".find(e => e.textContent.includes(t));"
            "if (hit) { hit.click(); return true; } return false;", target)
        if not clicked:
            _log(f'壳导航里找不到「{target}」：请用 OMNIBOX_DEBUG_VIEW 指定其他名称')
            return 2

        frame = _wait_iframe(driver)
        _log(f'已进入插件 iframe：{frame.get_attribute("src")}')
        driver.switch_to.frame(frame)
        _wait(driver, "return !!window.mediaPlayerApp && !!mediaPlayerApp.core;", '插件未初始化')
        _wait(driver, "return !!document.querySelector('#media-content .mp-row, #mp-detail-list .mp-row, .mp-empty-state');",
              '媒体列表未渲染')

        container = _pick_list(driver)
        titles = _titles(driver, container)
        if len(titles) < 3:
            _log(f'所选列表只有 {len(titles)} 个条目，无法比较相邻落点；请先建立包含 >=3 条的列表')
            return 2

        mode_state = _state(driver, container)
        _log('')
        _log(f'播放模式：{mode_state["playModeName"]}（{mode_state["playMode"]}）')
        if mode_state['playMode'] != 0:
            _log('  说明：随机播放下列表顺序不是参照 —— 下一首/上一首都会随机取条目（可跳到任意位置）；'
                 '单曲循环下「下一首」重播当前条目。')
            if os.environ.get('OMNIBOX_DEBUG_KEEP_MODE'):
                _log('  按 OMNIBOX_DEBUG_KEEP_MODE 保持该模式继续比较（结果必然与列表顺序不符）')
            else:
                _normalize_mode(driver, container)

        state = _state(driver, container)
        _log('')
        _log(f'列表来源：{container}，共 {len(titles)} 条')
        _log(f'列表顺序：{titles}')
        _log(f'选中前队列长度：{len(state["queueOrder"])}')
        _log(f'当前视图：{state["view"]}，播放模式：{state["playModeName"]}（{state["playMode"]}）')

        _log('')
        _log('—— 未点选条目时先点一次「下一首」 ——')
        mismatch = _probe_before_selection(driver, container, titles)
        if mismatch:
            mismatches.append(mismatch)

        driver.execute_script(
            f"document.querySelector('{container} .mp-row[data-idx=\"0\"]').click();")
        _wait_playing(driver, container, titles[0])
        selected = _state(driver, container)
        _log('')
        _log(f'基线：点第 1 行 → 正在播放「{titles[0]}」，队列顺序：{selected["queueOrder"]}')
        if selected['queueOrder'] != titles:
            mismatches.append('点选条目后队列顺序与列表顺序不一致：下一首/上一首会走另一份列表')

        _log('')
        _log('—— 依次点「下一首」 ——')
        for index in range(min(len(titles) - 1, MAX_STEPS)):
            mismatch = _step(driver, container, titles, 'next', index, index + 1)
            if mismatch:
                mismatches.append(mismatch)
                break

        _log('')
        _log('—— 依次点「上一首」 ——')
        back = _state(driver, container)['index']
        for index in range(back, 0, -1):
            if back - index > MAX_STEPS:
                break
            mismatch = _step(driver, container, titles, 'prev', index, index - 1)
            if mismatch:
                mismatches.append(mismatch)
                break

        _log('')
        if mismatches:
            _log('结论：发现不一致，请把以上输出（含 MISMATCH 行）贴回：')
            for item in mismatches:
                _log(f'  - {item}')
            return 1
        _log('结论：本次运行中「下一首 / 上一首」落点与列表顺序一致，未复现不一致')
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


# --------------------------------------------------------------------- 工具


def _wait(driver, script: str, message: str, timeout: float = 30.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if driver.execute_script(script):
                return True
        except Exception:
            pass
        time.sleep(0.2)
    raise SystemExit(f'{message}（{timeout:.0f}s 超时）')


def _wait_iframe(driver, timeout: float = 30.0):
    from selenium.common.exceptions import NoSuchElementException

    start = time.time()
    while time.time() - start < timeout:
        for frame in driver.find_elements('tag name', 'iframe'):
            src = frame.get_attribute('src') or ''
            if '/plugins/media-player/' in src:
                return frame
        time.sleep(0.2)
    raise NoSuchElementException('未找到媒体播放器 iframe')


def _pick_list(driver) -> str:
    """优先用侧边栏第一个歌单；没有歌单时退到「全部音乐 / 全部视频」，都为空则提示先扫描。"""
    playlists = driver.execute_script(
        "return document.querySelectorAll('#mp-playlist-list .mp-playlist-item').length;")
    if playlists:
        name = driver.execute_script(
            "return document.querySelector('#mp-playlist-list .mp-playlist-item .pl-name').textContent.trim();")
        driver.execute_script(
            "document.querySelector('#mp-playlist-list .mp-playlist-item').click();")
        time.sleep(0.6)
        _wait(driver, "return document.querySelectorAll('#mp-detail-list .mp-row').length > 0;",
              '歌单详情未渲染')
        _log(f'已选中歌单：{name}')
        return '#mp-detail-list'

    for view, label in (('all-audio', '全部音乐'), ('all-video', '全部视频')):
        clicked = driver.execute_script(
            "const b = document.querySelector('.mp-nav-item[data-view=\"' + arguments[0] + '\"]');"
            "if (b) { b.click(); return true; } return false;", view)
        if not clicked:
            continue
        for _ in range(50):                       # 最多等 10s：视图切换是异步的
            if driver.execute_script(
                    "return document.querySelectorAll('#media-content .mp-row').length;"):
                _log(f'侧边栏没有歌单，改用「{label}」列表')
                return '#media-content'
            time.sleep(0.2)
    raise SystemExit('媒体库没有可比较的条目：先在插件里执行扫描，或建立包含 >=3 条的列表')


def _titles(driver, container: str) -> list:
    return driver.execute_script(
        f"return Array.from(document.querySelectorAll('{container} .mp-row'))"
        ".map(r => r.querySelector('.mp-row-title').textContent);")


def _state(driver, container: str) -> dict:
    return driver.execute_script("""
        const app = window.mediaPlayerApp, core = app.core;
        const names = ['顺序播放', '随机播放', '单曲循环'];
        return {
            index: core.currentIndex,
            playing: core.currentItem ? core.currentItem.title : null,
            queueOrder: core.queue.map(i => i.title),
            view: app.currentView,
            playMode: core.playMode,
            playModeName: names[core.playMode] || String(core.playMode),
            activeRows: Array.from(document.querySelectorAll(arguments[0] + ' .mp-row.active'))
                .map(r => r.dataset.idx + ':' + r.querySelector('.mp-row-title').textContent),
        };
    """, container)


def _normalize_mode(driver, container: str) -> None:
    """把播放模式切到顺序播放：本脚本比较的不变式是「按列表顺序推进」。"""
    for _ in range(3):
        driver.execute_script("document.querySelector('#btn-play-mode').click();")
        time.sleep(0.3)
        state = _state(driver, container)
        if state['playMode'] == 0:
            _log(f'  已切换为：{state["playModeName"]}（0）')
            return
    raise SystemExit('未能切换到顺序播放：请手动点播放模式按钮后重试')


def _probe_before_selection(driver, container: str, titles: list):
    """不点选条目，直接点一次「下一首」，检查当前队列是否跟随所选列表。

    复现要点：进程或 iframe 重载后播放核心只恢复当前条目（queue = [item]），
    此时「下一首 / 上一首」不会走到所选列表的相邻条目；点过任意条目后队列才变成该列表。
    """
    before = _state(driver, container)
    if not before['queueOrder']:
        _log('  说明：当前队列为空（无恢复条目），未点选条目时 next/prev 无响应')
        return '未点选条目时队列为空：此时「下一首 / 上一首」无响应，与所选列表无关'

    driver.execute_script("document.querySelector('#btn-next').click();")
    time.sleep(1.5)
    after = _state(driver, container)
    line = (f'{before["playing"]} → {after["playing"]}'
            f'（队列长度 {len(before["queueOrder"])} → {len(after["queueOrder"])}，'
            f'期望列表中的下一首）')
    if after['playing'] == before['playing']:
        _log(f'  MISMATCH {line}：未推进，队列是重载后恢复的单条队列')
        return '未点选条目时「下一首」未推进（队列只有恢复的当前条目）：与所选列表不符'
    if after['playing'] not in titles:
        _log(f'  MISMATCH {line}：落点不在所选列表中（队列是上次播放的列表）')
        return '未点选条目时「下一首」落到所选列表之外的条目：队列与当前列表不是同一份'
    expected = None
    if before['playing'] in titles:
        position = titles.index(before['playing'])
        if position + 1 < len(titles):
            expected = titles[position + 1]
    if expected and after['playing'] != expected:
        _log(f'  MISMATCH {line}：跳过相邻条目（表中为 {expected}）')
        return '未点选条目时「下一首」跳过相邻条目：队列顺序与列表不一致'
    _log(f'  OK    {line}')
    return None


def _wait_playing(driver, container: str, title: str, timeout: float = STEP_TIMEOUT) -> dict:
    start = time.time()
    state = {}
    while time.time() - start < timeout:
        state = _state(driver, container)
        if state['playing'] == title and driver.execute_script(
                "return window.mediaPlayerApp.core.mediaElement.currentTime > 0.1;"):
            return state
        time.sleep(0.2)
    raise SystemExit(f'等待「{title}」开始播放超时，当前状态：{state}')


def _step(driver, container: str, titles: list, action: str, from_index: int, expect_index: int):
    """点一次「下一首 / 上一首」，比对落点与列表相邻条目；不一致返回描述，否则 None。"""
    before = _state(driver, container)
    label = '下一首' if action == 'next' else '上一首'
    driver.execute_script(f"document.querySelector('#btn-{action}').click();")

    start = time.time()
    after = before
    while time.time() - start < STEP_TIMEOUT:
        after = _state(driver, container)
        if after['playing'] != before['playing']:
            break
        time.sleep(0.2)
    time.sleep(0.6)                       # 观察窗口：让自动推进/失败跳转的后续步骤暴露出来
    settled = _state(driver, container)

    expected = titles[expect_index]
    line = (f'{label}：{from_index + 1}. {before["playing"]} → {settled["playing"]}'
            f'（index={settled["index"]}，高亮={settled["activeRows"]}，期望 {expect_index + 1}. {expected}）')
    if settled['playing'] == expected:
        _log(f'  OK    {line}')
        return None
    _log(f'  MISMATCH {line}')
    if settled['playing'] not in titles:
        return f'{label} 落点「{settled["playing"]}」不在所选列表中：队列与列表不是同一份'
    if settled['playing'] == before['playing']:
        return f'{label} 未推进（仍为「{before["playing"]}」）：单曲循环模式或该条目加载失败'
    if after['playing'] != settled['playing']:
        return f'{label} 落点在观察窗口内再次变化（{after["playing"]} → {settled["playing"]}）：疑与自动推进/失败跳转叠加'
    return f'{label} 跳过相邻条目：由 {from_index + 1} 落到 {settled["index"] + 1}，期望 {expect_index + 1}'


if __name__ == '__main__':   # pragma: no cover
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
