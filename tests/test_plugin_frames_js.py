"""壳插件 iframe 挂载计划的行为用例（tests/js/plugin_frames.mjs）。

原先是两条源码字符串断言（`assertIn('filter(p => p.keepAlive)')` 与
`assertIn('filter(p => !p.keepAlive)')`），改注释就红、把极性写反却可能绿。
判定逻辑现在在 `shell/frontend/src/core/plugin-frames.js`，本用例用 Node 直接
import 真实实现跑行为断言。

为什么实现是 `.js` 而不是 `.ts`：Node 直接跑 `.ts` 需要 `--experimental-strip-types`
（22.6+），CI 的前端 Node 版本低于该门槛，那样写会在 CI 上静默跳过 —— 即"看起来
有门禁、实际没跑"。纯 JS + 同名 `.d.ts` 是 `plugin-url.js` / `plugin-visibility.js`
已有的做法，Node 不需要任何开关。

运行：
    python -m unittest tests.test_plugin_frames_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'plugin_frames.mjs'
MODULE = PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'core' / 'plugin-frames.js'
APP = PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'App.vue'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过插件 iframe 挂载计划用例')
class PluginFramesPlanTest(unittest.TestCase):
    def test_harness_reads_real_sources(self):
        """用例引用的模块与接线处必须真的存在，否则"全过"可能是空转。"""
        for file in (HARNESS, MODULE, APP):
            self.assertTrue(file.is_file(), f'用例引用的文件不存在: {file}')

    def test_mount_plan_behaviour(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'插件 iframe 挂载计划用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
