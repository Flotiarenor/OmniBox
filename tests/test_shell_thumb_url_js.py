"""Shell 缩略图 URL 编码的回归用例（tests/js/shell_thumb_url.mjs）。

缺陷背景：`Bridge.thumbUrl()`（shell/frontend/public/shell/base.js）原先直接
`/thumbs/${path}?plugin=…` 拼接相对路径。画师目录名带 `%`（如 `pixiv/29%/`）时
前端发出 `/thumbs/pixiv/29%/135504321.png`，nginx 视为非法百分号转义，在反代层
直接 400 —— 缩略图永远加载不出来，删缓存 / 全量重建都无效（请求没到应用）；
而原图走 `originalUrl()` 有 `encodeURIComponent`，所以点进灯箱反而正常。

base.js 是浏览器脚本，Python 侧无法直接驱动，这里用 Node + stub `window` 跑真实
文件。环境无 node 时跳过（前端构建本身依赖 node）。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'shell_thumb_url.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 shell URL 编码用例')
class ShellThumbUrlJsTest(unittest.TestCase):
    def test_thumb_url_is_encoded(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'shell thumbUrl 编码用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
