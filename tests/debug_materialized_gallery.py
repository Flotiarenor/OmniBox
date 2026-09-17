"""物化目录挂到 image-viewer 之后的**现场演示**：非无头、慢速点击、逐步截图（手动运行，不是 unittest）。

这个脚本回答一个只有肉眼能回答的问题：把 group-mesh 的物化目录加成 image-viewer 的
「额外图片目录」之后，**界面上到底长什么样**。已有的浏览器用例全部跑
`--headless=new`，所以"0 字节占位会让网格退化成什么样"始终只是读码推断 —— 实测结论
（见 `.dsh/group-mesh-materialize.md` §2.2）确实与推断不同，这里把它变成可以盯着看的东西。

它做四件事（每步之间停顿 `OMNIBOX_DEMO_PAUSE` 秒，默认 2.5）
----------------------------------------------------------------
1. 起两个**空白实例**（各自一份 OMNIBOX_HOME，见 tests/harness/app_instance.py），
   建团、共享一个含 6 张不同长宽比 JPEG 的目录、起节点、登记对端、物化。左窗口是
   「看图的那台设备」，右窗口是「提供图片的那台设备」。
2. 在左窗口进「图片相册」，点进物化目录那个顶层节点 → **看那一屏**：宽高 0×0 让
   justified 布局退化，缩略图整片 404。
3. 点一张照片（会打开灯箱、请求原图）→ 仍然什么都没有：请求是发给 image-viewer 的，
   而取字节的钩子属于 group-mesh，所以**点照片也不会把字节取回来**。
4. 用 group-mesh 自己的根把 6 张图取回来，再点「刷新」→ 同一屏恢复正常。

截图落在 `OMNIBOX_DEMO_OUT`（默认 `.build/demo-materialized/`），一步步对应上面的阶段。

运行
----
    venv\\Scripts\\pip install -r requirements-e2e.txt        # 只需一次
    venv\\Scripts\\python tests\\debug_materialized_gallery.py

可选环境变量
------------
    OMNIBOX_DEMO_PAUSE     每步停顿秒数（默认 2.5；设 0 可以快速跑一遍）
    OMNIBOX_DEMO_HOLD      演示结束后保持浏览器打开秒数（默认 300；设 0 立即关）
    OMNIBOX_DEMO_OUT       截图输出目录
    OMNIBOX_DEMO_NO_OWNER  设为 1 则不开右窗口（只开看图那台）
    OMNIBOX_CHROME_BINARY / OMNIBOX_CHROMEDRIVER   同其它 e2e 脚本

注意
----
* 它会真的打开可见的 Chrome 窗口并在你桌面上点击；演示期间不要手动操作那两个窗口
  （会打乱脚本的元素定位）。
* 结束时保持打开一段时间，方便自己点点看；按 `OMNIBOX_DEMO_HOLD=0` 可立刻收工。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.harness.app_instance import MeshCluster, boot_prerequisites
from tests.test_media_player_browser_e2e import (
    _chrome_binary,
    _chromedriver_path,
)

# 每张图刻意不同长宽比：宽高正确时是一个正常的 justified 瀑布流，
# 宽高退化成 0×0 时一眼就能看出不对劲（这就是本演示的看点）。
#
# 图片必须放在**叶子目录**里：额外根的顶层节点是"配置点"（虚拟节点，`direct_count`
# 恒为 0，见 plugins/image-viewer/backend/albums.py:196），点它只会显示子相册网格；
# 要看到图片瀑布流，得再点进一层没有子目录的叶子目录。
PHOTOS = {
    '照片/宽幅全景.jpg': (1200, 300, (46, 120, 200)),
    '照片/竖构图.jpg': (400, 1200, (190, 70, 90)),
    '照片/方构图.jpg': (800, 800, (110, 110, 130)),
    '照片/标准横构图.jpg': (1000, 700, (60, 160, 90)),
    '其他/竖构图2.jpg': (600, 900, (150, 90, 190)),
    '其他/方构图2.jpg': (800, 800, (200, 140, 50)),
}
LEAF = '照片'
SHARE_ID = 'album'
PHOTO_TIMEOUT = 60.0


def _force_utf8_stdout() -> None:
    """让 stdout/stderr 用 UTF-8 输出。

    脚本会打印中文与符号（`✕`、`…`、`→`），而 Windows 控制台默认是 GBK —— `✕` 不在
    GBK 里，`print` 会直接抛 UnicodeEncodeError 把演示打断在半路（实测踩到，正好发生在
    "关灯箱"那一步，于是后半段取字节/刷新的演示全没了）。`errors='replace'` 保证即使
    某个字符打不出来也只显示成 `?`，不会中断。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, '') or default)
    except ValueError:
        return default


PAUSE = env_float('OMNIBOX_DEMO_PAUSE', 2.5)
HOLD = env_float('OMNIBOX_DEMO_HOLD', 300.0)


def say(message: str) -> None:
    """打印一行演示进度（用户是跟着这行字看界面的）。"""
    print(f'[演示] {message}', flush=True)


def pause(message: str = '') -> None:
    if message:
        say(message)
    if PAUSE > 0:
        time.sleep(PAUSE)


def build_shared_dir(root: Path) -> Path:
    """铺一个共享目录：亚目录 + 6 张不同长宽比的真实 JPEG。"""
    from PIL import Image

    shared = root / 'shared'
    for rel, (width, height, color) in PHOTOS.items():
        target = shared / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (width, height), color).save(target, 'JPEG', quality=88)
    return shared


def click(driver, element, note: str = '') -> None:
    """真点击（用户看得见）；元素不可点时退回 JS click 并把原因打出来。"""
    if note:
        say(note)
    try:
        element.click()
    except Exception as exc:
        say(f'（真实点击失败，改用 JS 点击：{type(exc).__name__}）')
        driver.execute_script('arguments[0].click()', element)


def wait_for(driver, predicate, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def enter_plugin(driver, nav_label: str, plugin_name: str):
    """从壳的导航点进某个插件，切到**它自己**那个 iframe 里，返回该 iframe。

    必须按 `data-plugin-name` 选，不能取 `frames[0]`：壳给每个访问过的插件各留一个
    iframe（`v-show` 隐藏，`shell/frontend/src/App.vue:282-296`），`frames[0]` 是最早
    访问的那个。实测踩到：切到「图片相册」之后仍在「团体组网」的文档里找元素 ——
    等满 25 秒超时，而截图里页面明明是对的（截图抓的是 OS 窗口，不受 frame 上下文影响）。
    """
    from selenium.webdriver.common.by import By

    driver.switch_to.default_content()
    nav = next((e for e in driver.find_elements(By.CSS_SELECTOR, '.nav-item')
                if nav_label in (e.text or '')), None)
    if nav is None:
        raise SystemExit(f'壳的导航里没有「{nav_label}」入口（插件没加载？见 /status）')
    click(driver, nav, f'点击左侧导航「{nav_label}」')
    frame_css = f'iframe[data-plugin-name="{plugin_name}"]'
    if not wait_for(driver, lambda: len(driver.find_elements(By.CSS_SELECTOR, frame_css)) > 0):
        raise SystemExit(f'「{nav_label}」的 iframe 没有出现：{frame_css}')
    frame = driver.find_element(By.CSS_SELECTOR, frame_css)
    driver.switch_to.frame(frame)
    return frame


def shoot(driver, out_dir: Path, name: str) -> None:
    """截图存档（同时也是我事后核对界面状态的凭据）。"""
    target = out_dir / f'{name}.png'
    try:
        driver.save_screenshot(str(target))
        say(f'截图: {target}')
    except Exception as exc:
        say(f'（截图失败：{exc}）')


def grid_state(driver) -> str:
    """把网格里每张卡的几何与图片状态都读出来。

    这一步是"看"的量化版本，也是本脚本最有价值的一行输出：宽高退化成 0×0 时，
    justified 布局给卡片的 inline `width/height` 就是 0 —— 卡片在 DOM 里但**看不见**，
    这比"缩略图 404"更难发现（截图里只剩一个很小的破图标记）。
    """
    script = """
    const out = {stats: '', cards: []};
    const stats = document.querySelector('#iv-stats');
    out.stats = stats ? (stats.innerText || '').replace(/\\s+/g, ' ').trim() : '';
    document.querySelectorAll('.iv-image-card').forEach(function (card) {
      const r = card.getBoundingClientRect();
      const img = card.querySelector('img');
      out.cards.push({
        path: card.dataset.path || null,
        url: card.dataset.url || null,
        w: Math.round(r.width), h: Math.round(r.height),
        img: img ? [img.naturalWidth, img.naturalHeight, img.complete] : null,
        text: (card.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 20)
      });
    });
    return JSON.stringify(out);
    """
    import json
    try:
        return json.dumps(json.loads(driver.execute_script(script)), ensure_ascii=False)
    except Exception as exc:
        return f'(读取网格状态失败: {exc})'


def lightbox_state(driver) -> str:
    """读灯箱当前展示的图：自然宽高 + 信息面板文案（占位时应当都是 0）。"""
    script = """
    const img = document.querySelector('#lightbox-img');
    const info = document.querySelector('.lightbox-info');
    const box = document.querySelector('.lightbox');
    return JSON.stringify({
      open: !!(box && box.classList.contains('active')),
      nw: img ? img.naturalWidth : null,
      nh: img ? img.naturalHeight : null,
      info: info ? info.innerText.replace(/\\s+/g, ' ').trim() : ''
    });
    """
    try:
        return str(driver.execute_script(script))
    except Exception as exc:
        return f'(读取灯箱状态失败: {exc})'


def close_lightbox(driver) -> None:
    """点 ✕ 关掉灯箱（壳的通用 Lightbox，见 shell/frontend/public/shell/base.js）。"""
    from selenium.webdriver.common.by import By

    close = next((e for e in driver.find_elements(By.CSS_SELECTOR, '.lightbox-close')
                  if e.is_displayed()), None)
    if close is not None:
        click(driver, close, '点关闭按钮（×）关掉灯箱')


def fetch_all_through_group_mesh(driver, cache_root: Path) -> str:
    """用 group-mesh 自己的根把每张图的字节取回本地（浏览器里发同源 fetch）。

    为什么要绕这一下：`/file?plugin=image-viewer` 对物化目录里的路径**不会**触发
    取字节（取字节的钩子在 group-mesh 那边），所以点照片什么都不会发生。演示里必须
    走 `plugin=group-mesh` 才能把字节弄到本地 —— 这正是"要不要把依赖/根并起来"这个
    设计问题的现场证据。
    """
    urls = []
    for rel in PHOTOS:
        path = str(cache_root / Path(rel).as_posix())
        urls.append('/file?' + urllib.parse.urlencode({'path': path, 'plugin': 'group-mesh'}))
    script = """
    const urls = arguments[0];
    const done = arguments[arguments.length - 1];
    (async () => {
      const out = [];
      for (const u of urls) {
        try {
          const r = await fetch(u);
          const buf = await r.arrayBuffer();
          out.push(r.status + ' ' + buf.byteLength + 'B');
        } catch (e) { out.push('ERR ' + e); }
      }
      done(out.join(' | '));
    })();
    """
    driver.switch_to.default_content()
    return str(driver.execute_async_script(script, urls))


def main() -> int:
    _force_utf8_stdout()
    ok, reason = boot_prerequisites()
    if not ok:
        print(f'无法启动应用实例：{reason}')
        return 2
    binary = _chrome_binary()
    if binary is None:
        print('未检测到 Chrome / Edge（可用 OMNIBOX_CHROME_BINARY 指定）')
        return 2

    out_dir = Path(os.environ.get('OMNIBOX_DEMO_OUT')
                   or (PROJECT_ROOT / '.build' / 'demo-materialized'))
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix='omnibox-demo-'))
    shared = build_shared_dir(workdir)

    say(f'准备两台空白实例（临时目录 {workdir}）')
    cluster = MeshCluster(workdir / 'instances', names=('owner', 'viewer'))
    owner, viewer = cluster.instances
    cluster.start()
    cluster.form_group()
    cluster.call(owner, 'add_share',
                 {'share_id': SHARE_ID, 'path': str(shared), 'read': 'group'})
    endpoints = cluster.start_nodes()
    cluster.link(viewer, endpoints[0], name='owner')
    materialized = cluster.call(viewer, 'materialize_remote',
                                {'device_id': '', 'share_id': SHARE_ID})
    cache_root = Path(materialized['root'])
    say(f'物化完成：{materialized["dirs"]} 个目录、{materialized["files"]} 个文件占位 → {cache_root}')

    # 把物化目录加成额外图片目录（必须在物化之后：`_extra_roots()` 要求目录已存在）。
    saved = viewer.call('image-viewer', 'save_settings', {'extra_roots': str(cache_root)})
    if not saved.get('success'):
        print(f'登记额外目录失败: {saved}')
        return 2
    roots = viewer.call('image-viewer', 'list_roots')
    entry = next(r for r in roots if Path(r['path']).name == SHARE_ID)
    namespace = f'__{entry["namespace"]}'
    say(f'「图片相册」的额外目录已登记，虚拟路径前缀 = {namespace}')

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By

    driver_path = _chromedriver_path()

    def make_driver(position: str):
        options = Options()
        options.binary_location = binary
        # 刻意**不**加 --headless：这个脚本的全部意义就是让人看着它点。
        options.add_argument('--window-size=1500,950')
        options.add_argument(f'--window-position={position}')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-blink-features=AutomationControlled')
        if driver_path:
            return webdriver.Chrome(options=options, service=Service(driver_path))
        return webdriver.Chrome(options=options)

    driver = make_driver('0,0')
    driver.set_page_load_timeout(60)
    owner_driver = None
    try:
        if not os.environ.get('OMNIBOX_DEMO_NO_OWNER'):
            try:
                owner_driver = make_driver('1500,0')
                owner_driver.get(owner.base_url)
                say(f'右窗口 = 提供图片的那台设备（{owner.base_url}）')
            except Exception as exc:
                say(f'（右窗口没开起来，忽略：{type(exc).__name__}: {exc}）')
                owner_driver = None

        say(f'打开看图那台设备：{viewer.base_url}')
        driver.get(viewer.base_url)
        wait_for(driver, lambda: len(driver.find_elements(By.CSS_SELECTOR, '.nav-item')) > 0)
        pause('等壳的导航渲染完')

        # ── 阶段 1：物化确实发生过（团体组网的缓存一览）─────────────────────
        enter_plugin(driver, '团体组网', 'group-mesh')
        pause('进入「团体组网」：本地已物化 1 个共享项、占用多少磁盘都在这儿')
        shoot(driver, out_dir, '01-group-mesh-cache')

        # ── 阶段 2：把物化目录当图片库看 —— 这一屏是本演示的看点 ─────────────
        enter_plugin(driver, '图片相册', 'image-viewer')
        pause('进入「图片相册」：多出来的那个顶层节点就是物化目录（名字是共享标识）')
        shoot(driver, out_dir, '02-albums-with-remote-root')

        album = None
        if wait_for(driver, lambda: len(driver.find_elements(By.CSS_SELECTOR, '.iv-album')) > 0):
            albums = driver.find_elements(By.CSS_SELECTOR, '.iv-album')
            # 把找到的节点打出来：找不到时这一行就是唯一的线索（选择器/命名空间对不上）。
            say(f'相册树顶层节点: {[a.get_attribute("data-path") for a in albums]}')
            album = next((a for a in albums if a.get_attribute('data-path') == namespace), None)
        if album is None:
            say(f'没找到 {namespace} 那个相册节点，后面的演示跳过')
            shoot(driver, out_dir, '02b-album-node-missing')
        else:
            click(driver, album, f'点进物化目录那个节点（data-path={namespace}）')
            # 额外根的顶层是"配置点"：点它得到的是**子相册网格**（封面也是缩略图，
            # 同样取不到），而不是图片瀑布流。先看一眼，再点进叶子目录。
            wait_for(driver, lambda: len(driver.find_elements(By.CSS_SELECTOR, '.iv-album')) > 0)
            pause('这一层是子相册网格：封面同样是缩略图，一样取不到')
            shoot(driver, out_dir, '03-children-with-blank-covers')

            leaf_path = f'{namespace}/{LEAF}'
            leaf = next((a for a in driver.find_elements(By.CSS_SELECTOR, '.iv-album')
                         if a.get_attribute('data-path') == leaf_path), None)
            if leaf is None:
                say(f'没找到叶子目录 {leaf_path}，网格演示跳过')
            else:
                click(driver, leaf, f'点进放图片的叶子目录（{leaf_path}）')

            got_grid = wait_for(driver, lambda: len(driver.find_elements(
                By.CSS_SELECTOR, '.iv-image-card')) > 0)
            pause('进到图片瀑布流：注意布局与缩略图 —— 宽高 0×0、缩略图整片取不到')
            shoot(driver, out_dir, '04-broken-grid')
            say(f'网格状态: {grid_state(driver)}')

            # 只有带 data-url 的才是**图片**卡片；带 data-path 的是子文件夹瓦片，
            # 而且"自己有直接图片的子文件夹"点开是灯箱而不是进入目录（app-grid.js:101-145）。
            def image_cards():
                return driver.find_elements(By.CSS_SELECTOR, '.iv-image-card[data-url]')

            if got_grid and image_cards():
                click(driver, image_cards()[0], '点第一张照片：会开灯箱并请求原图')
                if wait_for(driver, lambda: driver.find_elements(
                        By.CSS_SELECTOR, '.lightbox.active')):
                    pass
                time.sleep(max(PAUSE, 2.0))
                shoot(driver, out_dir, '04-lightbox-placeholder')
                say(f'灯箱状态: {lightbox_state(driver)}')
                say('灯箱里仍然是空的（解析度 0 × 0）—— 请求发给的是 image-viewer，'
                    '而按需取字节的钩子在 group-mesh 那边，所以点照片不会把字节取回来')
                close_lightbox(driver)
                time.sleep(1.0)

            # ── 阶段 3：走 group-mesh 的根把字节取回本地 ─────────────────────
            pause('现在用 group-mesh 自己的根把这 6 张图取回本地（浏览器里同源 fetch）')
            say(f'取回结果: {fetch_all_through_group_mesh(driver, cache_root)}')
            pause('字节已落本地，回「图片相册」点刷新')

            enter_plugin(driver, '图片相册', 'image-viewer')
            refresh = None
            if wait_for(driver, lambda: len(driver.find_elements(By.ID, 'btn-refresh')) > 0):
                refresh = driver.find_element(By.ID, 'btn-refresh')
            if refresh is not None:
                click(driver, refresh, '点击「刷新」（清掉列表/相册缓存，重新扫描）')
            time.sleep(max(PAUSE, 2.0))
            # 刷新后若回到了相册树（而不是刚才那个目录），再点一次进去。
            if not driver.find_elements(By.CSS_SELECTOR, '.iv-image-card[data-url]'):
                album = next((a for a in driver.find_elements(By.CSS_SELECTOR, '.iv-album')
                              if a.get_attribute('data-path') == namespace), None)
                if album is not None:
                    click(driver, album, f'刷新后回到相册树了，再点进 {namespace}')
                    wait_for(driver, lambda: len(driver.find_elements(
                        By.CSS_SELECTOR, '.iv-image-card[data-url]')) > 0)
            pause('看这一屏：同一批文件，缩略图与布局都正常了')
            shoot(driver, out_dir, '05-after-fetch-refresh')
            say(f'刷新后的网格状态: {grid_state(driver)}')

            if image_cards():
                click(driver, image_cards()[0], '再点一次第一张照片：这次是真图')
                if wait_for(driver, lambda: driver.find_elements(
                        By.CSS_SELECTOR, '.lightbox.active')):
                    pass
                time.sleep(max(PAUSE, 2.0))
                shoot(driver, out_dir, '06-lightbox-real')
                say(f'灯箱状态: {lightbox_state(driver)}')
                close_lightbox(driver)

        say(f'演示结束。截图在 {out_dir}')
        if HOLD > 0:
            say(f'浏览器保持打开 {HOLD:.0f} 秒，你可以自己点点看（Ctrl+C 可提前结束）')
            time.sleep(HOLD)
    except KeyboardInterrupt:
        say('手动中断')
    finally:
        for each in (driver, owner_driver):
            if each is not None:
                try:
                    each.quit()
                except Exception:
                    pass
        cluster.stop()
        # 演示的临时实例目录里是**一次性的身份私钥**，没有保留价值；留着只会在 %TEMP%
        # 里越堆越多。要看就设 OMNIBOX_DEMO_KEEP=1。
        if os.environ.get('OMNIBOX_DEMO_KEEP'):
            say(f'按 OMNIBOX_DEMO_KEEP 保留临时目录：{workdir}')
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
