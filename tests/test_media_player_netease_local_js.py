"""媒体播放器「网易云 → 本地」前端逻辑的无头验证（tests/js/media_player_netease_local.mjs）。

缺陷背景：「我的歌单」视图把空结果也当缓存命中（`if (cached && cached.results)`，而 `[]`
在 JS 里是真值）。第一次进入时若接口失败（典型是还没登录 —— 推荐歌单是公开接口，所以
其它视图照常有数据），`{results: []}` 就被写进 localStorage，之后即使登录成功也永远走
缓存分支，列表一直是空的，空状态文案还是「暂无推荐歌单」。

用例同时覆盖 utils.js 的匹配函数（歌名 + 歌手/时长判定、同名不串台、同一本地条目不被
重复认领）与两个入口的调用参数（幂等导入喜欢、同名镜像歌单覆盖）。前端脚本没有构建步骤，
Python 侧无法直接驱动，这里用 Node + vm 跑真实脚本；无 node 时跳过（前端构建本身依赖 node）。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'media_player_netease_local.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过前端网易云本地化用例')
class MediaPlayerNeteaseLocalJsTest(unittest.TestCase):
    def test_match_and_cache_guards(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'网易云本地化逻辑用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
