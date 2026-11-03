"""数据根与派生路径：漫画根、书架状态文件、下载状态落点。

状态自持：根与由它派生的路径都在这里；换根走 `rebind()`，由入口编排。

下载状态目录建不出来时回落到用户主目录：旧 download-center 就是这条兜底落点，
任务记录必须继续找得到，否则换一个只读的漫画根就等于丢掉全部未完成任务。
回落逻辑跟着路径走，不散在下载中心里。

目录名与文件名由入口从 `const.py` 读出来当构造参数注入（组件之间不互相 import）。
"""

import os
from pathlib import Path


class PathLayout:
    """漫画根的派生路径。

    只有入口构造它；组件收它当依赖，不自己拼路径 —— 「书架状态放在哪、下载记录
    放在哪」改一次就够，不会出现两处写向不同目录。
    """

    def __init__(self, root: Path, download_dirname: str, download_filename: str) -> None:
        self._download_dirname = download_dirname
        self._download_filename = download_filename
        self.rebind(root)

    def rebind(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.state_file = self.root / '.cache' / 'manga_state.json'

    def download_state_dir(self) -> str:
        """下载状态目录；首选漫画根下的隐藏目录，不可写时回落到用户主目录。"""
        path = os.path.join(str(self.root), self._download_dirname)
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except (PermissionError, OSError):
            fallback = os.path.join(os.path.expanduser('~'), self._download_dirname)
            os.makedirs(fallback, exist_ok=True)
            return fallback

    def download_state_file(self) -> str:
        """下载任务表的文件名（目录由 `download_state_dir` 决定）。"""
        return os.path.join(self.download_state_dir(), self._download_filename)
