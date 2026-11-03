"""任务运行期的工作区：清单 SQLite 连接、任务文件与下载根的固定。

状态自持：懒连接、关闭时机、根固定标记都在这里。清单库与去重集合共用入口创建的
同一把可重入锁（迁移前就是同一把），因此这里不再造第二把 —— 换锁会改变并发语义。
"""

from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional

from . import tasks as tasks_mod
from .db import WorksDB


class TaskWorkspace:
    def __init__(self, paths, lock: RLock) -> None:
        self._paths = paths
        self._lock = lock
        self._db: Optional[WorksDB] = None

    # ---------- 清单（SQLite） ----------

    def db(self) -> WorksDB:
        """懒连接清单库：首次使用才建库（双检锁，与调用方共享同一把锁）。"""
        if self._db is None:
            with self._lock:
                if self._db is None:
                    self._db = WorksDB(self._paths.works_file(), self._lock)
        return self._db

    def close_db(self) -> None:
        """释放清单连接。

        不关连接会留下 -wal/-shm 边车文件并一直占着 works.db —— Windows 下文件被
        占用时连复制/移动数据目录都会失败。
        """
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ---------- 任务状态文件 ----------

    def tasks_file(self) -> Path:
        return self._paths.tasks_file()

    def load_task(self) -> Optional[Dict[str, Any]]:
        return tasks_mod.load_task(self.tasks_file())

    def persist_task(self, task: Dict[str, Any]) -> bool:
        return tasks_mod.persist_task(self.tasks_file(), task)

    # ---------- 下载根的固定 ----------

    def begin(self) -> None:
        self._paths.begin_task()

    def finish(self) -> None:
        self._paths.finish_task()
