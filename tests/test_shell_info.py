"""壳自身信息端点（shell/backend/shell_info.py + ThumbCache 维护入口）的回归测试。

为什么要固化这几条：

- `stats()` 若直接走 `_connect()`，光是"看一眼占用"就会给每个插件凭空建出一个
  空数据库（且占用不再为 0），清理按钮的"释放了多少"也会失真；
- 日志级别是**下一次启动、界面出现之前**就要生效的值，落盘格式或键名写错只会
  表现为"设置看起来成功、重启后回到 INFO"。

运行：
    python -m unittest tests.test_shell_info -v
"""

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.shell_info import (
    LOG_LEVELS,
    SHELL_SETTINGS_NAME,
    ShellInfo,
    stored_log_level,
)
from shell.backend.thumb_cache import ThumbCache, cache_stats, clear_all_caches


class ThumbCacheStatsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.cache = ThumbCache(self.dir / '.cache' / 'thumbs.db')

    def _seed(self):
        src = self.dir / 'a.jpg'
        src.write_bytes(b'x' * 32)
        self.assertTrue(self.cache.put('a.jpg', b'fake-jpeg-bytes', 'image/jpeg', src))

    def test_stats_does_not_create_db(self):
        self.assertEqual(self.cache.stats(), {'count': 0, 'bytes': 0})
        self.assertFalse(self.cache.db_path.exists(), '只读占用不应建出数据库文件')

    def test_stats_counts_rows_and_bytes(self):
        self._seed()
        stats = self.cache.stats()
        self.assertEqual(stats['count'], 1)
        self.assertGreater(stats['bytes'], 0)
        self.assertIn(str(self.cache.db_path), [item['path'] for item in cache_stats()])

    def test_clear_all_caches_empties_this_cache(self):
        self._seed()
        result = clear_all_caches()
        self.assertGreaterEqual(result['caches'], 1)
        self.assertGreaterEqual(result['freed_bytes'], 0)
        self.assertEqual(self.cache.stats()['count'], 0)


class ShellInfoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.info = ShellInfo({'directories': {'data_root': str(self.dir / 'data')}}, str(self.dir))
        self._previous_level = logging.getLogger().level
        self.addCleanup(logging.getLogger().setLevel, self._previous_level)

    def _saved(self):
        file = self.dir / f'{SHELL_SETTINGS_NAME}.json'
        return json.loads(file.read_text(encoding='utf-8')) if file.exists() else None

    def test_rejects_unknown_level_without_writing(self):
        for bad in ('VERBOSE', '', None, 5):
            self.assertFalse(self.info.set_log_level(bad)['success'], f'{bad!r} 不该被接受')
        self.assertIsNone(self._saved(), '非法级别不应落盘')

    def test_accepts_level_applies_and_persists(self):
        result = self.info.set_log_level('warning')
        self.assertTrue(result['success'])
        self.assertEqual(result['level'], 'WARNING')
        self.assertEqual(self._saved(), {'log_level': 'WARNING'})
        self.assertEqual(logging.getLogger().level, logging.WARNING)
        self.assertEqual(stored_log_level(str(self.dir)), logging.WARNING)
        self.assertEqual(self.info.get_info()['log_level'], 'WARNING')

    def test_absent_or_invalid_stored_level_falls_back_to_info(self):
        self.assertEqual(stored_log_level(str(self.dir)), logging.INFO)
        (self.dir / f'{SHELL_SETTINGS_NAME}.json').write_text(
            '{"log_level": "NOPE"}', encoding='utf-8'
        )
        self.assertEqual(stored_log_level(str(self.dir)), logging.INFO)

    def test_info_reports_paths_and_totals(self):
        info = self.info.get_info()
        self.assertEqual(info['data_root'], str(self.dir / 'data'))
        self.assertEqual(info['config_dir'], str(self.dir))
        self.assertTrue(info['log_file'].endswith('omnibox.log'))
        self.assertIn(info['log_level'], LOG_LEVELS)
        self.assertEqual(info['cache_total']['count'], sum(c['count'] for c in info['caches']))


if __name__ == '__main__':
    unittest.main()
