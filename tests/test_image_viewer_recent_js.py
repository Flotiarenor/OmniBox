"""image-viewer 时间线「按相册 / 按图片」切换的无头回归（tests/js/image_viewer_recent.mjs）。

覆盖新增的图片层：时间线顶部自绘分段控件、「按图片」用 `list_recent_images` 的更新单元
（作品卡 + 单图）铺 justified 网格、作品卡/单图的灯箱起始下标按前面单元张数累加、
状态栏区分单元数与张数（partial 用 warn）、切回相册 / 壳返回的退出路径、在途响应不重绘
的代次守卫，以及图片层重载走 `_reloadCurrentView()`（不误用 `list_folder_items`）。

后端接口全部用 Bridge 打桩，不依赖真实图库或后端实现。

运行：
    python -m unittest tests.test_image_viewer_recent_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'image_viewer_recent.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-viewer 最近图片用例')
class ImageViewerRecentImagesJsTest(unittest.TestCase):
    def test_timeline_mode_switch_and_recent_unit_layer(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'image-viewer 最近图片用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
