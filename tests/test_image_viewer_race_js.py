"""image-viewer 请求代次 / 刷新重绘的无头回归（tests/js/image_viewer_race.mjs）。

覆盖迁移后新增的两条竞态守卫（旧请求不重绘、首帧迟到视图只吞一次）与一条真实缺陷：
图片层点壳上的「校验」（或保存设置）时，`loadAlbums()` 走 `showAlbums()` 把层级强制
退回相册列表，刷新反而把用户弹走。用例先在旧实现上红，修复后同时锁住"相册层仍会刷新"。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'image_viewer_race.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-viewer 竞态用例')
class ImageViewerRaceJsTest(unittest.TestCase):
    def test_stale_requests_and_freshness_keep_image_layer(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'image-viewer 竞态用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
