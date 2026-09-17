"""「网络位置」的现场演示：非无头、慢速点击、逐步截图（手动运行，不是 unittest）。

它把这条链完整走一遍，让人看着它点：

1. 起两台空白实例（各自一份 OMNIBOX_HOME），建团、共享一个含真实 JPEG 的目录、起节点、登记对端；
2. 在「图片相册」的设置弹窗里点「🌐 网络位置」→ 壳的共享目录组件按 placement 发现提供方
   （group-mesh 的 `get_extensions()`）→ 弹窗里嵌入提供方页面；
3. 提供方页面里：选设备 → 选共享项 → 填本地落地目录 → 取回（`mirror_share`：**完整取字节**，
   不是留占位）；
4. 提供方把本地目录 `postMessage` 回填给组件 → 列表里出现该目录 + 「团体组网 · 设备/共享」标签
   （组件会校验 `event.source`，所以这一步同时验证了这条通路）；
5. 打开相册树里的那个目录 → 缩略图、布局、**原图**都正常。

注意（历史背景：缺陷③曾逼出这个落点选择）
------------------------------------------
镜像落在**第一根之下**（用户图库里的一个普通文件夹）时，原图走壳的相对路径分支 → 按
`roots[0]` 解析 → 正常打开。把它加成人生的"额外根"时原图曾 404（壳的 `/file` 相对路径
分支只认第一根，见 `.dsh/group-mesh-materialize.md` §2.2）——该缺陷已于 2026-09-17 修复
（`PluginBase.resolve_file_path`，提交 `820cc4d`）。本演示**仍然不保存设置**：同一目录既
以"第一根子目录"又以"额外根"出现会让演示画面重复，与那个缺陷无关。

运行
----
    venv\\Scripts\\python tests\\debug_network_location.py

可选环境变量：OMNIBOX_DEMO_PAUSE（每步停顿秒数，默认 2.5）、OMNIBOX_DEMO_HOLD
（结束后保持浏览器打开秒数，默认 300）、OMNIBOX_DEMO_OUT（截图目录）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.debug_materialized_gallery import (
    HOLD,
    PAUSE,
    _force_utf8_stdout,
    click,
    enter_plugin,
    pause,
    say,
    shoot,
    wait_for,
)
from tests.harness.app_instance import MeshCluster, boot_prerequisites
from tests.test_media_player_browser_e2e import _chrome_binary, _chromedriver_path

# 四张不同长宽比：宽高正确时是正常的 justified 瀑布流（这是"字节真的到本地了"的肉眼证据）
PHOTOS = {
    '旅行/宽幅全景.jpg': (1200, 300, (46, 120, 200)),
    '旅行/竖构图.jpg': (400, 1200, (190, 70, 90)),
    '旅行/方构图.jpg': (800, 800, (110, 110, 130)),
    '旅行/标准横构图.jpg': (1000, 700, (60, 160, 90)),
}
SHARE_ID = 'trip'
MIRROR_DIR_NAME = '来自朋友的旅行'
LEAF = '旅行'


def build_shared(root: Path) -> Path:
    from PIL import Image

    shared = root / 'shared'
    for rel, (width, height, color) in PHOTOS.items():
        target = shared / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (width, height), color).save(target, 'JPEG', quality=88)
    return shared


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
                   or (PROJECT_ROOT / '.build' / 'demo-network-location'))
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix='omnibox-netloc-'))
    shared = build_shared(workdir)

    say('准备两台空白实例并组网')
    cluster = MeshCluster(workdir / 'instances', names=('owner', 'viewer'))
    owner, viewer = cluster.instances
    cluster.start()
    cluster.form_group()
    cluster.call(owner, 'add_share', {'share_id': SHARE_ID, 'path': str(shared), 'read': 'group'})
    cluster.link(viewer, cluster.start_nodes()[0], name='owner')

    # 落点选在**看图那台的图库根之下**：镜像成为一个普通文件夹（原图因此能打开）。
    # 目录必须先存在（mirror_share 只往已存在的目录里写）。
    mirror_dir = Path(viewer.data_root) / MIRROR_DIR_NAME
    mirror_dir.mkdir(parents=True, exist_ok=True)
    say(f'本次的落地目录（用户自己选的，在图片库里）：{mirror_dir}')

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By

    options = Options()
    options.binary_location = binary
    # 刻意不加 --headless：这个脚本的意义就是让人看着它点
    options.add_argument('--window-size=1500,950')
    options.add_argument('--window-position=0,0')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-gpu')
    driver_path = _chromedriver_path()
    driver = (webdriver.Chrome(options=options, service=Service(driver_path))
              if driver_path else webdriver.Chrome(options=options))
    driver.set_page_load_timeout(60)

    plugin_frame = 'iframe[data-plugin-name="image-viewer"]'
    try:
        say(f'打开看图那台设备：{viewer.base_url}')
        driver.get(viewer.base_url)
        wait_for(driver, lambda: len(driver.find_elements(By.CSS_SELECTOR, '.nav-item')) > 0)

        # ── 1. 指给用户看「🌐 网络位置」在哪 ────────────────────────────────────
        enter_plugin(driver, '图片相册', 'image-viewer')
        wait_for(driver, lambda: len(driver.find_elements(By.ID, 'btn-settings')) > 0)
        click(driver, driver.find_element(By.ID, 'btn-settings'), '点「设置」打开显示与排序设置')
        wait_for(driver, lambda: driver.find_elements(By.CSS_SELECTOR, '#settings-modal.active'))
        pause('「图片文件夹」区块：输入框旁边就是新增的「🌐 网络位置」按钮')
        shoot(driver, out_dir, '01-settings-with-network-button')

        # ── 2. 点它：组件发现提供方 → 弹窗里嵌提供方页面 ────────────────────────
        click(driver, driver.find_element(By.CSS_SELECTOR, '#setting-roots [data-act="network"]'),
              '点「🌐 网络位置」：壳去发现提供方并弹出它的页面')
        if not wait_for(driver, lambda: driver.find_elements(By.CSS_SELECTOR, '.iv-network-frame')):
            say('提供方 iframe 没出现，后面的演示跳过')
        else:
            pause('弹窗里嵌的就是提供方页面（group-mesh 的子页面）')
            shoot(driver, out_dir, '02-provider-modal')

            # ── 3. 提供方页面：选设备 → 选共享项 → 填目录 → 取回 ────────────────
            driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, '.iv-network-frame'))
            if wait_for(driver, lambda: driver.find_elements(By.CSS_SELECTOR, '[data-device]')):
                click(driver, driver.find_elements(By.CSS_SELECTOR, '[data-device]')[0], '选择设备')
                wait_for(driver, lambda: driver.find_elements(By.CSS_SELECTOR, '[data-share]'))
                pause('共享项列出来了')
                shoot(driver, out_dir, '03-provider-pick-share')
                click(driver, driver.find_elements(By.CSS_SELECTOR, '[data-share]')[0], '选择共享项')

                dest = driver.find_element(By.ID, 'nl-dest-input')
                dest.clear()
                dest.send_keys(str(mirror_dir))
                filled = driver.execute_script(
                    "return document.getElementById('nl-dest-input').value;")
                say(f'填好落地目录：{filled}')
                run_btn = driver.find_element(By.ID, 'nl-run')
                say(f'「取回」按钮当前 disabled={run_btn.get_attribute("disabled") is not None}')
                shoot(driver, out_dir, '04-provider-destination')
                click(driver, run_btn, '点「取回到该目录」')
                pause('正在取回（完成后提示并把目录回填给组件）')
            else:
                say('提供方页面里没有设备，后面的演示跳过')

            # ── 4. 回填 → 组件关弹窗 → 列表里出现该目录 ─────────────────────────
            driver.switch_to.default_content()
            driver.switch_to.frame(driver.find_element(By.CSS_SELECTOR, plugin_frame))
            closed = wait_for(driver,
                              lambda: not driver.find_elements(By.CSS_SELECTOR, '.iv-network-frame'),
                              60.0)
            pause('弹窗已关闭（说明回填消息被组件接受）' if closed else '弹窗没关掉，看截图')
            rows = driver.find_elements(By.CSS_SELECTOR, '#setting-roots .iv-root-row')
            say(f'列表里现在 {len(rows)} 行：'
                + ' | '.join(r.text.replace('\n', ' ') for r in rows))
            shoot(driver, out_dir, '05-backfilled-into-list')
            click(driver, driver.find_element(By.ID, 'settings-cancel'),
                  '关掉设置弹窗（本次刻意不保存，原因见脚本说明）')

        # ── 5. 相册树里那个目录：缩略图 / 布局 / 原图 ──────────────────────────
        # 先点「刷新」：相册索引有 30 秒 TTL，刚由"网络位置"建出来的目录还没被扫到
        # （这也是真实用户加完一个位置后会做的事）。
        wait_for(driver, lambda: len(driver.find_elements(By.ID, 'btn-refresh')) > 0)
        click(driver, driver.find_element(By.ID, 'btn-refresh'), '点「刷新」让相册树重新扫描')
        time.sleep(max(PAUSE, 1.5))
        pause('回「全部相册」：镜像目录就是一个普通文件夹，出现在图库里')
        wait_for(driver, lambda: len(driver.find_elements(By.CSS_SELECTOR, '.iv-album')) > 0)
        say(f'相册节点：{[a.get_attribute("data-path") for a in driver.find_elements(By.CSS_SELECTOR, ".iv-album")]}')
        shoot(driver, out_dir, '06-albums-with-mirror-folder')

        leaf_path = f'{MIRROR_DIR_NAME}/{LEAF}'
        node = next((a for a in driver.find_elements(By.CSS_SELECTOR, '.iv-album')
                     if a.get_attribute('data-path') == MIRROR_DIR_NAME), None)
        if node is None:
            say(f'没找到 {MIRROR_DIR_NAME} 节点，网格演示跳过')
        else:
            click(driver, node, f'点进 {MIRROR_DIR_NAME}')
            # 它只有子目录、没有直接图片 → 子相册网格（瓦片带 data-path）
            wait_for(driver, lambda: len(driver.find_elements(By.CSS_SELECTOR, '.iv-image-card')) > 0)
            tiles = [c for c in driver.find_elements(By.CSS_SELECTOR, '.iv-image-card')
                     if c.get_attribute('data-path')]
            say(f'子目录瓦片：{[c.get_attribute("data-path") for c in tiles]}')
            # 按精确路径优先，找不到就用第一个子目录瓦片（本演示的镜像里只有一个）
            tile = next((c for c in tiles if c.get_attribute('data-path') == leaf_path), None) \
                or (tiles[0] if tiles else None)
            if tile is None:
                say('没有子目录瓦片，网格演示跳过')
            else:
                click(driver, tile, f'点进子目录（{tile.get_attribute("data-path")}）')
                wait_for(driver, lambda: len(driver.find_elements(
                    By.CSS_SELECTOR, '.iv-image-card[data-url]')) > 0)
                pause('网格：缩略图与布局都正常（字节是完整取回来的，不是占位）')
                shoot(driver, out_dir, '07-mirrored-grid')

                cards = driver.find_elements(By.CSS_SELECTOR, '.iv-image-card[data-url]')
                if cards:
                    click(driver, cards[0], '点第一张照片：这次原图能打开')
                    wait_for(driver, lambda: driver.find_elements(By.CSS_SELECTOR, '.lightbox.active'))
                    time.sleep(max(PAUSE, 2.0))
                    shoot(driver, out_dir, '08-lightbox-real-original')
                    info = driver.execute_script(
                        "const i=document.querySelector('#lightbox-img');"
                        "return i ? (i.naturalWidth + 'x' + i.naturalHeight) : 'no-img';")
                    say(f'灯箱里的原图尺寸：{info}（不是破图就说明原图真的取到了）')

        say(f'演示结束。截图在 {out_dir}')
        if HOLD > 0:
            say(f'浏览器保持打开 {HOLD:.0f} 秒，你可以自己点点看（Ctrl+C 可提前结束）')
            time.sleep(HOLD)
    except KeyboardInterrupt:
        say('手动中断')
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        cluster.stop()
        # 演示的临时实例目录里是**一次性的身份私钥**，没有保留价值；留着只会在 %TEMP%
        # 里越堆越多（实测：跑几轮就攒了 11 个）。要看就设 OMNIBOX_DEMO_KEEP=1。
        if os.environ.get('OMNIBOX_DEMO_KEEP'):
            say(f'按 OMNIBOX_DEMO_KEEP 保留临时目录：{workdir}')
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
