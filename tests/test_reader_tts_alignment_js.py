"""朗读音色/音频与句子对齐的无头回归（tests/js/reader_tts_alignment.mjs）。

缺陷背景：2026-10-04 07:27~07:28 的日志显示，读第三段时播放了第二段的音频，
同一句音频被请求两次。机制是预取结果**按序号**存：`_prefetchNext()` 的回调跑到时
`this.index` 已经前进，第 N 句的音频被塞进第 N+1 格；`_playCurrent()` 又只按序号取用，
从不核对"这是不是这一句的"。慢端点单句合成实测 7 秒，这段窗口足够让序号跑掉。

修复后由本用例守住：预取结果按内容指纹存储与取用、指纹不符即丢弃重取、
迟到的 `ended` 不推进播放位置。环境无 node 时跳过。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'reader_tts_alignment.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过朗读音频对齐用例')
class ReaderTtsAlignmentJsTest(unittest.TestCase):
    def test_prefetch_and_ended_are_bound_to_the_piece(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'朗读音频对齐用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
