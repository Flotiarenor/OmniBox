"""media-player 前端脚本「装载契约」的回归（tests/js/media_player_app_split.mjs）。

缺陷背景：插件前端没有构建步骤，`index.html` 的 `<script src>` 顺序是唯一的依赖声明，
而 `tools/check_plugins.py::_check_frontend_assets` 只校验"文件存在且不越界"——既不校验
顺序，也不校验磁盘上的脚本是否真的被引用。于是下面三类改动在门禁里完全看不见，只在运行
时表现成 `X is not defined` 或某个方法凭空消失：

  1. 新增的分片没加进 `index.html`（分片里的方法全部不存在）；
  2. 分片顺序错（装载期 ReferenceError）；
  3. 方法搬走后原文件里的副本没删（静默覆盖，两个实现同时存在）。

harness 按 `index.html` 的声明顺序在 vm 里装载全部本地脚本并逐条断言，因此它同时是
"把 1993 行的 `media-player/frontend/js/app.js` 拆成多个分片"这一步的护栏：拆分前后都
应通过，且**新增分片只要出现在 `index.html` 里就会被自动纳入检查**，不需要改 harness。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_media_player_scripts_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'media_player_app_split.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过前端脚本装载契约用例')
class MediaPlayerScriptLoadContractTest(unittest.TestCase):
    def test_scripts_load_in_declared_order_and_keep_method_contract(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'前端脚本装载契约失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
