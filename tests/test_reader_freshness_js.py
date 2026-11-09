"""document-reader 统一刷新接入的无头回归（tests/js/reader_freshness.mjs）。

切回前台按全部文档根同步、报告有变化才重读文档与书签、阅读态不重绘书架（顶栏书名
不被分类标题覆盖）、零变化与 `skip` 不重读；`cubby:freshness` 与旧的 `refresh`
动作两条通知路径都在用例里覆盖。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'reader_freshness.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 document-reader 刷新用例')
class DocumentReaderFreshnessJsTest(unittest.TestCase):
    def test_freshness_sync_and_reading_guard(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'document-reader 刷新用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
