"""image-viewer 子文件夹瓦片的数量角标（tests/js/image_viewer_grid_badge.mjs）。

角标原先只在「Pixiv 排序支持」的瓦片下显示，普通排序里作品瓦片看不到页数；
而灯箱打开的是块首（即封面 p0），于是「一个作品有几页」完全不可见。用例锁住
「数量已知就显示、与排序模式无关」与「数量未知不显示」，两条渲染路径都覆盖：
首屏渲染与二级加载补齐。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'image_viewer_grid_badge.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-viewer 数量角标用例')
class ImageViewerGridBadgeJsTest(unittest.TestCase):
    def test_badge_shows_whenever_count_is_known(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'image-viewer 数量角标用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
