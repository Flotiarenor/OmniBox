"""media-player manifest 的壳骨架声明清理。

`frontend.chrome.content`（`canvas` / `native` / `hybrid`）与 nav 组的 `dynamic`
从来没有运行时消费方：内容归属由壳按 URL 决定，动态歌单组由内容页通过
`omnibox:chrome` 的 `nav` 载荷整组替换。两个字段保留在 manifest 里只会让人以为
壳会读它们（`tools/check_plugins.py` 的认可清单也将在后续任务删除）。

本用例锁住决定"删除，不实现"之后的最终状态，并顺带防止误删仍在使用的骨架字段：
  1. chrome 不再声明 `content`；
  2. 没有 nav 组再声明 `dynamic`；
  3. brand / nav / toolbar / search / status / freshness / extensions / sidebarOrder
     仍然完整（删死字段时不要连壳真正读取的声明一起删）；
  4. sidebarOrder 里的 `nav:<组 id>` / `extensions` 都能对应到已声明的组。

运行：
    python -m unittest tests.test_media_player_manifest -v
"""

import json
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PROJECT_ROOT / 'plugins' / 'media-player' / 'manifest.json'


class MediaPlayerManifestChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
        cls.chrome = cls.manifest['frontend']['chrome']

    def test_no_dead_content_declaration(self):
        self.assertNotIn(
            'content', self.chrome,
            'frontend.chrome.content 没有运行时消费方（决定删除，不实现）',
        )

    def test_no_dynamic_nav_group(self):
        dynamic = [group.get('id') for group in self.chrome.get('nav') or [] if 'dynamic' in group]
        self.assertEqual(
            dynamic, [],
            'nav[].dynamic 没有运行时消费方；动态歌单组由内容页 omnibox:chrome 的 nav 载荷替换',
        )

    def test_live_chrome_declarations_stay(self):
        for key in ('brand', 'nav', 'toolbar', 'search', 'status', 'freshness',
                    'extensions', 'sidebarOrder'):
            self.assertIn(key, self.chrome, f'壳会读取的 chrome.{key} 不能一并删掉')
        self.assertEqual(self.chrome['status'].get('kind'), 'eq-text')
        self.assertEqual(self.chrome['freshness'].get('unit'), '首')

    def test_sidebar_order_references_declared_groups(self):
        group_ids = {group.get('id') for group in self.chrome.get('nav') or []}
        for entry in self.chrome['sidebarOrder']:
            if entry == 'extensions':
                self.assertIn('extensions', self.chrome)
                continue
            self.assertTrue(entry.startswith('nav:'), entry)
            self.assertIn(entry[len('nav:'):], group_ids, f'sidebarOrder 指向不存在的组：{entry}')

    def test_nav_items_have_ids_and_labels(self):
        items = [item for group in self.chrome.get('nav') or [] for item in group.get('items') or []]
        self.assertTrue(items)
        for item in items:
            self.assertTrue(item.get('id'), f'nav 项缺 id：{item}')
            self.assertTrue(item.get('label'), f'nav 项缺 label：{item}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
