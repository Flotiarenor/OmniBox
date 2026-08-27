"""pixiv-sync 核心逻辑的离线单元测试。

运行：
    venv/bin/python -m unittest tests.test_pixiv_sync -v
"""

from __future__ import annotations

import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_BACKEND = PROJECT_ROOT / "plugins" / "pixiv-sync" / "backend"
PLUGIN_LIBS = PLUGIN_BACKEND / "libs"
for p in (str(PLUGIN_BACKEND), str(PLUGIN_LIBS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from pixiv_mini import PixivClient, PixivError  # noqa: E402
from pixiv_sync import artist as artist_mod  # noqa: E402
from pixiv_sync import download, scan, store  # noqa: E402
from pixiv_sync import pixiv_purge_non_original as purge  # noqa: E402
from pixiv_sync.db import WorksDB  # noqa: E402
from pixiv_sync.download import process_illust  # noqa: E402


class _StaticLimiter:
    def wait(self):
        pass


class _FakeP:
    """只提供 pixiv_sync 模块需要的方法/属性。"""

    def __init__(self, tmp: Path, *, max_refresh: int = 0, scan_workers: int = 1):
        self.tmp = tmp
        self._cancel_flag = False
        self._task_lock = threading.Lock()
        self._tasks_file = lambda: tmp / ".cache" / "pixiv-sync" / "tasks.json"
        self._rate_limiter = _StaticLimiter()
        self._max_refresh = lambda: max_refresh
        self._scan_workers = lambda: scan_workers
        self._max_download = lambda: 0
        self._workers = lambda: 1
        self._root = lambda: tmp
        self._artists_file = lambda: tmp / ".cache" / "pixiv-sync" / "artists.json"
        self._load_selected_artists = lambda: set()
        self._db_wrapper = WorksDB(
            tmp / ".cache" / "pixiv-sync" / "works.db", self._task_lock
        )

    def _client(self):
        return self.client

    def _db(self):
        return self._db_wrapper


class _FakeDownloadClient:
    def __init__(self, behaviors=None):
        self.behaviors = behaviors or {}

    def download(self, url: str, path: str = ".", name: str | None = None) -> bool:
        action = self.behaviors.get(url, True)
        if action is False:
            raise PixivError(f"download HTTP 404: {url}")
        if isinstance(action, Exception):
            raise action
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / name).write_bytes(b"x")
        return True


def _new_task():
    return {
        "done": 0,
        "downloaded": 0,
        "failed": 0,
        "skipped": 0,
        "current": "",
    }


def _work_item(iid: int, tags=None, uid: int = 1) -> dict:
    return {
        "id": iid,
        "type": "illust",
        "title": f"w{iid}",
        "page_count": 1,
        "create_date": f"2026-01-{iid % 28 + 1:02d}T00:00:00+00:00",
        "user": {"id": uid, "name": f"A{uid}"},
        "urls": [f"https://img/{iid}.jpg"],
        "tags": tags or [],
        "done": False,
    }



def _bookmark_page(iid: int, next_max: int | None):
    return {
        "illusts": [
            {
                "id": iid,
                "type": "illust",
                "title": f"w{iid}",
                "page_count": 1,
                "create_date": "2026-01-01T00:00:00+00:00",
                "user": {"id": 2, "name": "B"},
                "meta_pages": [],
                "meta_single_page": {"original_image_url": f"https://img/{iid}.jpg"},
                "tags": [],
            }
        ],
        "next_url": (
            f"/v1/user/bookmarks/illust?max_bookmark_id={next_max}"
            if next_max is not None
            else None
        ),
    }


class _FakeBookmarkClient:
    user_id = 1

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def parse_qs(self, next_url: str | None):
        if not next_url:
            return None
        return {k: v[-1] for k, v in parse_qs(urlparse(next_url).query).items()}

    def user_bookmarks_illust(self, user_id, **params):
        self.calls.append(dict(params))
        # 调用方会用 max_bookmark_id 作为翻页游标；首页无该参数。
        cursor = int(params.get("max_bookmark_id", 10))
        return self.pages[cursor]


class PixivSyncCoreTests(unittest.TestCase):
    def test_partial_page_failure_is_not_marked_downloaded(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _FakeP(tmp)
            p.client = _FakeDownloadClient({"u1": True, "u2": False})
            ids, failed = set(), set()
            task = _new_task()
            process_illust(
                p,
                {"id": 123, "title": "t", "urls": ["u1", "u2"]},
                task,
                ids,
                failed,
                tmp / "pixiv" / "A",
            )
            self.assertEqual(ids, set())
            self.assertEqual(failed, {123})  # 404 页永久跳过，但不写 ids
            self.assertEqual(task["failed"], 1)
            self.assertEqual(task["done"], 1)

    def test_all_pages_404_adds_permanent_failed_only(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _FakeP(tmp)
            p.client = _FakeDownloadClient({"u1": False})
            ids, failed = set(), set()
            process_illust(
                p,
                {"id": 456, "title": "gone", "urls": ["u1"]},
                _new_task(),
                ids,
                failed,
                tmp / "pixiv" / "A",
            )
            self.assertEqual(ids, set())
            self.assertEqual(failed, {456})

    def test_download_pending_uses_ids_not_stale_db_done(self):
        """删除本地文件 + 刷新记录后，done=1 的清单快照不能阻止重下。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _FakeP(tmp)
            p.client = _FakeDownloadClient({"u3": True})
            item = {
                "id": 124,
                "type": "illust",
                "title": "x",
                "page_count": 1,
                "create_date": "",
                "user": {"id": 1, "name": "A"},
                "urls": ["u3"],
                "tags": [],
                "done": True,  # 扫描时保存的快照；ids 已无此作品
            }
            p._db_wrapper.save_pending("following", [item], {"complete": True})
            task = _new_task()
            download.download_pending(p, task, set(), set(), "following")
            self.assertEqual(task["downloaded"], 1)
            self.assertTrue((tmp / "pixiv" / "A" / "124.jpg").exists())

    def test_bookmark_cursor_uses_max_bookmark_id(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = _FakeP(tmp, max_refresh=1)
            p.client = _FakeBookmarkClient(
                {10: _bookmark_page(10, 9), 9: _bookmark_page(9, 8), 8: _bookmark_page(8, None)}
            )
            task = _new_task()
            items, info = scan.collect_bookmarks_pending(p, task, set())
            self.assertEqual([i["id"] for i in items], [10])
            self.assertIn("next_qs", info)
            self.assertNotIn("offset", info)

            # 第二次应复用断点继续；首页会重拉一次用于去重（不可避免），
            # 之后必须直接使用 max_bookmark_id=9，而不是 offset=None。
            items, info = scan.collect_bookmarks_pending(p, task, set())
            self.assertEqual([i["id"] for i in items], [10, 9])
            self.assertEqual(info["next_qs"], {"max_bookmark_id": "9"})
            self.assertNotIn("offset", info)
            self.assertEqual(p.client.calls[2].get("max_bookmark_id"), "9")

    def test_zero_byte_file_is_not_considered_downloaded(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            root = tmp / "pixiv"
            root.mkdir()
            empty = root / "999_p0.jpg"
            empty.write_bytes(b"")

            self.assertEqual(store.collect_existing_ids(root), set())

            class _FakeResp:
                status_code = 200
                headers = {}
                raw = io.BytesIO(b"real-bytes")

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            client = PixivClient()
            client._request = lambda *a, **kw: _FakeResp()
            result = client.download(
                "https://img/999_p0.jpg", path=str(root), name="999_p0.jpg"
            )
            self.assertTrue(result)
            self.assertEqual((root / "999_p0.jpg").read_bytes(), b"real-bytes")

    def test_store_accepts_legacy_array_format(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "downloaded_ids.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertEqual(store.load_ids(path), {1, 2, 3})
            self.assertTrue(store.save_failed_ids(path, {2, 4}))
            self.assertEqual(store.load_failed_ids(path), {2, 4})

    def test_existing_scan_accepts_p0_without_underscore(self):
        """部分历史文件命名为 123456p0.png（无下划线），也必须能识别。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "pixiv"
            root.mkdir()
            (root / "123456p0.png").write_bytes(b"x")
            (root / "123456_p1.png").write_bytes(b"x")
            self.assertEqual(store.collect_existing_ids(root), {123456})

    def test_artist_collision_new_artist_uses_uid_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache_file = tmp / ".cache" / "pixiv-sync" / "artists.json"
            cache = {"1": "Alice"}
            sub = artist_mod.artist_dir(
                tmp, cache_file, 2, "Alice", cache=cache, persist=False
            )
            self.assertEqual(sub, tmp / "pixiv" / "Alice (2)")
            self.assertEqual(cache["2"], "Alice (2)")

    def test_artist_collision_existing_shared_dir_is_not_moved(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache_file = tmp / ".cache" / "pixiv-sync" / "artists.json"
            cache = {"1": "Alice", "2": "Alice"}
            shared = tmp / "pixiv" / "Alice"
            shared.mkdir(parents=True)
            (shared / "1.jpg").write_bytes(b"x")
            sub1 = artist_mod.artist_dir(tmp, cache_file, 1, "Alice", cache=cache, persist=False)
            sub2 = artist_mod.artist_dir(tmp, cache_file, 2, "Alice", cache=cache, persist=False)
            self.assertEqual(sub1, shared)
            self.assertEqual(sub2, shared)
            self.assertTrue((shared / "1.jpg").exists())

    def test_artist_rename_with_collision_migrates_unique_old_dir(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cache_file = tmp / ".cache" / "pixiv-sync" / "artists.json"
            cache = {"1": "OldA", "2": "Alice"}
            old = tmp / "pixiv" / "OldA"
            old.mkdir(parents=True)
            (old / "10.jpg").write_bytes(b"x")
            sub = artist_mod.artist_dir(
                tmp, cache_file, 1, "Alice", cache=cache, persist=False
            )
            self.assertEqual(sub, tmp / "pixiv" / "Alice (1)")
            self.assertTrue((sub / "10.jpg").exists())

    def test_db_replacing_list_cleans_stale_tags(self):
        with tempfile.TemporaryDirectory() as td:
            db = WorksDB(Path(td) / "works.db", threading.Lock())
            db.save_pending("following", [_work_item(1, ["a", "b"])], {"complete": False})
            db.save_pending("following", [_work_item(1, ["b"])], {"complete": False})
            items, _ = db.load_pending("following")
            self.assertEqual(items[0]["tags"], ["b"])

    def test_db_replace_artist_is_incremental(self):
        with tempfile.TemporaryDirectory() as td:
            db = WorksDB(Path(td) / "works.db", threading.Lock())
            db.save_pending("following", [
                _work_item(11, ["a"], uid=1),
                _work_item(21, ["b"], uid=2),
            ], {"done_uids": [1, 2]})
            db.replace_artist("following", 1, [_work_item(12, ["c"], uid=1)], {"done_uids": [1]})
            items, scan = db.load_pending("following")
            ids = {i["id"] for i in items}
            self.assertEqual(ids, {12, 21})  # 只替换 uid=1，uid=2 保留
            self.assertEqual(scan["done_uids"], [1])

    def test_purge_tool_does_not_delete_original_larger_than_1200(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p1 = tmp / "1300.jpg"
            p2 = tmp / "1200.jpg"
            p3 = tmp / "800.jpg"
            works = {
                1: {"dir": Path("A"), "pages": [p1, p2]},
                2: {"dir": Path("B"), "pages": [p3, p2]},
            }
            sizes = {p1: (1300, 1000), p2: (1200, 900), p3: (800, 600)}
            old = purge.image_size
            purge.image_size = lambda path: sizes.get(path)
            try:
                upgrade, keep = purge.classify(works)
            finally:
                purge.image_size = old
            self.assertNotIn(1, upgrade)  # 1300 页证明已是原图，1200 页也不删
            self.assertIn(2, upgrade)
            self.assertEqual(upgrade[2]["delete"], [p2])  # 只删正好 1200 的页






if __name__ == "__main__":
    unittest.main()
