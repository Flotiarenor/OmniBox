"""灯箱解码位图内存缓存的无头回归（`tests/js/shell_lightbox_cache.mjs`）。

锁住的行为：`createLightbox().setDecodedLimit(n)` 决定灯箱保留多少张**解码后的位图**
（靠一小池离屏 `<img>` 把位图留在渲染进程里），0 表示关闭，关灯箱即清空。

为什么需要它：客户端持有文件字节 ≠ 显示时不卡。实测同一张 10 MB / 数千像素的原图，
重复打开会出现两种表现 —— 有时整张瞬出，有时从上到下一行行刷。后者是渲染进程把
解码位图丢掉了，重新解码 + 渐进绘制。请求侧（HTTP 缓存、系统页缓存）无法解释这个
差异，所以判据只能放在位图生命周期上。

用例直接执行壳的真实实现（shell/frontend/public/shell/base.js），用最小 DOM 桩。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'shell_lightbox_cache.mjs'


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过灯箱缓存用例')
class ShellLightboxCacheJsTest(unittest.TestCase):
    def test_decoded_bitmap_pool(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'{HARNESS.name} 失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')
        self.assertIn('ALL PASS', proc.stdout)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
