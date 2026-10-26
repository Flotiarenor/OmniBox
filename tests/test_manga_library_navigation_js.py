"""manga-library 多级返回与阅读器的无头回归（tests/js/manga_library_navigation.mjs）。

返回链（书架 → 章节 → 图片）与阅读器是最内层这两件事以前没有用例。迁移把返回按钮
从内容页搬到壳顶栏，阅读器覆盖层不再挡住返回入口，于是"阅读器打开时点壳返回"
从不可达变成正常路径：内容页必须先关阅读器、再逐级返回。用例同时覆盖共享灯箱与
旧 `.manga-reader` 覆盖层两个分支。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'manga_library_navigation.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 manga-library 返回用例')
class MangaLibraryNavigationJsTest(unittest.TestCase):
    def test_back_chain_and_reader_state(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'manga-library 返回/阅读器用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
