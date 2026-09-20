"""「远端下载目录」字段不该出现「网络位置」入口 —— 真壳真渲染的验证。

问题：设置里的 `directory` 字段默认允许「网络位置」来源（把远端共享项取回本地），
但 group-mesh 的「远端下载目录」**本身就是取回文件的落点**，在那里选网络位置语义
不成立（等于拿取回的中间目录当下载目录）。修法是 schema 声明 `local_only: True`。

这里走真实壳服务与真实插件页：进插件 iframe → 调壳的 `openSettingsModal()`（与用户
点「设置」同一条路径）→ 在设置弹窗里找该字段，断言没有网络位置按钮、其余控件仍在；
同时断言别的字段（媒体文件夹那种）**仍然有**，避免"改错方向"也算通过。

用法：
    venv/Scripts/python.exe tests/debug_settings_local_only_ui.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    print(f'  {"PASS" if ok else "FAIL"}  {name}{(" — " + detail) if detail else ""}')
    if not ok:
        FAILURES.append(f'{name} ({detail})')


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _wait_health(base_url: str, timeout: float = 40.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f'{base_url}/health', timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def _wait_for(predicate, timeout: float = 20.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def open_plugin_settings_directly(driver, base_url: str, plugin: str) -> bool:
    """直接打开插件页并调壳的 openSettingsModal（不依赖导航与 iframe 切换顺序）。

    比走导航更稳：壳在 `serve_plugin_frontend` 里对**每个**插件页注入同一份 base.js，
    所以 `http://<host>/plugins/<name>/frontend/index.html` 里就有
    `openSettingsModal`、`FolderPicker` 与 `Bridge`（Bridge 的插件前缀也由注入脚本设好）。
    """
    driver.get(f'{base_url}/plugins/{plugin}/frontend/index.html')
    if not _wait_for(lambda: driver.execute_script(
            "return typeof window.openSettingsModal === 'function';")):
        return False
    driver.execute_script("window.openSettingsModal({ title: '验证设置字段' });")
    return _wait_for(lambda: driver.execute_script(
        "return !!document.querySelector('.settings-form .field[data-key]');"))


def open_plugin_settings(driver, plugin: str, nav_label: str) -> bool:
    """进插件 iframe 并调壳的 openSettingsModal（与点「设置」同一条路径）。

    开头必须先 `switch_to.default_content()`：上一次调用结束时停在**上一个插件的
    iframe** 里，不回到顶层就 `switch_to.frame()` 会嵌进旧 iframe 的内部 frame，
    结果读到的还是上一个插件的设置（本文件第一版就这样把 media-player 的对照用例
    跑成了 group-mesh 的 schema）。
    """
    from selenium.webdriver.common.by import By
    driver.switch_to.default_content()
    nav = next((e for e in driver.find_elements(By.CSS_SELECTOR, '.nav-item')
                if nav_label in e.text), None)
    if nav is None:
        return False
    nav.click()
    if not _wait_for(lambda: len(driver.find_elements(By.TAG_NAME, 'iframe')) > 0):
        return False
    driver.switch_to.frame(driver.find_element(By.TAG_NAME, 'iframe'))
    ok = _wait_for(lambda: driver.execute_script(
        "return typeof window.openSettingsModal === 'function';"))
    if not ok:
        return False
    driver.execute_script("window.openSettingsModal({ title: '验证设置字段' });")
    # 设置弹窗由壳注入的 base.js 渲染；等它出现
    return _wait_for(lambda: driver.execute_script(
        "return !!document.querySelector('.settings-form .field[data-key]');"))


def main() -> int:
    from selenium import webdriver

    port = _free_port()
    base_url = f'http://127.0.0.1:{port}'
    server = subprocess.Popen(
        [sys.executable, 'main.py', '--web-only', '--port', str(port)],
        cwd=str(PROJECT_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_health(base_url):
        server.terminate()
        print('壳服务未在超时内就绪')
        return 1

    options = webdriver.ChromeOptions()
    for arg in ('--headless=new', '--disable-gpu', '--no-sandbox',
                '--window-size=1400,900'):
        options.add_argument(arg)
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(base_url)
        _wait_for(lambda: driver.execute_script(
            "return document.querySelectorAll('.nav-item').length > 0;"))
        time.sleep(1.0)

        print('\n[group-mesh：远端下载目录 = local_only]')
        opened = open_plugin_settings(driver, 'group-mesh', '团体组网')
        check('能打开插件设置弹窗（壳的 openSettingsModal 通路可用）', opened)
        if opened:
            info = driver.execute_script("""
              var field = document.querySelector('.settings-form .field[data-key="download_dir"]');
              if (!field) { return {found: false}; }
              var addRow = field.querySelector('.iv-roots-add');
              return {
                found: true,
                hasNetwork: !!field.querySelector('[data-act="network"]'),
                hasBrowse: !!field.querySelector('[data-act="browse"]'),
                hasAdd: !!field.querySelector('[data-act="add"]'),
                label: (field.querySelector('.field-label') || {}).textContent || '',
                html: addRow ? addRow.innerHTML.slice(0, 120) : ''
              };""")
            check('设置里能定位到 download_dir 字段', info.get('found'), str(info)[:120])
            check('字段确实叫「远端下载目录」', '远端下载目录' in (info.get('label') or ''),
                  info.get('label'))
            # 断言文案里不直接写 emoji：Windows 控制台按 GBK 编码，打印 emoji 会抛
            # UnicodeEncodeError 把用例带崩（实测）
            check('没有「网络位置」入口', not info.get('hasNetwork'), info.get('html'))
            check('「浏览…」与「添加」仍在', info.get('hasBrowse') and info.get('hasAdd'),
                  info.get('html'))
        driver.switch_to.default_content()

        print('\n[对照：未声明 local_only 的目录列表仍给「网络位置」入口]')
        # 直接验共享组件本身而不是"媒体播放器的设置弹窗"：后者的打开路径随插件实现
        # 而异（实测它不在壳的 openSettingsModal 通路上），而这条不变量与插件无关 ——
        # 壳的目录列表默认必须有网络位置入口，只有声明 local_only 的字段才没有。
        driver.get(f'{base_url}/plugins/media-player/frontend/index.html')
        check('媒体播放器页面上有壳注入的 FolderPicker', _wait_for(
            lambda: driver.execute_script("return typeof window.FolderPicker === 'object';")))
        control = driver.execute_script("""
          var normal = FolderPicker.createList({ paths: [] });
          var local = FolderPicker.createList({ paths: [], localOnly: true });
          return {
            normalNetwork: !!normal.element.querySelector('[data-act="network"]'),
            normalBrowse: !!normal.element.querySelector('[data-act="browse"]'),
            localNetwork: !!local.element.querySelector('[data-act="network"]')
          };""")
        check('默认目录列表有「网络位置」入口', control.get('normalNetwork'),
              str(control))
        check('默认目录列表仍有「浏览…」', control.get('normalBrowse'), str(control))
        check('声明 local_only 的列表没有「网络位置」入口', not control.get('localNetwork'),
              str(control))
    finally:
        driver.quit()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print(f'\n{"全部通过" if not FAILURES else "失败 " + str(len(FAILURES)) + " 项"}')
    for item in FAILURES:
        print('  -', item)
    return 1 if FAILURES else 0


if __name__ == '__main__':
    raise SystemExit(main())
