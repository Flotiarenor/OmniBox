"""manga-library manifest 的迁移尾项：删除壳未消费的 `frontend.chrome.content`。

`content`（canvas/native/hybrid）只被 `tools/check_plugins.py` 校验枚举值，
`PluginChrome`/`App.vue` 都没有读取方；manga-library 还声明成了与实际形态不符的
`hybrid`。按结论删除该字段，保留其余骨架声明。
"""

import json
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'manga-library'


class ManifestChromeTests(unittest.TestCase):
    def test_manifest_chrome_has_no_content_field(self):
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        chrome = manifest['frontend']['chrome']
        self.assertNotIn('content', chrome)
        for key in ('brand', 'nav', 'toolbar', 'search', 'status', 'freshness'):
            self.assertIn(key, chrome, key)
        self.assertIs(chrome['status'], True)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
