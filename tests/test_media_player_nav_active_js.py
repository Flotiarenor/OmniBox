"""media-player 侧栏高亮的互斥用例（`tests/js/media_player_nav_active.mjs`）。

背景：作用域项与扩展入口原先各清各的高亮，点过「网易云登录」再点「全部音乐」会两个
同时亮着（侧栏看起来进了两个视图）。

运行：
    python -m unittest tests.test_media_player_nav_active_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'media_player_nav_active.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过侧栏高亮用例')
class MediaPlayerNavActiveJsTest(unittest.TestCase):
    def test_nav_highlight_is_exclusive(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'侧栏高亮用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
