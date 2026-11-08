"""group-mesh 统一刷新与共享项用量展示的无头回归（tests/js/group_mesh_freshness.mjs）。

切回前台按全部共享根同步、报告有变化才重读后端状态、共享项卡片展示用量与条目数、
最近同步失败在节点卡片上可见且字段缺失时静默；`omnibox:freshness` 与旧的 `refresh`
动作两条通知路径都在用例里覆盖。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'group_mesh_freshness.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 group-mesh 刷新用例')
class GroupMeshFreshnessJsTest(unittest.TestCase):
    def test_freshness_sync_and_usage(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'group-mesh 刷新用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
