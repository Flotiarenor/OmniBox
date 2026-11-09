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

另外这里守**双端一致性**：`freshness_scope` 由壳归一化后交给引擎，两边对同一个
字符串必须解析成同一个目录（去首尾 `/`、折叠 `//`、丢掉 `.` 段），而前端判非法的
形状（`..` 段、反斜杠、盘符、UNC、超长）不许被后端解释成另一个位置。用例驱动
`shell_freshness.mjs --scope-cases` 拿到真实归一化结果，再与引擎的
`FsSource.scope_start` 逐条比对 —— 两边各维护一份输入表迟早会漂。

运行：
    python -m unittest tests.test_shell_freshness_js -v
"""

import json
import os
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
SCOPE_HARNESS = HARNESSES[1]


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


def _scope_rejected(raw: object, limit: int) -> bool:
    """契约为"前端判非法"的形状（与实现在**同一份文档规则**上独立判一遍）。"""
    if not isinstance(raw, str):
        return True
    text = raw.strip()
    if not text:
        return False
    if len(text) > limit or '\\' in text or text.startswith('//'):
        return True
    if len(text) >= 2 and text[1] == ':' and text[0].isalpha():
        return True
    return '..' in [part for part in text.split('/') if part not in ('', '.')]


def _norm_rel(value: str) -> str:
    """目录相对路径的平台无关规范化（`a//b` / `a/./b` 与 `a/b` 视为同一目录）。"""
    return os.path.normpath(value).replace('\\', '/')


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过作用域一致性用例')
class FreshnessScopeContractTest(unittest.TestCase):
    """`normalizeFreshnessScope` 与引擎 `FsSource.scope_start` 必须解释成同一个目录。"""

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            ['node', str(SCOPE_HARNESS), '--scope-cases'],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(f'作用域用例导出失败（exit={proc.returncode}）\n'
                                 f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        cls.limit = payload['max']
        cls.cases = payload['cases']

    def test_cases_are_not_empty(self):
        self.assertGreater(len(self.cases), 20, '作用域用例表被清空，这条会变成空转')

    def test_invalid_shapes_are_dropped(self):
        from shell.backend.freshness import FsSource

        source = FsSource(roots=(PROJECT_ROOT / 'tests',))
        for case in self.cases:
            raw, got = case['raw'], case['normalized']
            with self.subTest(raw=raw):
                if _scope_rejected(raw, self.limit):
                    self.assertIsNone(got, f'{raw!r} 必须被前端判非法')
                    continue
                self.assertIsInstance(got, str, f'{raw!r} 不该被丢弃')
                # 归一化结果必须自洽（不再含首尾 `/`、重复 `/`、`.` 段）
                self.assertEqual(got, got.strip().strip('/'))
                self.assertNotIn('//', got)
                self.assertNotIn('/./', f'/{got}/')
                # 与引擎对原始值的解释落到同一个目录
                want = source.scope_start(raw)
                have = source.scope_start(got)
                if want is None or have is None:
                    self.fail(f'{raw!r} → {got!r} 有一侧引擎解析失败'
                              f'（raw={want!r}, normalized={have!r}）')
                self.assertEqual(want[0], have[0], f'{raw!r} 的根序号不一致')
                self.assertEqual(_norm_rel(want[1]), _norm_rel(have[1]),
                                 f'{raw!r} 与归一化结果 {got!r} 落到不同目录')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
