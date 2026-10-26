"""共享内容卡片/行（shell/frontend/public/shell/content-ui.js）的纯逻辑回归。

content-ui.js 注入每个插件 iframe，无法在 Python 里直接执行 DOM/键盘逻辑；
这里用 Node + DOM 替身跑真实脚本（tests/js/content_ui.mjs）。环境无 node 时跳过。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'content_ui.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 ContentUI 前端用例')
class ContentUiJsTest(unittest.TestCase):
    def test_card_row_api_keyboard_and_actions(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'{HARNESS.name} 用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
