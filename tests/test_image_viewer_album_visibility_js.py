"""image-viewer 前端纯逻辑的无头回归（`tests/js/*.mjs`）。

锁住两组新行为：

- **相册可见性**（`image_viewer_album_visibility.mjs`）：子相册默认折叠
  （只有显式「展开」过的目录才显示下级）；递归都没有可读图片的目录不显示，
  但「新建相册」保留可见、其上级也跟着显示。
- **图片文件夹列表**（`image_viewer_roots_list.mjs`）：主目录行与额外目录行
  一样带移除按钮（`icon:x`，行高一致），删掉主行后下一行顶上，清空列表时提示回退默认目录。

用例直接执行插件前端的真实实现（app.js），不在测试里重写一遍判断逻辑。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESSES = [
    PROJECT_ROOT / 'tests' / 'js' / 'image_viewer_album_visibility.mjs',
    PROJECT_ROOT / 'tests' / 'js' / 'image_viewer_roots_list.mjs',
]


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-viewer 前端逻辑用例')
class ImageViewerFrontendLogicJsTest(unittest.TestCase):
    def _run(self, harness: Path):
        proc = subprocess.run(
            ['node', str(harness)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'{harness.name} 失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')

    def test_album_visibility_rules(self):
        self._run(HARNESSES[0])

    def test_roots_list_rows(self):
        self._run(HARNESSES[1])


if __name__ == '__main__':   # pragma: no cover
    unittest.main()

