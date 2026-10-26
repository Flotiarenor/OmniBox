"""image-viewer 设置保存失败的无头回归（tests/js/image_viewer_settings.mjs）。

后端 `PluginBase.save_settings` 校验 number/range 失败时整次不落盘并返回
`{success:false, errors}`；内容页设置弹窗以前忽略返回值，照常提示「设置已保存」、
更新内存并关窗（同一 payload 里的 root_dir 也没写进去）。用例先在旧实现上红，
修复后锁住失败契约与成功路径的原有行为。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'image_viewer_settings.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-viewer 设置保存用例')
class ImageViewerSettingsJsTest(unittest.TestCase):
    def test_save_failure_keeps_form_and_shows_field_errors(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'image-viewer 设置保存用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
