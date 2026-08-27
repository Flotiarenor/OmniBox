"""任务状态机：创建 / 落盘 / 恢复（断点）。"""

import json
import os
import tempfile
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


def persist_task(path: Path, task: Dict[str, Any]) -> bool:
    """原子写入 tasks.json，避免进程中断留下半个 JSON 文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(task, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        print(f"[pixiv-sync] 保存 tasks.json 失败: {e}")
        return False


def load_task(path: Path) -> Optional[Dict[str, Any]]:
    try:
        t = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(t, dict) and t.get("state") in ("queued", "running"):
            t["state"] = "paused"  # 上次中断/重启，标记为可续跑
        return t
    except Exception:
        return None