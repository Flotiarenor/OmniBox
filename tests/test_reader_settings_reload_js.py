"""设置弹窗保存后重读阅读偏好的无头回归（tests/js/reader_settings_reload.mjs）。

缺陷背景：壳的设置弹窗只把值写进后端，既不重载页面也不通知插件页
（`omnibox:settings-changed` 在壳里只有接收分支，见 docs/plugin-guide.md §4.3）。
document-reader 的「设置」按钮没有收尾，于是「正文底色 = 透出壳背景」这一项在弹窗里
改完看不到任何变化；更糟的是页内下一次 save() 会把整份偏好写回后端，内存里的旧值
把刚保存的新值静默盖掉 —— 表现为"这一项永远不生效"。

`ReaderSettingsStore.reload()` 承担收尾：重读后端、重新应用、失败时保持现状。
这段逻辑是纯 JS，Python 侧看不见，因此用 Node + 共享 DOM 替身跑真实脚本。
环境无 node 时跳过。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'reader_settings_reload.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过阅读偏好重读用例')
class ReaderSettingsReloadJsTest(unittest.TestCase):
    def test_reload_applies_saved_settings(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'阅读偏好重读用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
