"""Shell 共享组件「同步 / 校验」的进度卡片用例（`tests/js/shell_freshness_card.mjs`）。

背景：被动同步报 `partial` 时，组件原先只弹卡片、**没有真的起校验**，卡片停在
静态占位符「0 / 0」上永不消失（`renderCard` 在"没有任务"时直接 return，
`onSettled` 也不会触发）——用户看到的是"为什么弹出一个正在校验（同步未覆盖全部
目录）— 0 / 0"。同时预算的语义要区分 `budget`（还有活）与 `dir_cap`（只是没走完）。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_shell_freshness_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'shell_freshness_card.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过共享组件用例')
class ShellFreshnessCardJsTest(unittest.TestCase):
    def test_card_flow(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'共享组件卡片用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
