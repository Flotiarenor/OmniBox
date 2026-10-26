"""group-mesh 的页面骨架声明（manifest `frontend.chrome`）与内容页的一致性。

背景：group-mesh 在本区间从「自绘侧栏 / 顶栏」迁移到「壳按 manifest 渲染骨架」。
迁移后有一类静默失败：manifest 里声明了壳会渲染的导航项，但内容页 `index.html`
里没有对应的 `data-panel` 区段（点导航没反应），或内容页还留着旧 chrome 标记
（壳会再画一份，页面出现两个侧栏）。两类问题都没有运行期报错，只能静态对账。

本用例锁住：
  1. 每个 nav 项 id 都能在内容页找到 `data-panel` 相同的区段，且区段带标题/副标题
     （壳的标题来自内容页上报的 `data-title` / `data-sub`）；
  2. 内容页不再出现 chrome 标记（`.view-sub-sidebar`、`.view-toolbar`、
     `.sub-sidebar-brand`、`.obx-nav-item`）与侧栏分隔条；
  3. chrome 上报的 badges 键（`shares` / `remote`）都是真实存在的 nav 项 id ——
     壳按 nav 项 id 查 `ui.badges`，键对不上就是"徽标永远不出现"；
  4. 顶栏动作 id 只有两类：壳代管（`settings`）或内容页处理（`refresh`），
     不允许声明了却没人处理的 id；
  5. 本插件不声明 `search`，内容页也不处理 `omnibox:query`（声明了却不接会让
     搜索框出现但输入无效）。

运行：
    python -m unittest tests.test_group_mesh_chrome -v
"""

import json
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'group-mesh'
INDEX_HTML = PLUGIN_DIR / 'frontend' / 'index.html'
APP_JS = PLUGIN_DIR / 'frontend' / 'js' / 'app.js'
MANIFEST = PLUGIN_DIR / 'manifest.json'

FORBIDDEN_CHROME_MARKERS = (
    'view-sub-sidebar',
    'view-toolbar',
    'sub-sidebar-brand',
    'obx-nav-item',
    'data-obx-splitter',
)
# 壳代管的顶栏动作，内容页不需要处理。
SHELL_OWNED_ACTIONS = {'settings'}


class GroupMeshChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
        cls.html = INDEX_HTML.read_text(encoding='utf-8')
        cls.app_js = APP_JS.read_text(encoding='utf-8')
        cls.chrome = cls.manifest['frontend']['chrome']

    def _sections(self):
        sections = {}
        pattern = re.compile(
            r'<section\b[^>]*\bdata-panel="([^"]+)"[^>]*>(.*?)</section>', re.DOTALL)
        for match in pattern.finditer(self.html):
            sections[match.group(1)] = match.group(2)
        return sections

    def _nav_items(self):
        items = []
        for group in self.chrome.get('nav') or []:
            items.extend(group.get('items') or [])
        return items

    def test_every_nav_item_has_a_content_section(self):
        sections = self._sections()
        nav_ids = [item['id'] for item in self._nav_items()]
        self.assertTrue(nav_ids, 'manifest 必须声明 nav')
        self.assertEqual(len(nav_ids), len(set(nav_ids)), f'nav id 不能重复：{nav_ids}')
        for nav_id in nav_ids:
            self.assertIn(nav_id, sections, f'导航项 {nav_id} 没有对应的 data-panel 区段（点了会没反应）')
            # 标题/副标题在 section 开标签上，单独从 HTML 里取
            head = re.search(
                r'<section\b[^>]*\bdata-panel="' + re.escape(nav_id) + r'"[^>]*>', self.html)
            block = head.group(0) if head else ''
            self.assertRegex(block, r'data-title="[^"]+"', f'{nav_id} 区段缺 data-title')
            self.assertRegex(block, r'data-sub="[^"]*"', f'{nav_id} 区段缺 data-sub')

    def test_content_page_has_no_chrome_markers(self):
        for marker in FORBIDDEN_CHROME_MARKERS:
            self.assertNotIn(marker, self.html,
                             f'内容页出现壳骨架标记 {marker}：迁移后应只剩内容')

    def test_badge_keys_are_real_nav_item_ids(self):
        nav_ids = {item['id'] for item in self._nav_items()}
        # app.js 只上报名单/设备两处计数；壳按 nav 项 id 查 ui.badges
        reported = set(re.findall(r'badges\.([a-zA-Z_]+)\s*=', self.app_js))
        self.assertTrue(reported, '内容页应上报至少一个徽标')
        for key in reported:
            self.assertIn(key, nav_ids, f'徽标键 {key} 不是任何 nav 项的 id，壳不会显示它')

    def test_toolbar_actions_are_handled(self):
        handled = set(re.findall(r"data\.action === '([^']+)'", self.app_js))
        declared = [item['id'] for item in self.chrome.get('toolbar') or []]
        self.assertTrue(declared)
        self.assertEqual(len(declared), len(set(declared)), 'toolbar id 不能重复')
        for action in declared:
            if action in SHELL_OWNED_ACTIONS:
                continue
            self.assertIn(action, handled,
                          f'顶栏动作 {action} 没有任何内容页处理分支（点了没反应）')

    def test_no_search_contract_mismatch(self):
        self.assertNotIn('search', self.chrome,
                         'manifest 没有声明 search，壳才不渲染搜索框')
        self.assertNotIn('omnibox:query', self.app_js,
                         '内容页不处理 omnibox:query；manifest 也不应声明 search')

    def test_status_and_freshness_contract(self):
        self.assertIs(self.chrome.get('status'), True,
                      'status: true 是壳显示状态栏的条件')
        self.assertEqual((self.chrome.get('freshness') or {}).get('unit'), '项')
        self.assertNotIn('sidebarOrder', self.chrome,
                         '本插件没有扩展入口，不需要 sidebarOrder；声明了壳会按不存在的键排序')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
