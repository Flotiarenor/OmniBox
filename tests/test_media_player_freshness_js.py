"""media-player 统一刷新接入的无头回归（tests/js/media_player_freshness.mjs）。

切回前台按全部媒体根同步、报告有变化就地重载（专辑与歌单详情停在原地）、零变化与
`skip` 不重载、网易云顶栏「刷新」先同步壳侧引擎再清本地缓存并重拉、封面 URL 优先用
后端的封面文件 mtime。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'media_player_freshness.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 media-player 刷新用例')
class MediaPlayerFreshnessJsTest(unittest.TestCase):
    def test_freshness_sync_and_reload(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'media-player 刷新用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
