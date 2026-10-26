"""document-reader 设置 schema：朗读页字段 hidden，`tts_order` 例外保留在壳弹窗。

背景：朗读设置已迁到左栏的「朗读设置」页，`settings_schema` 里对应的 `tts_*` 字段
只为 `save_settings` 的写入白名单而保留，统一标 `hidden: True`，否则壳的设置弹窗会
重复列一遍。

`tts_order`（自动模式顺序）是**有意的例外**：朗读设置页没有这一项的界面入口，
隐藏它等于删掉用户改自动降级顺序的唯一路径。它留在壳设置弹窗里，代价是那里会多出
一个只有该项的「朗读」分组；后续若要清理这个分组，应当先把入口加到朗读设置页，
再标 hidden，而不是反过来先隐藏。

迁移尾项：`frontend.chrome.content` 是壳未消费的死声明，已删除。
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'document-reader'

# 朗读设置页上的字段（见 frontend/js/reader-voice-page.js 的 dom 表）
VOICE_PAGE_TTS_KEYS = ('tts_engine', 'tts_base_url', 'tts_api_key',
                       'tts_model', 'tts_voice', 'tts_rate', 'tts_prefetch')


def _load_plugin_class():
    path = PLUGIN_DIR / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('document_reader_schema_test', str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DocumentReaderPlugin


class TtsSchemaHiddenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin_cls = _load_plugin_class()
        cls.schema = {item['key']: item for item in cls.plugin_cls.settings_schema}

    def test_voice_page_tts_fields_are_hidden(self):
        for key in VOICE_PAGE_TTS_KEYS:
            self.assertIn(key, self.schema, f'{key} 没有声明')
            self.assertIs(self.schema[key].get('hidden'), True,
                          f'{key} 已迁到朗读设置页，不应再显示在壳设置弹窗里')

    def test_tts_order_stays_visible_as_its_only_entry(self):
        """`tts_order` 是例外：朗读页没有入口，隐藏会删掉唯一配置路径。"""
        order = self.schema.get('tts_order')
        self.assertIsNotNone(order, 'tts_order 不能删掉：schema 同时是写入白名单')
        self.assertIsNot(order.get('hidden'), True)


class ManifestChromeTests(unittest.TestCase):
    def test_manifest_chrome_has_no_content_field(self):
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        chrome = manifest['frontend']['chrome']
        self.assertNotIn('content', chrome)
        for key in ('brand', 'nav', 'toolbar', 'search', 'status', 'freshness'):
            self.assertIn(key, chrome, key)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
