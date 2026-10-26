"""document-reader 阅读态刷新/加载/进度轨/空态的无头回归（tests/js/reader_refresh_state.mjs）。

迁移到壳页面骨架后，壳顶栏的标题与返回由内容页上报的动作驱动。统一刷新
（`omnibox:action: refresh`）会重扫书架，但阅读态下不能顺手把顶栏标题改回书架标题；
同文件还锁住加载提示的挂载容器、进度轨显隐、读取失败与空分类的区分。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'reader_refresh_state.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 document-reader 状态用例')
class DocumentReaderRefreshStateJsTest(unittest.TestCase):
    def test_reading_state_survives_refresh(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'document-reader 状态用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
