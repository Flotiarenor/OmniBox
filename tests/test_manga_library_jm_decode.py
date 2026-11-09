"""禁漫图片乱序解码的用例（纯计算，不联网）。

这些断言钉住的是**协议常量与条带数学**：分割阈值、摘要取档、余数补在第一条上。
改任何一处都会让下载出来的图错位，而错位不会报错 —— 只能靠数值对照发现。

分割数基准值来自与 jmcomic 2.6.19 的实测对照：同一本子（id 1043134）、同一张图
（`00001.webp`）双方都算出 4，解码结果逐像素一致（见仓库根 `THIRD_PARTY_NOTICES.md`
里的移植说明）。
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.plugin_utils import load_sibling

PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'manga-library' / 'backend'
#: 禁漫客户端是 `backend/libs` 下的 vendored 模块（与 pixiv-sync 的 pixiv_mini 同形态），
#: 不是插件后端组件；按文件路径加载，避免依赖测试进程的 sys.path 状态。
decode = load_sibling(str(PLUGIN_BACKEND / 'libs' / 'jm_mini.py'), 'jm_mini',
                      'manga_library_jm')


def _tiny_image(rows: int, width: int = 8) -> object:
    """生成一张每行颜色不同的竖条测试图（按行号着色，便于判断重排是否生效）。"""
    from PIL import Image

    image = Image.new('RGB', (width, rows))
    for y in range(rows):
        image.paste((y % 256, 0, 0), (0, y, width, y + 1))
    return image


def _scramble(image: object, num: int) -> object:
    """按站点的方式把条带打乱：`decode` 的逆操作。

    `decode` 做的是「目标条带 ← 源条带」，逆操作就是反过来「源条带 ← 目标条带」。
    注意余数只加在第一条上，条带尺寸因此不对称 —— 把 decode 直接套两遍不是恒等。
    """
    from PIL import Image

    width, height = image.size
    out = Image.new('RGB', (width, height))
    over = height % num
    for index in range(num):
        move = height // num
        y_src = height - move * (index + 1) - over
        y_dst = move * index
        if index == 0:
            move += over
        else:
            y_dst += over
        out.paste(image.crop((0, y_dst, width, y_dst + move)), (0, y_src, width, y_src + move))
    return out


class SegmentationNumTests(unittest.TestCase):

    def test_below_scramble_threshold_is_untouched(self):
        self.assertEqual(decode.segmentation_num(220980, 220979, '00001'), 0)

    def test_fixed_ten_strips_between_thresholds(self):
        self.assertEqual(decode.segmentation_num(220980, 220980, '00001'), 10)
        self.assertEqual(decode.segmentation_num(220980, 268849, '00001'), 10)

    def test_hashed_buckets_after_threshold(self):
        # 与 jmcomic 实测一致：章节 1043134、图片 00001 → 4 条
        self.assertEqual(decode.segmentation_num(220980, 1043134, '00001'), 4)

    def test_bucket_count_drops_to_eight_after_421926(self):
        """421926 之后摘要只取 8 档，取模基数变了，条数必须跟着变。"""
        import hashlib

        aid = 421926
        digest = hashlib.md5(f'{aid}00001'.encode()).hexdigest()
        expected = ord(digest[-1]) % 8 * 2 + 2
        self.assertEqual(decode.segmentation_num(220980, aid, '00001'), expected)
        # 阈值前同一张图按 10 档算，两者通常不同（同余才相等，这里只钉基数口径）
        before = decode.segmentation_num(220980, aid - 1, '00001')
        digest_before = hashlib.md5(f'{aid - 1}00001'.encode()).hexdigest()
        self.assertEqual(before, ord(digest_before[-1]) % 10 * 2 + 2)

    def test_result_is_deterministic_and_name_sensitive(self):
        first = decode.segmentation_num(220980, 1043134, '00001')
        again = decode.segmentation_num(220980, 1043134, '00001')
        other = decode.segmentation_num(220980, 1043134, '00002')
        self.assertEqual(first, again, '同一个输入必须算出同一个分割数')
        self.assertNotEqual(first, other, '图片名参与摘要，不同图应能得到不同条数')

    def test_segmentation_is_even_and_bounded(self):
        for aid in (268850, 300000, 421925, 421926, 900000, 1043134):
            num = decode.segmentation_num(220980, aid, '00003')
            self.assertGreaterEqual(num, 2)
            self.assertLessEqual(num, 20)
            self.assertEqual(num % 2, 0, '条数必须是偶数（成对上下拼回）')


class DecodeMathTests(unittest.TestCase):

    def test_zero_segments_returns_input(self):
        image = _tiny_image(61)
        self.assertIs(decode.decode(image, 0), image)

    def test_decode_restores_scrambled_image(self):
        for num in (2, 4, 10, 14):
            with self.subTest(num=num):
                original = _tiny_image(120, width=4)
                scrambled = _scramble(original, num)
                restored = decode.decode(scrambled, num)
                self.assertEqual(restored.size, original.size)
                self.assertIsNone(_diff_bbox(restored, original),
                                  f'{num} 条时重排结果与原始图不一致')

    def test_remainder_goes_to_first_strip(self):
        """行数不能被条数整除时，余数补在第一条：用 121 行（121 % 4 == 1）验证。"""
        original = _tiny_image(121, width=4)
        restored = decode.decode(_scramble(original, 4), 4)
        self.assertIsNone(_diff_bbox(restored, original))


class SaveImageTests(unittest.TestCase):

    def test_jpeg_saved_after_decode(self):
        import tempfile

        from PIL import Image

        num = decode.segmentation_num(220980, 1043134, '00001')
        original = _tiny_image(120, width=4)
        scrambled = _scramble(original, num)
        buffer = io.BytesIO()
        scrambled.save(buffer, format='PNG')

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / '00001.jpg'
            decode.save_image(buffer.getvalue(), target, scramble_id=220980,
                              photo_id=1043134, source_name='00001.webp')
            self.assertTrue(target.is_file())
            with Image.open(target) as saved:
                self.assertEqual(saved.format, 'JPEG')
                self.assertEqual(saved.size, original.size)

    def test_gif_is_written_verbatim(self):
        """动图没有单帧可重排：必须原样写出，按 JPEG 重排会把动画切坏。"""
        import tempfile

        from PIL import Image

        frames = [Image.new('RGB', (4, 4), (index * 20, 0, 0)) for index in range(3)]
        buffer = io.BytesIO()
        frames[0].save(buffer, format='GIF', save_all=True, append_images=frames[1:], duration=50)
        raw = buffer.getvalue()

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / '00001.jpg'
            decode.save_image(raw, target, scramble_id=220980,
                              photo_id=1043134, source_name='00001.gif')
            self.assertEqual(target.read_bytes(), raw, '动图被改写过了')


def _diff_bbox(left: object, right: object):
    from PIL import ImageChops

    return ImageChops.difference(left.convert('RGB'), right.convert('RGB')).getbbox()


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
