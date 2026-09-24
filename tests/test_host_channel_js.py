"""内嵌插件向上层申请 UI 的协议（HostChannel）回归（tests/js/host_channel.mjs）。

背景：内嵌 iframe 是独立文档，`.modal { position: fixed }` 只相对它自己那个 iframe ——
弹窗既盖不住宿主侧栏，也没有整页遮罩。修法不是让子插件自绘第二套样式，而是让它把
"我要一个设置弹窗"这件事 postMessage 给上层，由上层用自己的文档渲染、再把值交回子页
由它自己落盘（见 shell/frontend/public/shell/base.js 的 HostChannel 与
docs/plugin-ui-guide.md §3.4）。

base.js 是注入到插件 iframe 的前端脚本，Python 侧无法直接驱动，这里用 Node + vm stub
跑真实脚本。环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESSES = [
    PROJECT_ROOT / 'tests' / 'js' / 'host_channel.mjs',        # 子插件侧（真实插件页 + stub 宿主）
    PROJECT_ROOT / 'tests' / 'js' / 'host_channel_host.mjs',   # 宿主侧（stub DOM）
]


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 HostChannel 前端用例')
class HostChannelJsTest(unittest.TestCase):
    def test_child_side_request_and_callback_protocol(self):
        self._run(HARNESSES[0])

    def test_host_side_toolbar_mount_and_settings_multiround(self):
        self._run(HARNESSES[1])

    def _run(self, harness):
        proc = subprocess.run(
            ['node', str(harness)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'{harness.name} 用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
