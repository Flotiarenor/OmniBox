"""壳交互的真实浏览器冒烟门禁（`ci.yml` 的 `shell-e2e` job 跑的就是这一条）。

为什么需要
----------
`tests/*_e2e.py` 的 9 个文件共 104 条用例都是真实浏览器用例，但全部带
`@skipUnless(selenium)` 一类的条件跳过，而 CI 从不安装 selenium —— 于是"交互与布局
回归"这条路径在 CI 上从来没有门禁：导航不渲染、iframe 不挂载、生命周期通知不送达、
侧栏拖动只在松手后跳一次这类缺陷，只有真实浏览器 + 真实壳能发现。

更隐蔽的是"跳过也算成功"：一个 job 里跑 `python -m unittest <模块>` 时，缺 selenium /
缺 Chrome / 前端没构建都会让类整体跳过，退出码仍然是 0。门禁看起来在跑，实际什么都没
验证。因此这个入口把"没跑起来"统一当失败处理，只有真的跑过、且一条都没被跳过，才以 0 退出。

跑哪几条
--------
按"覆盖面最广、最先能发现回归"选两条壳交互链路（其余 7 个 e2e 仍留在本地手动跑）：

1. `tests/test_shell_browser_e2e.py`：接口数据 → 导航渲染 → iframe 挂载 → 生命周期通知，
   外加壳加载时控制台零错误。壳整体坏没坏，先看这条。
2. `tests/test_splitter_browser_e2e.py`：壳侧栏拖动逐帧跟随指针、松手落盘、把手净占位为 0、
   设置页滑块与拖动共用同一支变量。这些是只看拖动前后两次取值抓不到的回归。

怎么让它真的能跑
----------------
1. 前端必须先构建（`npm --prefix shell/frontend run build`）：壳首页由
   `shell/frontend/dist/index.html` 提供，dist 缺失时 `/` 直接 404。
2. 浏览器定位分两层：`tests/harness/browser_binary.py` 只认 `chrome` / `chrome.exe` 与
   Windows 安装目录；Linux runner 上的浏览器叫 `google-chrome` / `chromium`。这里先按
   更宽的名字搜索，找不到时把找到的可执行文件以 `chrome` 的名字挂进一个临时目录并放到
   `PATH` 最前面，让 harness 自己的判据命中 —— 不修改被测用例，也不复制浏览器本体。

用法
----
    venv/Scripts/python.exe tools/shell_e2e_smoke.py     # 本地（需 Chrome + selenium）
    python tools/shell_e2e_smoke.py                      # CI 的 shell-e2e job

依赖：`requirements-e2e.txt`（selenium）与已构建的 `shell/frontend/dist`。

退出码：0 全部用例通过且一条都没跳过；1 有失败 / 有用例被跳过 / 用例数少于预期；
2 环境不具备（缺 selenium、缺 Chrome、缺前端产物）。2 也是失败 —— 环境退化不该表现为
"通过"，否则门禁会随 runner 镜像变化静默失效。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 要跑的用例模块与它们各自的最小用例数。只许增不许减：少一条说明有人删了断言，
#: 改这里才放行。
MODULES: tuple[tuple[str, int], ...] = (
    ('tests.test_shell_browser_e2e', 7),
    ('tests.test_splitter_browser_e2e', 7),
)

#: Linux runner 上的浏览器名字（harness 只认 `chrome`，见模块文档）。
_CHROME_NAMES = (
    'google-chrome',
    'google-chrome-stable',
    'chromium',
    'chromium-browser',
    'chrome',
)
_CHROME_PATHS = (
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/opt/google/chrome/chrome',
)


def _dist_built() -> bool:
    return (PROJECT_ROOT / 'shell' / 'frontend' / 'dist' / 'index.html').is_file()


def _resolve_chrome() -> str | None:
    """先按 harness 的判据找，再按更宽的名字/路径找。"""
    from tests.harness.browser_binary import browser_binary

    found = browser_binary()
    if found is not None:
        return found

    override = os.environ.get('OMNIBOX_CHROME_BINARY', '').strip()
    if override and Path(override).is_file():
        return override

    for name in _CHROME_NAMES:
        which = shutil.which(name)
        if which is not None:
            return which

    for candidate in _CHROME_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def _expose_to_harness(binary: str) -> Path | None:
    """让 `tests/harness/browser_binary.py` 也能找到这个浏览器。

    返回临时目录（调用方负责删除）；harness 本来就找得到时返回 None、不做任何改动。
    Linux 上建符号链接即可；Windows 只有"从别处找到 chrome.exe"这一种情况会走到这里，
    同卷用硬链接，跨卷才退回复制（复制 100+MB 只发生在本地非常规布局上）。
    """
    from tests.harness.browser_binary import browser_binary

    if browser_binary() is not None:
        return None

    shim_dir = Path(tempfile.mkdtemp(prefix='omnibox-chrome-shim-'))
    shim = shim_dir / ('chrome.exe' if os.name == 'nt' else 'chrome')
    if os.name == 'nt':
        try:
            os.link(binary, shim)
        except OSError:
            shutil.copy2(binary, shim)
    else:
        shim.symlink_to(binary)
    os.environ['PATH'] = str(shim_dir) + os.pathsep + os.environ.get('PATH', '')
    return shim_dir


def _fail(message: str) -> int:
    print(f'shell_e2e_smoke: {message}', file=sys.stderr)
    return 2


def _run_modules() -> int:
    """顺序跑每条模块；任何一个模块失败、跳过或不足预期条数都返回 1。"""
    total = 0
    skipped: list[str] = []
    bad: list[str] = []

    for module, min_tests in MODULES:
        print(f'shell_e2e_smoke: 运行 {module}（至少 {min_tests} 条）', flush=True)
        suite = unittest.TestLoader().loadTestsFromName(module)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        total += result.testsRun
        for test, reason in result.skipped:
            # 跳过 = 这条门禁没有验证任何东西；把跳过当失败，否则环境退化会被当成通过。
            skipped.append(f'{test}: {reason}')
        if result.testsRun < min_tests:
            bad.append(f'{module} 只跑了 {result.testsRun} 条用例（预期至少 {min_tests}）')
        if not result.wasSuccessful():
            bad.append(f'{module} 有用例失败')

    if skipped:
        print(f'shell_e2e_smoke: {len(skipped)} 条用例被跳过', file=sys.stderr)
        for line in skipped:
            print(f'  - {line}', file=sys.stderr)
    for line in bad:
        print(f'shell_e2e_smoke: {line}', file=sys.stderr)
    if skipped or bad:
        return 1

    print(f'shell_e2e_smoke: OK（{total} 条用例通过，0 跳过）', flush=True)
    return 0


def main() -> int:
    try:
        import selenium  # noqa: F401  # pyright: ignore[reportMissingImports]
    except Exception:
        return _fail('未安装 selenium（pip install -r requirements-e2e.txt）——门禁不能跳过')

    if not _dist_built():
        return _fail('缺 shell/frontend/dist/index.html'
                     '（先 npm --prefix shell/frontend run build）——门禁不能跳过')

    binary = _resolve_chrome()
    if binary is None:
        return _fail('找不到 Chrome/Chromium（设 OMNIBOX_CHROME_BINARY 或装一个）'
                     '——门禁不能跳过')

    print(f'shell_e2e_smoke: 浏览器 {binary}', flush=True)
    shim_dir = _expose_to_harness(binary)
    try:
        return _run_modules()
    finally:
        if shim_dir is not None:
            shutil.rmtree(shim_dir, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
