"""壳的图标 sprite 与引用一致性。

为什么需要这组用例
------------------
图标迁移最容易出的问题**不是报错，而是静默画不出来**：

1. sprite 里的 `<symbol>` 是 shadow tree，**外部样式进不去** —— 漏写
   `stroke="currentColor"` 时图标仍是黑的，不跟随主题与用户自定义颜色；只写 viewBox
   不写宽高时 `<svg>` 按默认 300×150 撑开整行。这两类静态看 DOM 都"存在且正确"。
2. `res/icons/icon_data.json`（冻结的图标源）与生成物 `icons.svg` 可能不同步 ——
   有人改了源数据没重新生成，界面照旧是旧图形。
3. 模板里引用了没冻结的图标名时，`<use>` 指向不存在的 id，结果是**一片空白且无报错**。

所以这里钉住三件事：源数据与 sprite 同步、引用全部有源、两个样式表里的 `.obx-icon` 逐值一致。
（真实浏览器里的渲染验证见 tests/debug_shell_icons_ui.py。）
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.build_icons import (
    DATA_FILE,
    SPRITE_FILE,
    collect_references,
    render,
)
from tools.check_plugins import UI_EMOJI_RE

SHELL_CSS = PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'styles' / 'shell.css'
BASE_CSS = PROJECT_ROOT / 'shell' / 'frontend' / 'public' / 'shell' / 'base.css'
ICON_COMPONENT = PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'components' / 'Icon.vue'

SYMBOL_RE = re.compile(r'<symbol\s+id="([a-z0-9-]+)"([^>]*)>(.*?)</symbol>', re.DOTALL)
CSS_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)
CSS_BLOCK_RE = re.compile(r'\.obx-icon\s*\{([^}]*)\}')
WHITESPACE_RE = re.compile(r'\s+')


def load_data() -> dict:
    return json.loads(DATA_FILE.read_text(encoding='utf-8'))


def sprite_symbols() -> dict:
    text = SPRITE_FILE.read_text(encoding='utf-8')
    return {name: attrs for name, attrs, _inner in SYMBOL_RE.findall(text)}


class IconDataTests(unittest.TestCase):
    """冻结的图标源：名字、授权、图形本体。"""

    def test_icon_data_exists_and_has_icons(self):
        data = load_data()
        self.assertTrue(data['icons'], 'icon_data.json 里没有任何图标')
        self.assertEqual(data['library'], 'lucide')
        self.assertEqual(data['license'], 'ISC')

    def test_every_icon_has_geometry_and_viewbox(self):
        for name, icon in load_data()['icons'].items():
            with self.subTest(icon=name):
                self.assertRegex(name, r'^[a-z0-9]+(?:-[a-z0-9]+)*$', f'{name} 不是 kebab-case')
                self.assertTrue(icon['inner'].strip(), f'{name} 图形本体为空')
                self.assertEqual(icon['viewBox'], '0 0 24 24', f'{name} viewBox 非预期')
                self.assertIn('ISC', icon['source'], f'{name} 缺少 Lucide 的 ISC 授权标记')

    def test_icon_data_carries_lucide_version(self):
        """授权与版本要能追溯：出问题时要能回答"这批图形来自哪个版本"。"""
        self.assertRegex(load_data()['package'], r'^lucide-static@\d+\.\d+\.\d+$')


class SpriteBuildTests(unittest.TestCase):
    """sprite 生成物：与源数据同步、且各属性齐全（漏了会静默画不出来）。"""

    def test_sprite_matches_icon_data(self):
        """sprite 与 icon_data.json 必须一致：改了源数据就要重新生成。"""
        current = SPRITE_FILE.read_text(encoding='utf-8')
        self.assertEqual(
            current, render(load_data()['icons'], load_data()),
            'icons.svg 与 res/icons/icon_data.json 不一致：运行 venv/Scripts/python tools/build_icons.py',
        )

    def test_sprite_contains_exactly_the_frozen_icons(self):
        self.assertEqual(set(sprite_symbols()), set(load_data()['icons']))

    def test_symbols_carry_stroke_and_viewbox(self):
        """描边必须写在 symbol 上且是 currentColor —— 否则图标不跟随主题。"""
        for name, attrs in sprite_symbols().items():
            with self.subTest(icon=name):
                self.assertIn('viewBox="0 0 24 24"', attrs)
                self.assertIn('stroke="currentColor"', attrs)
                self.assertIn('fill="none"', attrs)
                self.assertIn('stroke-width="2"', attrs)

    def test_sprite_header_records_source_and_license(self):
        head = SPRITE_FILE.read_text(encoding='utf-8')[:900]
        self.assertIn('lucide-static@', head)
        self.assertIn('ISC', head)
        self.assertIn('tools/build_icons.py', head)


class IconReferenceTests(unittest.TestCase):
    """引用一致性：模板/manifest/后端用到的图标必须都已冻结。"""

    def test_every_reference_is_frozen(self):
        icons = set(load_data()['icons'])
        for name, files in sorted(collect_references().items()):
            with self.subTest(icon=name):
                self.assertIn(
                    name, icons,
                    f'{name} 被引用（{"、".join(sorted(set(files)))}）但未冻结：'
                    f'运行 tools/fetch_lucide_icons.py --add {name}',
                )

    def test_manifests_use_frozen_icons(self):
        """8 个内置插件的 manifest 图标必须都是 sprite 里存在的名字。"""
        icons = set(load_data()['icons'])
        manifests = sorted((PROJECT_ROOT / 'plugins').glob('*/manifest.json'))
        self.assertEqual(len(manifests), 8, '内置插件数量变了，这条用例要一起看')
        for path in manifests:
            value = json.loads(path.read_text(encoding='utf-8'))['icon']
            with self.subTest(plugin=path.parent.name):
                self.assertTrue(value.startswith('icon:'), f'{path.parent.name} 的图标不是 sprite 引用：{value}')
                self.assertIn(value[len('icon:'):], icons)

    def test_backend_fallback_icon_is_frozen(self):
        """manifest 缺 icon 字段时后端给的默认值也必须在 sprite 里有。"""
        source = (PROJECT_ROOT / 'shell' / 'backend' / 'plugin_manager.py').read_text(encoding='utf-8')
        match = re.search(r"m\.get\('icon',\s*'icon:([a-z0-9-]+)'\)", source)
        self.assertIsNotNone(match, 'plugin_manager 的默认图标写法变了，用例要同步')
        self.assertIn(match.group(1), set(load_data()['icons']))


class IconStyleTests(unittest.TestCase):
    """共享样式与引用路径：壳页面与插件页必须拿到同一份视觉。"""

    def _icon_block(self, path: Path) -> str:
        text = CSS_COMMENT_RE.sub('', path.read_text(encoding='utf-8'))
        match = CSS_BLOCK_RE.search(text)
        self.assertIsNotNone(match, f'{path.name} 里没有 .obx-icon 规则')
        return WHITESPACE_RE.sub(' ', match.group(1)).strip()

    def test_obx_icon_rule_is_identical_in_both_stylesheets(self):
        """壳页面不注入 base.css，两份 .obx-icon 必须逐值一致，否则壳页与插件页图标大小不同。"""
        self.assertEqual(
            self._icon_block(BASE_CSS), self._icon_block(SHELL_CSS),
            '.obx-icon 在 base.css 与 shell.css 里已经不一致，两份要同步改',
        )

    def test_obx_icon_has_explicit_size(self):
        """只写 viewBox 的 <svg> 会按 300×150 渲染，尺寸必须显式给。"""
        block = self._icon_block(BASE_CSS)
        self.assertIn('width: 1em', block)
        self.assertIn('height: 1em', block)

    def test_component_uses_absolute_shell_path(self):
        """`href` 必须是绝对路径：壳是 history 路由，相对路径在嵌套路由下会解析错。"""
        source = ICON_COMPONENT.read_text(encoding='utf-8')
        self.assertIn('/res/icons/icons.svg#', source)
        self.assertNotIn("href=\"icons.svg#", source)


class EmojiGatePolicyTests(unittest.TestCase):
    """emoji 门禁的策略本身（存量未清完，规则先就位，见 tools/check_plugins.py 的说明）。"""

    def test_pictographic_emoji_is_matched(self):
        for char in ('🖼', '🔄', '🗑', '🎧'):
            with self.subTest(char=char):
                self.assertIsNotNone(UI_EMOJI_RE.search(char))

    def test_symbol_glyphs_are_deliberately_allowed(self):
        """箭头、几何符号与符号类（✓✕⚠★☆❤❮❯）不在拦截范围：跨平台字形稳定，且部分是数据语义。"""
        for char in ('✕', '⚠', '★', '☆', '❤', '✓', '→', '❮', '❯', '✦', '⑨'):
            with self.subTest(char=char):
                self.assertIsNone(UI_EMOJI_RE.search(char))

    def test_variation_selector_alone_is_not_an_error(self):
        """U+FE0F 只修饰前一个字符，单独出现不应判错（否则错误信息指向不可见的字符）。"""
        self.assertIsNone(UI_EMOJI_RE.search('\ufe0f'))


if __name__ == '__main__':
    unittest.main()
