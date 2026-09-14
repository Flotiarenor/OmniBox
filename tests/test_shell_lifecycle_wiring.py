"""壳前端（App.vue）确实把三种生命周期通知接进消息通道的构建级回归。

判定逻辑（可见性状态机本身）由 tests/js/plugin_lifecycle.mjs 覆盖；这里补的是"接线"：
App.vue 是 SFC，Python/Node 都不容易直接驱动，但它必须把状态机的输出真正
postMessage 成 omnibox:plugin-shown / -hidden / -dispose —— 漏掉任何一个，
插件就收不到通知（onShow / onHide / onDispose 三个钩子各对应一个类型）。

做法是在 vite 构建产物里核对这三个消息类型（CI 的 frontend job 先跑
`npm run build`，随后 test job 才跑 unittest）。产物不存在时跳过，
避免把"没构建"误报成失败。
"""

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_ASSETS = PROJECT_ROOT / 'shell' / 'frontend' / 'dist' / 'assets'

REQUIRED_MESSAGE_TYPES = (
    'omnibox:plugin-shown',
    'omnibox:plugin-hidden',
    'omnibox:plugin-dispose',
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
        app = (PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'App.vue').read_text(encoding='utf-8')
        for needle in (
            "from './core/plugin-visibility'",
            'postMessage({ type: message }',
            "addEventListener('visibilitychange'",
            "addEventListener('beforeunload'",
            "addEventListener('pagehide'",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, app)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
