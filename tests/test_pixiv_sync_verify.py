"""「校验内容」把缺页作品重新入队的插件层测试（走真实的 PixivSyncPlugin）。

链路：本地半截文件（3 页只有 p0）→ `verify_downloaded` 移出去重集合 + 重置 done
→ 下次同步开始前的旧图导入不再把 id 认回来 → 下载阶段只补缺的页，补齐后重新入记。

运行：
    venv/bin/python -m unittest tests.test_pixiv_sync_verify -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PIXIV_BACKEND = PROJECT_ROOT / "plugins" / "pixiv-sync" / "backend"
for _p in (str(PROJECT_ROOT), str(PIXIV_BACKEND), str(PIXIV_BACKEND / "libs")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pixiv_sync import download, store


def _load_plugin_class():
    main_path = PIXIV_BACKEND / "main.py"
    spec = importlib.util.spec_from_file_location("pixiv_sync_plugin_test", str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PixivSyncPlugin


class _FakeImageClient:
    """只记真正会发起的下载：文件已存在时按 pixiv_mini 语义返回 False（不覆盖）。"""

    def __init__(self):
        self.requested = []

    def download(self, url: str, path: str = ".", name: str | None = None) -> bool:
        target = Path(path) / name
        if target.exists() and target.stat().st_size > 0:
            return False
        self.requested.append(url)
        Path(path).mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
        return True


class PixivSyncVerifyTests(unittest.TestCase):
    """一件 3 页作品只下到第 0 页：修复链路必须把它补齐且不被旧图导入撤销。"""

    WORK_ID = 4242
    DONE_ID = 777

    @classmethod
    def setUpClass(cls):
        cls.plugin_class = _load_plugin_class()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.work_dir = self.root / "pixiv" / "A" / str(self.WORK_ID)
        self.work_dir.mkdir(parents=True)
        (self.work_dir / f"{self.WORK_ID}_p0.jpg").write_bytes(b"x")

        cache = self.root / ".cache" / "pixiv-sync"
        cache.mkdir(parents=True)
        (cache / "downloaded_ids.json").write_text(
            json.dumps({"ids": [self.WORK_ID, self.DONE_ID]}), encoding="utf-8"
        )
        (cache / "failed_ids.json").write_text(json.dumps({"ids": []}), encoding="utf-8")

        original = getattr(self.plugin_class, "_resolved_config", None)
        self.plugin_class._resolved_config = {"download_dir": str(self.root)}
        try:
            self.plugin = self.plugin_class({"name": "pixiv-sync"}, {"directories": {}})
        finally:
            self.plugin_class._resolved_config = original

        self.plugin._db().save_pending(
            "following",
            [{
                "id": self.WORK_ID,
                "type": "illust",
                "title": "half",
                "page_count": 3,
                "create_date": "2025-07-09T00:02:37+09:00",
                "user": {"id": 1, "name": "A"},
                "urls": ["u0", "u1", "u2"],
                "tags": [],
                "done": False,
            }],
            {"complete": True},
        )
        self.plugin._db().mark_done("following", [self.WORK_ID])

    def tearDown(self):
        self.plugin.on_unload()  # 关 SQLite：Windows 下不关连接临时目录删不掉（WinError 32）
        self._tmp.cleanup()

    def _done(self) -> bool:
        items, _ = self.plugin._db().load_pending("following")
        return next(i["done"] for i in items if i["id"] == self.WORK_ID)

    def test_verify_requeues_incomplete_work(self):
        """校验内容：缺页作品移出去重集合、done 归 0，本地无文件的失效 id 同样移除。"""
        self.assertTrue(self._done())  # 旧状态：半截下载被当成已下载

        result = self.plugin.verify_downloaded()

        self.assertTrue(result["ok"])
        self.assertEqual(result["incomplete"], 1)
        self.assertEqual(result["stale_removed"], 1)  # 777 本地无文件
        self.assertEqual(store.load_ids(self.plugin._ids_file()), set())
        self.assertFalse(self._done())

    def test_sync_import_does_not_take_partial_work_back(self):
        """校验内容之后，同步开始前的旧图导入不会把缺页作品重新认成已下载。"""
        self.plugin.verify_downloaded()

        found = self.plugin._scan_existing_ids()

        self.assertEqual(found, 0)  # 本地只有缺页作品的文件，一个都不并入
        self.assertNotIn(self.WORK_ID, self.plugin._load_ids())

    def test_download_fills_only_missing_pages_and_records_again(self):
        """重新入队后同步只补缺的页；补齐后作品重新入去重集合、清单 done 置 1。"""
        self.plugin.verify_downloaded()
        self.plugin._scan_existing_ids()
        client = _FakeImageClient()
        self.plugin._pixiv_client = client
        task = {"done": 0, "downloaded": 0, "failed": 0, "skipped": 0, "current": ""}

        download.download_pending(
            self.plugin, task, self.plugin._load_ids(), set(), "following"
        )

        self.assertEqual(client.requested, ["u1", "u2"])  # p0 不再重下
        self.assertEqual(
            sorted(f.name for f in self.work_dir.iterdir()),
            [f"{self.WORK_ID}_p0.jpg", f"{self.WORK_ID}_p1.jpg", f"{self.WORK_ID}_p2.jpg"],
        )
        self.assertIn(self.WORK_ID, self.plugin._load_ids())
        self.assertTrue(self._done())
        self.assertEqual(task["failed"], 0)
        self.assertEqual(task["downloaded"], 1)


if __name__ == "__main__":
    unittest.main()
