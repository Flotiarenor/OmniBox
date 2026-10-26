"""manga-library 书架/搜索迁移的无头回归（tests/js/manga_library_shelf.mjs）。

迁移把书架卡片换成壳的共享内容原语 `ContentUI.card`（结构、键盘激活、操作槽位），
这里锁住迁移后仍须成立的行为：卡片可点击/回车打开详情、收藏按钮不误触卡片、
封面失败走插件自己的降级、搜索无结果与空库是两套文案。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'manga_library_shelf.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过 manga-library 书架用例')
class MangaLibraryShelfJsTest(unittest.TestCase):
    def test_shelf_cards_and_search_empty_state(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'manga-library 书架用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
