"""给浏览器 e2e 用例定位 Chrome 可执行文件。

为什么需要这个模块
------------------
`webdriver.Chrome` 只会去配 **ChromeDriver**，它驱动不了 Edge。而各 e2e 文件里的
`_have_browser()` 会把 Edge 也算作"有浏览器"（Windows 自带），于是用例在只有 Edge 的
机器上照跑，随后以 `unrecognized Chrome version: Edg/...` 失败 —— 明明是环境不具备，
却表现成用例红了。这里把"哪个可执行文件才真的能用"收敛到一处。

顺序
----
1. 仓库内 `.build/cft/chrome-win64/chrome.exe`：chrome-for-testing 的免安装包，
   与项目在 Linux CI 里用的东西同源（见 `tests/test_media_player_browser_e2e.py`
   模块文档里的 OMNIBOX_CHROME_BINARY 用法）。`.build/` 在 `.gitignore` 里，
   因此这份是可选的本地便利，不是仓库内容。
2. PATH（`chrome` / `chrome.exe`）与两个常见安装位置。

刻意**不**回落到 Edge：见上。只有 Edge 的机器应当跳过这些用例，而不是跑红。
"""

from __future__ import annotations

import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def browser_binary() -> str | None:
    """可用于 `webdriver.Chrome` 的 Chrome 可执行文件；没有则 None。"""
    local = PROJECT_ROOT / '.build' / 'cft' / 'chrome-win64' / 'chrome.exe'
    if local.is_file():
        return str(local)
    for exe in ('chrome', 'chrome.exe'):
        found = shutil.which(exe)
        if found:
            return found
    for candidate in (
        Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'),
        Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'),
    ):
        if candidate.is_file():
            return str(candidate)
    return None
