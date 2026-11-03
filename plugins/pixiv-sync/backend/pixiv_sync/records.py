"""去重集合与永久跳过集合的内存态 + 落盘。

状态自持：两份懒加载的集合都在这里。**内存里的集合对象必须跨保存保持不变**
（就地增删），因为任务线程持有同一个 set 对象；换成新对象会让下载线程随后写回的
记录全部丢失。落盘由 store 的原子写入口负责，这里只决定"什么时候写"。
"""

import logging
from pathlib import Path
from typing import Optional, Set

from . import store

log = logging.getLogger(__name__)


class IdRecords:
    def __init__(self, paths) -> None:
        self._paths = paths
        self._downloaded: Optional[Set[int]] = None
        self._failed: Optional[Set[int]] = None

    # ---------- 已下载（去重记录） ----------

    def downloaded(self) -> Set[int]:
        """已下载作品 id 集合（同一对象，就地增删）。"""
        if self._downloaded is None:
            self._downloaded = store.load_ids(self.ids_file())
        return self._downloaded

    def save_downloaded(self) -> bool:
        return store.save_ids(self.ids_file(), self.downloaded())

    # ---------- 永久跳过（404/已删除） ----------

    def failed(self) -> Set[int]:
        if self._failed is None:
            self._failed = store.load_failed_ids(self.failed_file())
        return self._failed

    def save_failed(self) -> bool:
        return store.save_failed_ids(self.failed_file(), self.failed())

    def clear_failed(self) -> None:
        """清空内存里的失败集合（下一次 save_failed 落盘成空记录）。"""
        self._failed = set()

    # ---------- 换根 ----------

    def reset(self) -> None:
        """下载根变了：丢弃内存态，下一次访问从新根的缓存文件重读。"""
        self._downloaded = None
        self._failed = None

    def ids_file(self) -> Path:
        return self._paths.ids_file()

    def failed_file(self) -> Path:
        return self._paths.failed_file()
