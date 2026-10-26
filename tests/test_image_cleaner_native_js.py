"""image-cleaner 原生面板（`tests/js/image_cleaner_native.mjs`）的驱动。

本插件的文件级分页（提交 253f783）没有任何 Python 侧用例：分组结果由后端给、选中
集合与删除请求由 native.js 在 iframe 里算，二者之间正是"以为全选实际只删了前 12 张"
的数据风险带。node 夹具装载真实 `/shell/native-client.js` 与 `native.js`，锁住：
每组首屏 12 张、每次展开 24 张、组内全选/只留一张作用于全部文件、模式切换 / 重读 /
删除后重置、组级「显示更多」保留。

无 node 时跳过（前端构建本身依赖 node，CI 上不会真的缺）。

运行：
    python -m unittest tests.test_image_cleaner_native_js -v
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'image_cleaner_native.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 image-cleaner 原生面板用例')
class ImageCleanerNativeJsTest(unittest.TestCase):
    def test_pagination_and_selection_scope(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'image-cleaner 原生面板用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
