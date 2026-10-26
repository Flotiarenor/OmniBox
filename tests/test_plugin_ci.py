"""插件 CI 测试选择的纯逻辑用例（不启动浏览器、不跑子进程）。"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from tools import plugin_ci


class TestPluginCiClassification(unittest.TestCase):
    def test_plugin_list_comes_from_manifests(self):
        names = plugin_ci.plugin_names()
        self.assertIn('image-viewer', names)
        self.assertIn('media-player', names)
        self.assertEqual(names, sorted(names))

    def test_document_reader_alias_is_classified(self):
        self.assertEqual(
            plugin_ci.test_owner('document_reader_tts'),
            'document-reader',
        )
        self.assertEqual(plugin_ci.test_owner('reader_voice_cards_js'), 'document-reader')

    def test_plugin_tests_are_classified_by_prefix(self):
        self.assertEqual(plugin_ci.test_owner('media_player_index'), 'media-player')
        self.assertEqual(plugin_ci.test_owner('image_cleaner_hash'), 'image-cleaner')
        self.assertIsNone(plugin_ci.test_owner('plugin_spec'))
        self.assertIsNone(plugin_ci.test_owner('shell_extensions_js'))

    def test_every_test_file_belongs_to_exactly_one_bucket(self):
        files = {path.stem for path in (plugin_ci.PROJECT_ROOT / 'tests').glob('test_*.py')}
        buckets = [set(plugin_ci.selected_stems('core', None))]
        for plugin in plugin_ci.plugin_names():
            bucket = set(plugin_ci.selected_stems('plugin', plugin))
            buckets.append(bucket)
        union = set().union(*buckets)
        self.assertEqual(files, union)
        total = sum(len(bucket) for bucket in buckets)
        self.assertEqual(total, len(union))

    def test_plugin_selection_has_tests_for_each_plugin(self):
        for plugin in plugin_ci.plugin_names():
            with self.subTest(plugin=plugin):
                stems = plugin_ci.selected_stems('plugin', plugin)
                self.assertTrue(stems, f'{plugin} 没有可运行的测试文件')

    def test_missing_plugin_raises(self):
        with self.assertRaises(ValueError):
            plugin_ci.selected_stems('plugin', 'not-a-plugin')

    def test_list_output_is_json_array(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(plugin_ci.main(['--list']), 0)
        self.assertEqual(json.loads(buffer.getvalue()), plugin_ci.plugin_names())


if __name__ == '__main__':
    unittest.main()
