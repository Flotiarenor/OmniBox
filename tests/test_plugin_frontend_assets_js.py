"""插件前端「资源装载契约」的全量入口（tests/js/plugin_asset_contract.mjs）。

缺陷背景：插件前端没有构建步骤，`index.html` 的 `<script src>` 顺序是唯一的依赖声明。
此前只有 image-viewer / media-player 两个插件有专门的装载契约用例（各 1500 行的
app.js 拆分时才加的），novel-reader（4 个脚本）、manga-library（3 个）、
image-cleaner / netease-music（各 1 个）完全没有门禁：新分片忘了挂进 index.html、
顺序写错、或脚本搬走后副本没删，都只会在运行时表现成 `X is not defined`。

本用例跑的是不依赖入口类名的资源契约（声明与磁盘一致 + 按顺序装载），因此对
"零本地脚本、由服务端注入 /shell/*.js"的 pixiv-sync 前端同样适用。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_plugin_frontend_assets_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'plugin_asset_contract.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过插件前端资源契约用例')
class PluginFrontendAssetContractTest(unittest.TestCase):
    def test_all_plugin_frontends_keep_asset_contract(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=180,
        )
        if proc.returncode != 0:
            self.fail(f'插件前端资源契约失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
