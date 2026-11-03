"""下载根与派生路径的唯一推导处。

状态自持：任务运行期间固定的根、以及"跟设置走的当前根"都在这里。换根由入口编排
（reset / finish_task），不再由任何一处随手改插件实例上的属性。

根有三种来源，优先级从高到低：
  1. 任务运行期间固定的根（begin_task）——任务中途改下载目录会让 SQLite、去重记录
     与落盘目录指向两个不同的根，表现为"下载了但列表里没有"；
  2. `download_dir` 设置；
  3. 宿主 image-viewer 的数据根（未配置下载目录时的默认落点）。
"""

from pathlib import Path
from typing import Callable, Optional

from shell.backend.ports import SettingsPort

from .const import CACHE_SUBDIR


class CachePaths:
    def __init__(self, settings: SettingsPort, host_root: Callable[[], Path]) -> None:
        self._settings = settings
        # 宿主取值延后到第一次真正需要时：宿主实例由壳在依赖全部加载后注入。
        self._host_root = host_root
        self._active_root: Optional[Path] = None

    # ---------- 根 ----------

    def root(self) -> Path:
        """当前下载根；任务运行期间返回固定的那一份。"""
        if self._active_root is not None:
            return self._active_root
        raw = (self._settings.setting("download_dir") or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return Path(self._host_root())

    def begin_task(self) -> None:
        """任务开始：固定本次任务的下载根（重复调用不改变已固定的值）。"""
        if self._active_root is None:
            self._active_root = self.root()

    def finish_task(self) -> None:
        """任务结束：解除固定，下一次取根重新跟设置走。"""
        self._active_root = None

    def reset(self) -> None:
        """设置变更后丢弃固定根；运行中的变更由入口延迟到任务结束再调用。"""
        self._active_root = None

    def pixiv_root(self) -> Path:
        """作品图片的落盘根（<下载根>/pixiv/）。"""
        return self.root() / "pixiv"

    # ---------- 派生路径 ----------

    def cache_dir(self) -> Path:
        return self.root() / CACHE_SUBDIR

    def ids_file(self) -> Path:
        return self.cache_dir() / "downloaded_ids.json"

    def failed_file(self) -> Path:
        return self.cache_dir() / "failed_ids.json"

    def tasks_file(self) -> Path:
        return self.cache_dir() / "tasks.json"

    def artists_file(self) -> Path:
        return self.cache_dir() / "artists.json"

    def works_file(self) -> Path:
        return self.cache_dir() / "works.db"

    def selected_file(self) -> Path:
        return self.cache_dir() / "selected_artists.txt"
