"""manga-library 统一刷新接入的无头回归（tests/js/manga_library_freshness.mjs）。

切回前台按当前作用域同步、报告有变化就地重载（层级不变、阅读器不关）、零变化与
`skip` 不重载、阅读器页码进入「最近阅读」、2 秒任务轮询只在任务变化时重报 chrome。
壳的 `cubby:freshness` 与旧的 `cubby:action: refresh` 两条通知路径都在用例里覆盖。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'manga_library_freshness.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 manga-library 刷新用例')
class MangaLibraryFreshnessJsTest(unittest.TestCase):
    def test_freshness_sync_and_reload(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'manga-library 刷新用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
