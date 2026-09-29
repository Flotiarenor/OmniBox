"""Shell 统一设置表单（`createSettingsForm`）的渲染/提交契约（tests/js/shell_settings_form.mjs）。

背景：设置表单此前只有"能改的字段"——`text / number / range / checkbox / select /
textarea / directory`。image-cleaner 要在设置里显示"相册根目录"（这一行原先画在内嵌视图
自己的工具栏上，那排已整条去掉），而根目录由宿主 image-viewer 决定、在插件里不可改，
用任何一个现有类型都只能渲染成可编辑输入框。

本用例锁住新增的只读信息行 `type: "info"`：值渲染得出来（`values[key]` 或 schema 的
`value`）、没有输入控件、且**不参与提交**（`getValues()` 里没有这个键）。后端一侧
（`plugin_base.save_settings` 把 info 字段排除在可写键外）由
`tests/test_settings_info_field.py` 覆盖。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_shell_settings_form_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'shell_settings_form.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过设置表单用例')
class ShellSettingsFormTest(unittest.TestCase):
    def test_info_field_renders_and_is_not_submitted(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'设置表单用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
