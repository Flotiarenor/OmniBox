"""状态聚合：把任务、清单统计、账号与本地校验进度汇总成一次 `get_status` 返回。

为什么聚合单独成组件：状态是首屏唯一的热路径（面板空闲时每 8 秒轮询一次），它读的
东西横跨任务线程、清单库、去重记录与引擎。放在入口会让"读状态"与"跑任务"两种代码
混在一处；放在这里，每一段的失败兜底（数据库读不出来就报 0）也只有一处。
"""

import logging
from typing import Dict, Optional

log = logging.getLogger(__name__)


class StatusReport:
    def __init__(self, paths, records, selection, session, runner, workspace,
                 library, settings) -> None:
        self._paths = paths
        self._records = records
        self._selection = selection
        self._session = session
        self._runner = runner
        self._workspace = workspace
        self._library = library
        self._settings = settings

    def report(self) -> Dict:
        task = self._runner.task_snapshot()
        root = None
        try:
            root = str(self._paths.root())
        except Exception:
            pass
        ids = self._records.downloaded()

        def cnt(db, kind: str, done: Optional[int] = None) -> int:
            try:
                return db.counts(kind, done)
            except Exception:
                return 0

        following_total = following_done = bookmarks_total = bookmarks_done = 0
        try:
            db = self._workspace.db()
            following_total = cnt(db, "following")
            following_done = cnt(db, "following", 1)
            bookmarks_total = cnt(db, "bookmarks")
            bookmarks_done = cnt(db, "bookmarks", 1)
        except Exception as e:
            log.error(f"[pixiv-sync] 读取清单统计失败: {e}")
        library = self._library.state()
        return {
            "task": task,
            "root_dir": root,
            "token_configured": bool((self._settings.setting("refresh_token") or "").strip()),
            "downloaded_total": len(ids),
            "running": self._runner.is_running(),
            "selected_artists": len(self._selection.selected()),
            "selected_file": str(self._paths.selected_file()),
            # 清单统计
            "following_total": following_total,
            "following_done": following_done,
            "pending_following": following_total - following_done,
            "bookmarks_total": bookmarks_total,
            "bookmarks_done": bookmarks_done,
            "pending_bookmarks": bookmarks_total - bookmarks_done,
            "other_done": max(0, len(ids) - following_done - bookmarks_done),
            # 永久跳过（404/已删除）
            "failed_skipped": len(self._records.failed()),
            # 429 冷却倒计时（秒），0 表示无需冷却
            "rate_limit_cooldown": self._session.cooldown_seconds(),
            # 本地内容校验（后台任务）的进度投影：前端据此画进度条与停止按钮
            "library": library,
        }
