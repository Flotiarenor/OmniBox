"""插件前端生命周期（onShow / onHide / onDispose）的无头回归（tests/js/plugin_lifecycle.mjs）。

缺陷背景：插件 iframe 默认常驻，切换后其定时器与
rAF 自循环继续运行，插件前端也没有统一的销毁钩子 —— 实测 plugins/**/*.js 合计
addEventListener 171 : removeEventListener 4。修复方向不是"离开即销毁"（readme.md
写明常驻是有意设计：切换时媒体播放不中断），而是宿主在可见性变化时通知插件。

base.js 是注入到插件 iframe 的前端脚本，Python 侧无法直接驱动，这里用 Node + vm stub
跑真实脚本。环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'plugin_lifecycle.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过插件生命周期前端用例')
class PluginLifecycleJsTest(unittest.TestCase):
    def test_hooks_and_visibility_state_machine(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'插件生命周期用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
