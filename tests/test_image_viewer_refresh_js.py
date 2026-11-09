"""image-viewer 刷新作用域与重载判定的无头回归（tests/js/image_viewer_refresh.mjs）。

锁住四件事：作用域按层取值（图片层 = 当前目录，相册层与时间线「按图片」层 = 全根）、
`cubby:chrome` 带上 `freshness_scope`、报告驱动的重载判定（有变化才重载，目录集合
信号 `new_dirs` / `vanished_dirs` 也算变化，`skip` 与零变更都不重载）、以及缩略图版本
号优先取封面文件自身的 mtime 且精确到毫秒。壳侧的触发时机由 `shell_freshness` 用例覆盖。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'image_viewer_refresh.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-viewer 刷新作用域用例')
class ImageViewerRefreshJsTest(unittest.TestCase):
    def test_scope_and_reload_judgement(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'image-viewer 刷新作用域用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
