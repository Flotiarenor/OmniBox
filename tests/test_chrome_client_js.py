"""内容页 ↔ 壳的 chrome 握手客户端（tests/js/chrome_client.mjs）的驱动。

为什么需要：`omnibox:ready` / `omnibox:chrome` / `omnibox:view` / `omnibox:action` /
`omnibox:query` 这五条消息原先由四个内容页各写一份（document-reader / image-viewer /
manga-library / media-player），协议改一处就要同步四处，漏一处只表现为"该插件点了没反应"。
握手收敛到 `/shell/chrome-client.js` 之后，本用例跑真实实现断言两件在界面上看不出来的事：
来源校验（只认父窗口 + 同源）与"先监听、再 ready"的顺序；同时核对四份 app.js 确实都走
这一份实现。

环境无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_chrome_client_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'chrome_client.mjs'
CLIENT = PROJECT_ROOT / 'shell' / 'frontend' / 'public' / 'shell' / 'chrome-client.js'
PLUGIN_APPS = (
    'plugins/document-reader/frontend/js/app.js',
    'plugins/image-viewer/frontend/js/app.js',
    'plugins/manga-library/frontend/js/app.js',
    'plugins/media-player/frontend/js/app.js',
)


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 chrome 握手客户端用例')
class ChromeClientTest(unittest.TestCase):
    def test_harness_reads_real_sources(self):
        """用例引用的实现与四份 app.js 必须真的存在（改名后"全过"不能是空转）。"""
        for rel in (HARNESS, CLIENT, *(PROJECT_ROOT / p for p in PLUGIN_APPS)):
            self.assertTrue(rel.is_file(), f'用例引用的文件不存在: {rel}')

    def test_handshake_behaviour(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'chrome 握手用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
