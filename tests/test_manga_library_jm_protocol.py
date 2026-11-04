"""禁漫接口客户端的用例（纯计算，不联网）。

钉住的是"改了就 403 / 解不开响应"的那些细节：请求头签名的两个值、响应体 AES-ECB
的密钥推导、漫画章节的去重（站点同一序号会列出多条）、章节目录名的措辞，
以及图片 URL 的拼接位置。

协议细节来自 jmcomic 2.6.19（MIT，`Copyright (c) 2023 hect0x7`），许可与出处见
仓库根 `THIRD_PARTY_NOTICES.md`。这类断言的基准值都写成可复核的算式，不写"上次
跑出来的数字"。
"""

from __future__ import annotations

import base64
import hashlib
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.plugin_utils import load_sibling

PLUGIN_BACKEND = PROJECT_ROOT / 'plugins' / 'manga-library' / 'backend'
#: 禁漫客户端是 `backend/libs` 下的 vendored 模块（与 pixiv-sync 的 pixiv_mini 同形态）。
protocol = load_sibling(str(PLUGIN_BACKEND / 'libs' / 'jm_mini.py'), 'jm_mini',
                        'manga_library_jm')


def _md5(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


class CryptoContractTests(unittest.TestCase):

    def test_request_signature_matches_documented_formula(self):
        client = protocol.JmClient()
        headers = client._token_headers(1700000000, '2.1.12', protocol.TOKEN_SECRET)
        self.assertEqual(headers['token'], _md5('1700000000' + protocol.TOKEN_SECRET))
        self.assertEqual(headers['tokenparam'], '1700000000,2.1.12')
        self.assertIn('user-agent', headers)

    def test_content_secret_produces_a_different_token(self):
        """章节模板接口必须换第二个密钥，否则站点返回 403。"""
        client = protocol.JmClient()
        first = client._token_headers(1700000000, '2.1.12', protocol.TOKEN_SECRET)['token']
        second = client._token_headers(1700000000, '2.1.12',
                                       protocol.TOKEN_SECRET_CONTENT)['token']
        self.assertNotEqual(first, second)

    def test_decrypt_reverses_aes_ecb_with_ts_key(self):
        from Crypto.Cipher import AES

        payload = {'name': '测试漫画', 'series': [{'id': '1', 'sort': '1'}]}
        raw = protocol.json.dumps(payload, ensure_ascii=False).encode('utf-8')
        pad = 16 - len(raw) % 16
        key = _md5('1700000000' + protocol.DATA_SECRET).encode('utf-8')
        encrypted = AES.new(key, AES.MODE_ECB).encrypt(raw + bytes([pad]) * pad)
        encoded = base64.b64encode(encrypted).decode('ascii')

        plain = protocol._decrypt(encoded, '1700000000', protocol.DATA_SECRET)
        self.assertEqual(protocol.json.loads(plain), payload)


class AlbumParsingTests(unittest.TestCase):

    @staticmethod
    def _series(count: int, duplicates: int) -> list:
        """构造 `count` 个章节，其中前 `duplicates` 个各自重复一条（同 sort 不同 id）。"""
        series = []
        for index in range(1, count + 1):
            series.append({'id': str(1000 + index), 'name': f'第{index}话', 'sort': str(index)})
            if index <= duplicates:
                series.append({'id': str(2000 + index), 'name': f'第{index}话(重复)',
                               'sort': str(index)})
        return series

    def test_duplicate_sorts_are_collapsed(self):
        series = self._series(19, duplicates=16)
        self.assertEqual(len(series), 35, '前置条件：站点把这本子的章节列成了 35 条')
        refs = protocol.JmClient._distinct_chapters(series)
        self.assertEqual(len(refs), 19, '同一序号只保留一条，实测 35 条 → 19 章')
        self.assertEqual([ref.sort for ref in refs], list(range(1, 20)), '去重后必须按序号排好')

    def test_first_entry_of_each_sort_wins(self):
        series = [
            {'id': '2', 'name': '后写的', 'sort': '3'},
            {'id': '1', 'name': '先写的', 'sort': '3'},
        ]
        refs = protocol.JmClient._distinct_chapters(series)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].photo_id, '2', '排序稳定，同序号以列表里靠前的为准')

    def test_author_and_series_id_normalisation(self):
        self.assertEqual(protocol.JmClient._author(['a', ' b ', '']), 'a b')
        self.assertEqual(protocol.JmClient._author('单人'), '单人')
        self.assertEqual(protocol.JmClient._author(None), '')
        self.assertEqual(protocol.JmClient._series_id('400222'), 400222)
        self.assertEqual(protocol.JmClient._series_id(None), 0)
        self.assertEqual(protocol.JmClient._series_id('x'), 0)


class ChapterTests(unittest.TestCase):

    def _chapter(self, series_id: int, sort: int) -> object:
        return protocol.Chapter(photo_id='1043134', name='昭和式女僕闲话抄',
                                sort=sort, series_id=series_id, images=['00001.webp'])

    def test_single_album_index_normalised_to_one(self):
        """单章本子（series_id == 0）站点给的 sort 是 2，目录名要用第 1 話。"""
        chapter = self._chapter(series_id=0, sort=2)
        self.assertTrue(chapter.is_single_album)
        self.assertEqual(chapter.album_index, 1)
        self.assertEqual(chapter.indextitle, '第1話 昭和式女僕闲话抄')

    def test_multi_album_keeps_site_sort(self):
        chapter = self._chapter(series_id=400222, sort=7)
        self.assertFalse(chapter.is_single_album)
        self.assertEqual(chapter.album_index, 7)
        self.assertEqual(chapter.indextitle, '第7話 昭和式女僕闲话抄')

    def test_len_is_image_count(self):
        chapter = self._chapter(series_id=0, sort=2)
        chapter.images = ['00001.webp', '00002.webp']
        self.assertEqual(len(chapter), 2)


class UrlTests(unittest.TestCase):

    def test_image_url_uses_chapter_id_and_five_digit_index(self):
        client = protocol.JmClient()
        url = client.image_url('1043134', '00027.webp')
        self.assertIn('/media/photos/1043134/00027.webp', url)
        self.assertTrue(any(domain in url for domain in protocol.IMAGE_DOMAINS))

    def test_image_url_keeps_original_suffix(self):
        client = protocol.JmClient()
        self.assertTrue(client.image_url('9', '00003.gif').endswith('/00003.gif'))

    def test_cover_url_points_at_album_cover(self):
        client = protocol.JmClient()
        self.assertIn('/media/albums/1043134.jpg', client.cover_url('1043134'))


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
