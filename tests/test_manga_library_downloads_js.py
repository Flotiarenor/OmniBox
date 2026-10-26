"""manga-library 下载中心迁移的无头回归（tests/js/manga_library_downloads.mjs）。

下载中心是壳左栏的复合导航 id（`downloads:<filter>`）与四个顶栏动作的宿主：
过滤、任务行、行内暂停/删除/详情、空态与添加任务弹窗都经内容页上报/处理后由壳渲染。
用例锁住这些动作仍调用正确后端、动作的可见/禁用状态随任务变化。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'manga_library_downloads.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 manga-library 下载中心用例')
class MangaLibraryDownloadsJsTest(unittest.TestCase):
    def test_download_center_rows_and_actions(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'manga-library 下载中心用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
