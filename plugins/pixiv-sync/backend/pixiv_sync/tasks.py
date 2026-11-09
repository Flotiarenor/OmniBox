"""任务状态机：创建 / 落盘 / 恢复（断点）。

落盘走内核 `shell.backend.json_cache` 的唯一原子写入口（临时文件 + fsync +
os.replace）：任务状态每张一写，直接覆写时进程被杀会留下半个 JSON，下次启动读不出来，
"断点续跑"就退化成"从头再来"。
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from shell.backend.json_cache import write_json

log = logging.getLogger(__name__)


def new_task(kind: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "state": "queued",
        "done": 0,
        "total": 0,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "current": "",
        "started_at": time.time(),
        "finished_at": None,
        "error": None,
    }


def persist_task(path: Path, task: Dict[str, Any]) -> bool:
    """原子写入 tasks.json。"""
    try:
        ok = write_json(path, task, indent=2, create_parent=True)
    except Exception as e:
        log.error(f"[pixiv-sync] 保存 tasks.json 失败: {e}")
        return False
    if not ok:
        log.error(f"[pixiv-sync] 保存 tasks.json 失败: {path}")
    return ok


def load_task(path: Path) -> Optional[Dict[str, Any]]:
    try:
        t = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(t, dict) and t.get("state") in ("queued", "running"):
            t["state"] = "paused"  # 上次中断/重启，标记为可续跑
        return t
    except Exception:
        return None
