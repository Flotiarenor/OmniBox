"""Shell 分隔条内核（splitter）注册表生命周期用例（`tests/js/shell_splitter.mjs`）。

背景：`splitter.js` 的 `items` 注册表只增不删 —— 插件每次重渲染都产生新的
`[data-obx-splitter]` 元素，`relayout()` 对已移除元素只是 `return`。而把手（handle）
是插在元素后面的**兄弟节点**，元素被移除时它不会跟着走，于是注册表与 DOM 一起随
重渲染线性增长。本用例锁住"relayout 时摘掉已移除元素及其把手"。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_shell_splitter_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'shell_splitter.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过分隔条用例')
class ShellSplitterJsTest(unittest.TestCase):
    def test_registry_cleanup(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'{HARNESS.name} 用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
