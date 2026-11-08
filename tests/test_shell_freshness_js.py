"""Shell 共享组件「同步 / 校验」的前端用例（`tests/js/shell_freshness*.mjs`）。

背景：被动同步报 `partial` 时，组件原先只弹卡片、**没有真的起校验**，卡片停在
静态占位符「0 / 0」上永不消失（`renderCard` 在"没有任务"时直接 return，
`onSettled` 也不会触发）——用户看到的是"为什么弹出一个正在校验（同步未覆盖全部
目录）— 0 / 0"。同时预算的语义要区分 `budget`（还有活）与 `dir_cap`（只是没走完）。

三个 harness 分别守：
  - `shell_freshness_card.mjs`：进度卡片的动作编排（自动同步 / 启动校验 / 取消），
    同步通道本身（`Freshness.sync` 打到 `system_freshness_sync`、挂载句柄的 `sync`
    与 `autoSync` 同源、`onChange` 收到的是报告），以及被跳过（`busy` / `throttled` /
    `bad_scope`）时状态行给出的原因与停留期低频状态重读；
  - `shell_freshness.mjs`：状态文案与轮询生命周期（含"同一实例第二次校验仍会轮询"），
    以及壳挂载型触发面的输入通道 `omnibox:chrome.freshness_scope` 的归一化
    （strip、空串合法、超长与含 `..` 的值丢弃）；
  - `shell_freshness_notify.mjs`：刷新通知面 —— `buildFreshnessEvent` 的报告透传、
    壳同时发 `omnibox:freshness` 与旧的 `omnibox:action: refresh`、
    `/shell/chrome-client.js` 把 action 的 `payload` 交给 `onAction` 第二参
    （富事件走 `onMessage`），以及设置页「数据与缓存」的概览 / 重建接线。
后者曾漏接线：文件存在、内容正确，但没有任何 `test_*_js.py` 引用，等于从未执行。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_shell_freshness_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESSES = [
    PROJECT_ROOT / 'tests' / 'js' / 'shell_freshness_card.mjs',
    PROJECT_ROOT / 'tests' / 'js' / 'shell_freshness.mjs',
    PROJECT_ROOT / 'tests' / 'js' / 'shell_freshness_notify.mjs',
]


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过共享组件用例')
class ShellFreshnessCardJsTest(unittest.TestCase):
    def test_card_flow(self):
        self._run(HARNESSES[0])

    def test_state_text_and_polling_lifecycle(self):
        self._run(HARNESSES[1])

    def test_rich_event_and_settings_overview(self):
        self._run(HARNESSES[2])

    def _run(self, harness):
        proc = subprocess.run(
            ['node', str(harness)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'共享组件用例失败（{harness.name}，exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
