"""两台实例之间的连接与共享访问：非无头现场演示（手动运行，不是 unittest）。

本脚本在**界面**上（两个真实浏览器窗口）验证两台设备之间的连接与共享访问是否可用，
不以终端断言代替观察：

1. 起两台空白实例（各自一份 `OMNIBOX_HOME`，见 `tests/harness/app_instance.py`）：
   `owner`（提供共享项那台）与 `member`（访问那台）。建团、owner 共享一个含三份真实
   文件的目录、两台都起共享节点；
2. 把两台设备**真实的监听地址程序化登记**进各自的手工对端（`MeshCluster.link()`）——
   等价于用户粘贴一次对方地址，脚本不要求人工输入连接信息；
3. 左窗口（`member`）：「团体组网」→「远端共享」→「刷新设备」→ 设备与共享项出现 →
   点共享项 → 列目录 → 「取回」一份中文名文件，界面给出落点；
4. 同一个窗口点「上传文件到此处」：把 `member` 本机的一份文件投放到 `owner` 的共享项，
   弹窗里能看到进度 → 完成后目录自动刷新、新文件出现在列表里；
5. 右窗口（`owner`）：「刷新状态」看团体/名单/节点/共享项，再「刷新设备」确认 `member`
   也在自己的设备列表里（双向可达）。
6. 结论以**字节**为准：脚本对两侧文件取 sha256 做对比，并逐项打印设备发现（双向）、
   列目录、取回一致、上传一致的结论。

每个阶段之间停顿 `OMNIBOX_DEMO_PAUSE` 秒；结束时保持浏览器打开 `OMNIBOX_DEMO_HOLD` 秒。

运行
----
    venv\\Scripts\\pip install -r requirements-e2e.txt        # 只需一次
    venv\\Scripts\\python tests\\debug_connection.py

可选环境变量
------------
    OMNIBOX_DEMO_PAUSE     每步停顿秒数（默认 2.0；设 0 可以快速跑一遍）
    OMNIBOX_DEMO_HOLD      演示结束后保持浏览器打开秒数（默认 300；设 0 立即关）
    OMNIBOX_DEMO_OUT       截图输出目录（默认 .build/demo-connection/）
    OMNIBOX_DEMO_UPLOAD    设 0 则跳过第 4 步（只看连接、发现与取回）
    OMNIBOX_DEMO_KEEP      设 1 则保留临时实例目录（内含本次的一次性身份私钥）
    OMNIBOX_CHROME_BINARY / OMNIBOX_CHROMEDRIVER   同其它 e2e 脚本

注意
----
* 它会真的打开两个可见 Chrome 窗口并在你桌面上点击；演示期间不要手动操作那两个窗口
  （会打乱脚本的元素定位）。两个窗口都开着同一个插件页，靠窗口标题下的地址区分：
  `owner` 在右上、`member` 在左上。
* 演示用的临时实例目录结束时会被删掉（里面是一次性的身份私钥）；要看就设
  `OMNIBOX_DEMO_KEEP=1`。
"""

from __future__ import annotations

import hashlib
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

SHARE_ID = 'handover'
# 三份共享内容：中文名 + 子目录 + 纯 ASCII。字节是随机的，用于 sha256 对比。
SHARED_FILES = {
    '交接说明.txt': None,                 # None = 用文本内容（见 _write_shared）
    '数据/blob.bin': 300 * 1024,          # 300 KB 随机字节，跨多个分块
    'note.txt': None,
}
DOWNLOAD_NAME = '交接说明.txt'
UPLOAD_NAME = '回执 2026.bin'
UPLOAD_BYTES = 1200 * 1024                # 1.2 MB：大到足以在弹窗里看到进度


def _random_bytes(size: int) -> bytes:
    return os.urandom(size)


def _write_shared(shared: Path) -> None:
    """铺共享目录：三份文件，其中一份在子目录里。"""
    for rel, size in SHARED_FILES.items():
        target = shared / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if size is None:
            target.write_text(f'来自 owner 的 {rel}\n', encoding='utf-8')
        else:
            target.write_bytes(_random_bytes(size))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def plugin_downloads_dir(instance) -> Path:
    """插件给的下载目录：`<实例数据根>/group-mesh/downloads`（设置项可改）。"""
    return Path(instance.data_root) / 'group-mesh' / 'downloads'


def watch_upload(driver, seconds: float) -> str:
    """采样上传弹窗里的进度文本，把变化过的都打出来，返回最后一次文本。

    为什么用 `textContent` 而不是 Selenium 的 `.text`：上传完成时弹窗会被关掉
    （`.modal` 去掉 `active`），而 `.text` 对隐藏元素返回空串 —— 于是"已上传 …"
    这行完成文案读不到，脚本只拿到上传早期的帧（实测：进度停在 0%，而文件已传完
    且两侧 sha256 一致）。

    为什么要采样而不是点完截一帧：1 MB 级文件走回环不到 1 秒就传完，肉眼与截图都可能
    只看到最后一帧；采样能把中间帧留在终端里，作为进度回传生效的证据。
    """
    from selenium.webdriver.common.by import By

    script = ("const b = document.getElementById('upload-progress');"
              "return b ? (b.textContent || '') : '';")
    seen: list[str] = []
    deadline = time.time() + seconds
    closed = False
    while time.time() < deadline:
        try:
            text = (driver.execute_script(script) or '').strip()
        except Exception:
            text = ''
        if text and (not seen or seen[-1] != text):
            seen.append(text)
            say(f'  进度：{text}')
        if text.startswith(('已上传', '上传失败', '上传被中断', '已取消')):
            break
        # 弹窗没了且没有完成文案：通常是传输过快，不必耗满整个采样预算
        if not closed and not driver.find_elements(By.CSS_SELECTOR, '#upload-box.active'):
            closed = True
            time.sleep(0.5)
            text = (driver.execute_script(script) or '').strip()
            if text:
                if not seen or seen[-1] != text:
                    seen.append(text)
                    say(f'  进度：{text}')
                break
        time.sleep(0.1)
    return seen[-1] if seen else ''


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
                   or (PROJECT_ROOT / '.build' / 'demo-connection'))
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix='omnibox-conn-'))
    shared = workdir / 'shared'
    _write_shared(shared)

    # ── 1. 两台空白实例 + 建团 + 起节点 ──────────────────────────────────────
    say('准备两台空白实例（owner / member）并建团')
    cluster = MeshCluster(workdir / 'instances', names=('owner', 'member'))
    owner, member = cluster.instances
    cluster.start()
    cluster.form_group()
    cluster.call(owner, 'add_share',
                 {'share_id': SHARE_ID, 'path': str(shared), 'read': 'group',
                  'write': 'group'})
    owner_endpoint, member_endpoint = cluster.start_nodes()
    say(f'owner 的节点监听：{owner_endpoint}')
    say(f'member 的节点监听：{member_endpoint}')

    # ── 2. 程序化登记地址（这一步替代"手工粘贴对方地址"）─────────────────────
    cluster.link(member, owner_endpoint, name='owner')
    cluster.link(owner, member_endpoint, name='member')
    say('已把两台设备的监听地址登记进各自的手工对端（脚本不要求手工输入）')

    # 待上传的文件放在 member 的本机用户目录里（插件拒绝上传身份目录与物化缓存）
    upload_source = Path(member.user_data) / UPLOAD_NAME
    upload_source.write_bytes(_random_bytes(UPLOAD_BYTES))
    say(f'待上传文件：{upload_source}（{UPLOAD_BYTES // 1024} KB）')

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By

    driver_path = _chromedriver_path()

    def make_driver(position: str):
        options = Options()
        options.binary_location = binary
        # 刻意不加 --headless：这个脚本的意义就是让人看着它点
        options.add_argument('--window-size=1100,950')
        options.add_argument(f'--window-position={position}')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-blink-features=AutomationControlled')
        if driver_path:
            return webdriver.Chrome(options=options, service=Service(driver_path))
        return webdriver.Chrome(options=options)

    member_driver = make_driver('0,0')
    member_driver.set_page_load_timeout(60)
    owner_driver = None
    # None = 未执行该步（被环境变量跳过或前置条件不满足）；True/False = 实测结论
    verdict: dict[str, bool | None] = {'download': None, 'upload': None}
    seen_both_ways = False
    try:
        owner_driver = make_driver('1110,0')
        owner_driver.set_page_load_timeout(60)
        say(f'右窗口 = owner（{owner.base_url}），左窗口 = member（{member.base_url}）')
        for drv, instance in ((owner_driver, owner), (member_driver, member)):
            drv.get(instance.base_url)
            wait_for(drv, lambda d=drv: len(d.find_elements(By.CSS_SELECTOR, '.nav-item')) > 0)
        pause('两个窗口都打开了壳的首页（导航在左边）')

        # ── 3. owner 窗口：本机状态（谁在名单里、节点在听、共享了什么）────────
        enter_plugin(owner_driver, '团体组网', 'group-mesh')
        wait_for(owner_driver, lambda: len(owner_driver.find_elements(By.ID, 'btn-refresh')) > 0)
        click(owner_driver, owner_driver.find_element(By.ID, 'btn-refresh'),
              '（右窗口）点「刷新状态」')
        pause('右窗口：本机身份 / 团体与名单 / 共享节点 / 共享项都在这一屏')
        shoot(owner_driver, out_dir, '01-owner-status')

        roster_text = owner_driver.find_element(By.ID, 'roster-body').text
        node_text = owner_driver.find_element(By.ID, 'node-body').text
        shares_text = owner_driver.find_element(By.ID, 'shares-body').text
        say(f'  名单：{roster_text.replace(chr(10), " | ")}')
        say(f'  节点：{node_text.replace(chr(10), " | ")}')
        say(f'  共享项：{shares_text.replace(chr(10), " | ")}')

        # owner 也刷新设备：它应当在设备列表里看到 member（双向可达）。
        # 注意判据是 `#peers-body` 的**文本**而不是 `.gm-remote-item`：后者只在
        # "该设备有共享项"时才有——member 没有共享项，它的条目里只有名字与说明。
        click(owner_driver, owner_driver.find_element(By.ID, 'btn-peer-refresh'),
              '（右窗口）点「刷新设备」：owner 也要看到 member')
        wait_for(owner_driver, lambda: 'member' in owner_driver.find_element(
            By.ID, 'peers-body').text, 30.0)
        pause('右窗口的设备列表：member 在这里（说明连接是双向的）')
        shoot(owner_driver, out_dir, '02-owner-sees-member')
        owner_peers = owner_driver.find_element(By.ID, 'peers-body').text
        say(f'  owner 的设备页：{owner_peers.replace(chr(10), " | ")}')
        seen_both_ways = 'member' in owner_peers

        # ── 4. member 窗口：发现 owner → 列共享项 → 取回 ──────────────────────
        enter_plugin(member_driver, '团体组网', 'group-mesh')
        wait_for(member_driver, lambda: len(member_driver.find_elements(
            By.ID, 'btn-peer-refresh')) > 0)
        click(member_driver, member_driver.find_element(By.ID, 'btn-peer-refresh'),
              '（左窗口）点「刷新设备」：从登记过的地址连过去，找回对方的共享清单')
        if not wait_for(member_driver, lambda: len(member_driver.find_elements(
                By.CSS_SELECTOR, '.gm-remote-item')) > 0, 30.0):
            say('左窗口没有列出任何设备/共享项 —— 看截图与实例日志')
            shoot(member_driver, out_dir, '03-member-no-peer')
        else:
            pause('左窗口：owner 的设备及其共享项出现了')
            shoot(member_driver, out_dir, '03-member-sees-owner')
            share_item = next((e for e in member_driver.find_elements(
                By.CSS_SELECTOR, '.gm-remote-item')
                if e.get_attribute('data-share') == SHARE_ID), None)
            if share_item is None:
                say(f'没找到共享项 {SHARE_ID}，后面的取回/上传演示跳过')
            else:
                click(member_driver, share_item, f'（左窗口）点共享项 {SHARE_ID}：列它的目录')
                wait_for(member_driver, lambda: len(member_driver.find_elements(
                    By.CSS_SELECTOR, '[data-download]')) > 0, 30.0)
                pause('左窗口：目录列出来了（连接可用的第一层证据）')
                shoot(member_driver, out_dir, '04-member-lists-files')

                # 取回一份文件：落点在下载目录，界面会显示出来
                target = next((b for b in member_driver.find_elements(
                    By.CSS_SELECTOR, '[data-download]')
                    if b.get_attribute('data-download') == DOWNLOAD_NAME), None)
                if target is None:
                    say(f'列表里没有 {DOWNLOAD_NAME}，取回演示跳过')
                else:
                    click(member_driver, target, f'（左窗口）点「取回」{DOWNLOAD_NAME}')
                    wait_for(member_driver, lambda: '已取回' in member_driver.find_element(
                        By.ID, 'remote-progress').text, 60.0)
                    pause('左窗口的进度行给出了本机落点')
                    shoot(member_driver, out_dir, '05-member-downloaded')
                    local = plugin_downloads_dir(member) / SHARE_ID / DOWNLOAD_NAME
                    if local.is_file():
                        same = sha256(local) == sha256(shared / DOWNLOAD_NAME)
                        verdict['download'] = same
                        say(f'  取回校验：{local}')
                        say(f'  sha256 一致 = {same}')
                    else:
                        say(f'  没找到取回的文件：{local}')

                # ── 5. member → owner 上传（进度 / 提交 / 目录刷新）──────────────
                if os.environ.get('OMNIBOX_DEMO_UPLOAD') == '0':
                    say('按 OMNIBOX_DEMO_UPLOAD=0 跳过上传演示')
                elif not wait_for(member_driver, lambda: len(member_driver.find_elements(
                        By.ID, 'btn-remote-upload')) > 0):
                    say('没看到「上传文件到此处」按钮，上传演示跳过')
                else:
                    click(member_driver, member_driver.find_element(By.ID, 'btn-remote-upload'),
                          '（左窗口）点「上传文件到此处」')
                    wait_for(member_driver, lambda: len(member_driver.find_elements(
                        By.CSS_SELECTOR, '#upload-box.active')) > 0)
                    local_input = member_driver.find_element(By.ID, 'upload-local')
                    local_input.clear()
                    local_input.send_keys(str(upload_source))
                    name_input = member_driver.find_element(By.ID, 'upload-name')
                    name_input.clear()
                    name_input.send_keys(UPLOAD_NAME)
                    pause('弹窗里填好了本机文件与目标文件名')
                    shoot(member_driver, out_dir, '06-upload-form')
                    click(member_driver, member_driver.find_element(By.ID, 'btn-do-upload'),
                          '点「上传」：观察进度行')
                    final = watch_upload(member_driver, 30.0)
                    pause(f'上传结束：{final or "（没有读到进度文本）"}')
                    shoot(member_driver, out_dir, '07-upload-done')

                    landed = shared / UPLOAD_NAME
                    if landed.is_file():
                        same = sha256(landed) == sha256(upload_source)
                        verdict['upload'] = same
                        say(f'  owner 侧落盘：{landed}')
                        say(f'  sha256 一致 = {same}（对端已收下并在目录里刷新出来）')
                    else:
                        verdict['upload'] = False
                        say(f'  owner 侧没有这个文件：{landed}')

                    # 目录刷新后新文件应当出现在列表里（界面这一层的证据）
                    appeared = wait_for(member_driver, lambda: any(
                        b.get_attribute('data-download') == UPLOAD_NAME
                        for b in member_driver.find_elements(By.CSS_SELECTOR, '[data-download]')),
                        30.0)
                    say(f'  上传后目录里出现 {UPLOAD_NAME} = {appeared}')
                    shoot(member_driver, out_dir, '08-member-refreshed-listing')

        # ── 6. 结论 ────────────────────────────────────────────────────────────
        def show(value: bool | None) -> str:
            return '未执行' if value is None else ('通过' if value else '不一致')

        say('—— 结论 ——')
        say('设备发现（member → owner）: '
            + ('通过' if len(member_driver.find_elements(
                By.CSS_SELECTOR, '.gm-remote-item')) > 0 else '失败'))
        say('设备发现（owner → member）: ' + ('通过' if seen_both_ways else '失败'))
        say('列远端目录               : '
            + ('通过' if len(member_driver.find_elements(By.CSS_SELECTOR, '[data-download]')) > 0
               else '失败'))
        say(f'取回字节一致             : {show(verdict["download"])}')
        say(f'上传字节一致             : {show(verdict["upload"])}')
        say(f'截图在 {out_dir}')
        if HOLD > 0:
            say(f'浏览器保持打开 {HOLD:.0f} 秒，你可以自己点点看（Ctrl+C 可提前结束）')
            time.sleep(HOLD)
    except KeyboardInterrupt:
        say('手动中断')
    finally:
        for drv in (member_driver, owner_driver):
            if drv is None:
                continue
            try:
                drv.quit()
            except Exception:
                pass
        cluster.stop()
        # 临时实例目录里是**一次性的身份私钥**，没有保留价值；留着只会在 %TEMP% 里越堆越多。
        if os.environ.get('OMNIBOX_DEMO_KEEP'):
            say(f'按 OMNIBOX_DEMO_KEEP 保留临时目录：{workdir}')
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
