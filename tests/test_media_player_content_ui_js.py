"""media-player 卡片 / 媒体行迁移到共享 ContentUI 之后的交互契约。

迁移提交把专辑卡与媒体行从手拼 innerHTML 换成壳注入的 `/shell/content-ui.js`；
`tests/js/media_player_content_ui.mjs` 用真实 content-ui.js + 真实 app-render.js 锁住
role=button / Tab / Enter / Space、行内动作不触发主激活、播放按钮、封面降级与视频
抽帧钩子。这些行为原先只有慢速浏览器 e2e 覆盖。

无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_media_player_content_ui_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'media_player_content_ui.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 media-player ContentUI 契约用例')
class MediaPlayerContentUiJsTest(unittest.TestCase):
    def test_shared_primitives_contract(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'media-player ContentUI 契约用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
