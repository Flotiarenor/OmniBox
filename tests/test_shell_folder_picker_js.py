"""Shell 共享目录组件（folder-picker）的回归（tests/js/shell_folder_picker.mjs）。

缺陷背景：`window.FolderPicker.createList` 渲染的是**磁盘目录名与完整路径**，而
Linux 的文件名可以含 `<` / `"` / `&`（Windows 禁用这几个字符，所以开发机上很难撞见）。
这套组件被 image-viewer / media-player / manga-library / novel-reader 共用一份实现，
未转义就是一个影响面很宽的注入点。

另一个背景：`tests/js/image_viewer_roots_list.mjs` 一直引用
`tests/js/shell_folder_picker.mjs`（注释里写着"组件本身的行为在这里守"），而该文件
**并不存在** —— 引用了一个不存在的文件，这条守护一直是空的。本用例把它接进 CI。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_shell_folder_picker_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'shell_folder_picker.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过共享目录组件用例')
class ShellFolderPickerTest(unittest.TestCase):
    def test_harness_exists(self):
        """引用方（image_viewer_roots_list.mjs）写着"行为在那里守"：文件必须存在。"""
        self.assertTrue(HARNESS.exists(), f'缺少 {HARNESS}')

    def test_escaping_and_basic_behaviour(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'共享目录组件用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
