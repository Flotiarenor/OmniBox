"""媒体播放器播放起点 / 进度记忆的前端逻辑回归（tests/js/media_player_playback.mjs）。

缺陷背景：曲目播完后媒体元素的 currentTime 停在 duration，切歌瞬间 `_loadItem` 先调
`_saveProgress()`，把这个"已播完"位置写回 localStorage，覆盖掉 `ended` 里的 `clear()`。
下次点击该曲目就续播到末尾，元素停在末尾时 `play()` 不出声 —— 实测表现为"点击曲目不是
从头开始而是从最后开始""放完一遍后再也播不动"，视频同样中招。

player-core.js 是纯前端脚本，Python 侧无法直接驱动，这里用 Node + stub 媒体元素跑真实
脚本。环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'media_player_playback.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过前端播放逻辑用例')
class MediaPlayerPlaybackJsTest(unittest.TestCase):
    def test_resume_and_progress_memory(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'播放起点逻辑用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
