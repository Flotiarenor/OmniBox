"""Shell 共享「扩展入口」渲染的互斥用例（`tests/js/shell_extensions_active.mjs`）。

背景：扩展入口的高亮原先由每个宿主自己写（各自两次 className 查询），media-player 只
写了单向 —— 先点「网易云登录」再点「全部音乐」两个同时高亮；image-viewer 两个方向都写
了才没暴露。现在 `renderExtensions` 统一维护（点扩展清宿主侧栏项、宿主切视图调
`clearActive()`），本用例直接测组件这一层。

运行：
    python -m unittest tests.test_shell_extensions_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'shell_extensions_active.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过扩展入口互斥用例')
class ShellExtensionsActiveJsTest(unittest.TestCase):
    def test_extension_highlight_is_exclusive(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'扩展入口互斥用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
