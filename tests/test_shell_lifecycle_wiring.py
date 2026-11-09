"""壳前端（App.vue）把三种生命周期通知接进消息通道的构建级回归。

判定逻辑（可见性状态机、挂载计划）由 ``tests/js/plugin_lifecycle.mjs`` 与
``tests/js/plugin_frames.mjs`` 跑真实模块覆盖 —— 这里补的是**接线**：
App.vue 是 SFC，Node 与 Python 都不容易直接驱动，但它必须

- 把状态机的输出真正 postMessage 成 cubby:plugin-shown / -hidden / -dispose，
  漏掉任何一个，插件就收不到 onShow / onHide / onDispose 三个钩子中对应的那个；
- 把窗口级事件（visibilitychange / beforeunload / pagehide）真的挂上；
- 用 core/plugin-frames 的判定分组与拼接 iframe src，而不是在 SFC 里再写一份。

这些都是"在哪里调用"的事实，只能在源码/产物层面校验；能抽成纯函数的判定已经不在这里
（原先"常驻组极性"的 assertIn 已换成 ``tests/js/plugin_frames.mjs`` 的行为断言）。

做法是在 vite 构建产物里核对这三个消息类型（CI 的 frontend job 先跑
`npm run build`，随后 test job 才跑 unittest）。产物不存在时跳过，
避免把"没构建"误报成失败。
"""

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP = PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'App.vue'
DIST_ASSETS = PROJECT_ROOT / 'shell' / 'frontend' / 'dist' / 'assets'

REQUIRED_MESSAGE_TYPES = (
    'cubby:plugin-shown',
    'cubby:plugin-hidden',
    'cubby:plugin-dispose',
)


def _bundle_text() -> str | None:
    """读取构建后的壳 JS 包文本；未构建时返回 None。"""
    if not DIST_ASSETS.is_dir():
        return None
    chunks = [p for p in DIST_ASSETS.glob('*.js') if p.is_file()]
    if not chunks:
        return None
    return '\n'.join(p.read_text(encoding='utf-8', errors='replace') for p in chunks)


class ShellLifecycleWiringTests(unittest.TestCase):
    def test_built_bundle_emits_all_three_lifecycle_messages(self):
        text = _bundle_text()
        if text is None:
            self.skipTest('未找到 shell/frontend/dist 产物（先跑 npm --prefix shell/frontend run build）')
        missing = [message for message in REQUIRED_MESSAGE_TYPES if message not in text]
        self.assertEqual(missing, [], f'构建产物里缺少这些生命周期消息类型: {missing}')

    def test_app_source_wires_state_machine_and_lifecycle_events(self):
        """源码级兜底：产物未构建时也要保证接线没被删掉。"""
        app = APP.read_text(encoding='utf-8')
        for needle in (
            "from './core/plugin-visibility'",
            'postMessage({ type: message }',
            "addEventListener('visibilitychange'",
            "addEventListener('beforeunload'",
            "addEventListener('pagehide'",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, app)

    def test_app_delegates_frame_plan_to_the_shared_module(self):
        """分组极性、iframe src、离开时 dispose 谁、重载后补什么通知都由共享模块判定。

        这四条判定的行为在 ``tests/js/plugin_frames.mjs`` 里逐条断言（含"常驻组极性写反"
        与"两组交叠"两种退化）；这里只守接线：App.vue 用的确实是那份实现。
        """
        app = APP.read_text(encoding='utf-8')
        self.assertIn("from './core/plugin-frames'", app)
        for call in ('splitByKeepAlive(', 'pluginFrameSrc(', 'leavingFrames(', 'reloadedFrameMessages('):
            with self.subTest(call=call):
                self.assertIn(call, app, f'App.vue 没有调用 {call}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
