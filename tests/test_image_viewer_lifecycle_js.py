"""image-viewer 前端生命周期迁移的无头回归（tests/js/image_viewer_lifecycle.mjs）。

image-viewer 是更新频率最高、监听器最多的插件（addEventListener 35 : removeEventListener 0），
其幻灯片每 3 秒 setInterval 换图且不随后台停止 —— 切到别的插件后仍在消耗 CPU 并继续拉缩略图。
本用例锁住迁移结果：onHide 停定时器并记住位置、onShow 原位继续、onDispose 摘掉 resize 监听器。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'image_viewer_lifecycle.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-viewer 生命周期用例')
class ImageViewerLifecycleJsTest(unittest.TestCase):
    def test_slideshow_stops_on_hide_and_listener_is_removed(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'image-viewer 生命周期用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
