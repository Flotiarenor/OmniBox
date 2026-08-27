"""任务状态机：创建 / 落盘 / 恢复（断点）。"""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


def new_task(kind: str) -> Dict[str, Any]:
    task = {
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
    return task


def persist_task(path: Path, task: Dict[str, Any]):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def load_task(path: Path) -> Optional[Dict[str, Any]]:
    try:
        t = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(t, dict) and t.get("state") in ("queued", "running"):
            t["state"] = "paused"  # 上次中断/重启，标记为可续跑
        return t
    except Exception:
        return None