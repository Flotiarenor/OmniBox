"""image-viewer 灯箱打开时壳返回/导航先关灯箱（tests/js/image_viewer_lightbox.mjs）。

壳的返回与左栏导航在灯箱之上仍可点击。`createLightbox` 提供 `isOpen()`/`hide()` 后，
插件在收到 `omnibox:action: back` / `omnibox:view` 时应先关灯箱并吞掉本次动作；
否则内容在灯箱背后切层，表现为“点了没反应”。用例同时锁住灯箱已关（普通返回照常）
与旧壳灯箱没有 `isOpen()` 的兼容路径。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'image_viewer_lightbox.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-viewer 灯箱用例')
class ImageViewerLightboxJsTest(unittest.TestCase):
    def test_shell_back_and_nav_close_open_lightbox(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'image-viewer 灯箱用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
