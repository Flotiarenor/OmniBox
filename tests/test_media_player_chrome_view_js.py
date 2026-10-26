"""media-player 与壳的左栏高亮契约（`omnibox:chrome.view`）驱动。

旧用例 `tests/js/media_player_nav_active.mjs` 在本区间被删除（abffebc）：它断言
`_setNavActive` 与 `.mp-nav-item`，而侧栏已改由壳渲染。本用例补回行为等价的断言：
一级视图 / 网易云扩展视图 / 二级页 / 本地歌单详情各自上报什么 `view`，以及切回一级
视图后不会留下扩展高亮 —— 侧栏高亮的唯一输入就是这份上报。

无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_media_player_chrome_view_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'media_player_chrome_view.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 media-player chrome.view 用例')
class MediaPlayerChromeViewJsTest(unittest.TestCase):
    def test_view_id_reporting(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'media-player chrome.view 用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
