"""待下载清单 SQLite 存储（works.db）。

表：works（待下载作品）/ tags / work_tags / meta（扫描断点）。
跨线程共享连接，写操作统一由外部传入的锁保护。
"""

import json
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

def _item_to_work_value(item: Dict[str, Any]) -> tuple:
    """item dict → works 行（除 id/kind 外的剩余列）。"""
    user = item.get("user") or {}
    return (
        item.get("type"),
        str(item.get("title") or ""),
        int(item.get("page_count") or 1),
        item.get("create_date"),
        int(user.get("id") or 0),
        str(user.get("name") or ""),
        json.dumps(item.get("urls") or [], ensure_ascii=False),
        json.dumps(item.get("tags") or [], ensure_ascii=False),
        1 if item.get("done") else 0,
    )


def _work_to_item(row: tuple, tags: List[str]) -> Dict[str, Any]:
    """works 行 → item dict（与旧格式兼容）。"""
    (wid, wtype, title, page_count, create_date, uid, uname, urls_json, tags_json, done) = row
    return {
        "id": wid,
        "type": wtype,
        "title": title,
        "page_count": page_count,
        "create_date": create_date,
        "user": {"id": uid, "name": uname},
        "urls": json.loads(urls_json or "[]"),
        "tags": tags or json.loads(tags_json or "[]"),
        "done": bool(done),
    }


class WorksDB:
    def __init__(self, path: Path, lock: Lock):
        self._path = path
        self._lock = lock
        self._conn: Optional[sqlite3.Connection] = None

    def conn(self) -> sqlite3.Connection:
        """懒连接（首次使用才建库），与调用方共享锁。"""
        if self._conn is None:
            with self._lock:
                if self._conn is None:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(str(self._path), check_same_thread=False)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS tags ("
                        " id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL)"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS works ("
                        " id INTEGER PRIMARY KEY, kind TEXT NOT NULL,"
                        " title TEXT, type TEXT, page_count INTEGER, create_date TEXT,"
                        " user_id INTEGER, user_name TEXT,"
                        " urls TEXT, tags_json TEXT,"
                        " done INTEGER DEFAULT 0)"
                    )
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS work_tags ("
                        " work_id INTEGER, tag_id INTEGER, PRIMARY KEY(work_id, tag_id))"
                    )
                    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_works_kind_done ON works(kind, done)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name)")
                    conn.commit()
                    self._conn = conn
        return self._conn

    # ---------- 清单读写 ----------

    def save_pending(self, kind: str, items: List[Dict[str, Any]], scan: Optional[dict] = None):
        """把 kind 的清单整体写入 SQLite（事务内替换）。scan=None 时不改断点。"""
        conn = self.conn()
        with self._lock:
            try:
                conn.execute("BEGIN")
                conn.execute("DELETE FROM works WHERE kind=?", (kind,))
                for item in items:
                    try:
                        wid = int(item["id"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO works"
                        " (id, kind, type, title, page_count, create_date, user_id, user_name, urls, tags_json, done)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (wid, kind) + _item_to_work_value(item),
                    )
                    for tname in item.get("tags") or []:
                        tname = str(tname)
                        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tname,))
                        row = conn.execute("SELECT id FROM tags WHERE name=?", (tname,)).fetchone()
                        if row:
                            conn.execute(
                                "INSERT OR IGNORE INTO work_tags(work_id, tag_id) VALUES (?,?)",
                                (wid, row[0]),
                            )
                if scan is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                        (f"scan_{kind}", json.dumps(scan, ensure_ascii=False)),
                    )
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[pixiv-sync] 保存清单失败: {e}")

    def save_pending_preserve(self, kind: str, items: List[Dict[str, Any]], scan: Optional[dict] = None):
        """保存清单；scan 为空时保留现有断点（与扫描结束保存语义一致）。"""
        if scan is None:
            _, scan = self.load_pending(kind)
        self.save_pending(kind, items, scan)

    def load_pending(self, kind: str) -> Tuple[List[Dict[str, Any]], Optional[dict]]:
        """读取清单，返回 (items, scan)。出错时为空清单。"""
        try:
            conn = self.conn()
            with self._lock:
                rows = conn.execute(
                    "SELECT id, type, title, page_count, create_date, user_id, user_name, urls, tags_json, done"
                    " FROM works WHERE kind=?",
                    (kind,),
                ).fetchall()
                tag_map: Dict[int, List[str]] = {}
                for wid, tname in conn.execute(
                    "SELECT wt.work_id, t.name FROM work_tags wt JOIN tags t ON t.id=wt.tag_id"
                ).fetchall():
                    tag_map.setdefault(wid, []).append(tname)
                items = [_work_to_item(r, tag_map.get(r[0])) for r in rows]
                scan = None
                row = conn.execute("SELECT value FROM meta WHERE key=?", (f"scan_{kind}",)).fetchone()
                if row:
                    try:
                        scan = json.loads(row[0])
                    except Exception:
                        scan = None
            return items, scan
        except Exception:
            return [], None

    def counts(self, kind: str, done: Optional[int] = None) -> int:
        try:
            conn = self.conn()
            with self._lock:
                if done is None:
                    row = conn.execute("SELECT COUNT(*) FROM works WHERE kind=?", (kind,)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM works WHERE kind=? AND done=?", (kind, done)
                    ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def mark_done(self, kind: str, ids):
        """把清单中的作品标记为已下载（保留在清单，供统计）。"""
        conn = self.conn()
        with self._lock:
            try:
                conn.execute("BEGIN")
                for iid in ids:
                    conn.execute("UPDATE works SET done=1 WHERE id=? AND kind=?", (iid, kind))
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[pixiv-sync] 更新清单 done 失败: {e}")