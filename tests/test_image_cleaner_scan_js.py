"""image-cleaner 扫描流程（后台任务）的无头前端用例（`tests/js/image_cleaner_scan.mjs`）。

背景：整库扫描改由壳的共享基建 `shell/backend/tasks.py`（`BackgroundTask`）执行
——`scan_start` → 轮询 `scan_status`（进度 + 取消）→ 结束后用 `get_cached_scan` 取结果。
前端不再直呼同步的 `duplicate_scan` / `similar_scan`，这条链路此前没有任何用例覆盖
（image-viewer 的重建与 media-player 的面板都有同类包装器，见 docs/ci-and-release.md）。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_image_cleaner_scan_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'image_cleaner_scan.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-cleaner 扫描流程用例')
class ImageCleanerScanJsTest(unittest.TestCase):
    def test_scan_task_flow(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'扫描流程用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
