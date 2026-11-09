"""media-player「仅声音 / 画面」偏好持久化失败路径（tests/js/media_player_stage_persist.mjs）。

缺陷背景（与 A1/A2 同类的"报成功但没写"）：`_toggleVideoMode()` 末尾原先是一句
`Bridge.call('media_set_config', …).catch(() => {})`。切换本身立即生效，但偏好没写进
设置时界面什么都不说 —— 用户重启后回到旧值，只会觉得"这个设置不生效"。

后端 `media_set_config` 把 `update_setting` 的布尔结果包成 `{'success': bool}` 返回，
HTTP 仍是 200，因此**两条失败路径都要覆盖**：Promise 被拒，以及 resolve 但
`success: false`。本用例装载线上 `app-stage.js` + 壳注入的唯一 Toast 实现（含同文本
去重），用可控 Bridge 替身断言：失败时有面向用户的提示、切换不回退、成功时无提示。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_media_player_stage_persist_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'media_player_stage_persist.mjs'
STAGE_JS = PROJECT_ROOT / 'plugins' / 'media-player' / 'frontend' / 'js' / 'app-stage.js'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 media-player 偏好持久化用例')
class MediaPlayerStagePersistTest(unittest.TestCase):
    def test_harness_reads_real_sources(self):
        """用例引用的舞台分片必须真的存在，且偏好写入仍走独立入口（改名会在这里红）。"""
        self.assertTrue(HARNESS.is_file(), f'缺少 {HARNESS}')
        source = STAGE_JS.read_text(encoding='utf-8')
        self.assertIn('_persistVideoMode', source,
                      '偏好写入应当有独立入口，两条失败路径才能在同一个地方覆盖')
        self.assertIn('Toast.error', source, '保存失败必须给用户可见的提示')

    def test_failure_and_success_paths(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'media-player 偏好持久化用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
