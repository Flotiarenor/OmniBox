"""统一日志的回归测试（docs/code-review.md §4.2）。

发行版打包 console=False，此时 CPython 的 sys.stdout/sys.stderr 是 None，
`print` 会静默丢弃一切诊断信息 —— 发布版等于闭眼运行。因此日志的**文件通道**
必须真的落盘，并且在没有控制台时不能因为写 None 而报错。

运行：
    python -m unittest tests.test_app_logging -v
"""

import logging
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend import app_logging


class AppLoggingTests(unittest.TestCase):
    def setUp(self):
        app_logging.reset_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        # 顺序很重要（addCleanup 是 LIFO）：必须先关掉日志 handler 再删临时目录，
        # 否则 Windows 上日志文件仍被占用，清理会报 WinError 32。
        # 先注册的 tmp 清理会后执行，后注册的 reset 会先执行。
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(app_logging.reset_for_tests)
        self.config_dir = Path(self._tmp.name)

    def _read_log(self, log_file: Path) -> str:
        logging.getLogger().handlers[0].flush()
        return log_file.read_text(encoding='utf-8') if log_file.exists() else ''

    def test_writes_to_file(self):
        log_file = app_logging.setup_logging(self.config_dir, console=False)
        self.assertIsNotNone(log_file)
        logger = logging.getLogger('omnibox.test')
        logger.warning('落盘 检查 %s', 'OK')
        content = self._read_log(log_file)
        self.assertIn('落盘 检查 OK', content)
        self.assertIn('WARNING', content)
        self.assertIn('omnibox.test', content)

    def test_works_without_console(self):
        """模拟打包后的 console=False：sys.stderr is None 也必须能记日志。"""
        original = sys.stderr
        sys.stderr = None
        try:
            log_file = app_logging.setup_logging(self.config_dir)
            self.assertIsNotNone(log_file)
            logging.getLogger('omnibox.noconsole').error('没有控制台也要落盘')
        finally:
            sys.stderr = original
        content = self._read_log(log_file)
        self.assertIn('没有控制台也要落盘', content)
        self.assertNotIn('StreamHandler', content)

    def test_is_idempotent(self):
        first = app_logging.setup_logging(self.config_dir, console=False)
        handlers_after_first = len(logging.getLogger().handlers)
        second = app_logging.setup_logging(self.config_dir, console=False)
        self.assertEqual(first, second)
        self.assertEqual(len(logging.getLogger().handlers), handlers_after_first, '重复调用不应重复挂 handler')
        logging.getLogger('omnibox.once').info('只应出现一次')
        content = self._read_log(first)
        self.assertEqual(content.count('只应出现一次'), 1)

    def test_survives_unwritable_log_dir(self):
        """配置目录不可用时不能把启动搞挂，返回 None 即可。"""
        bogus = Path(self._tmp.name) / 'not-a-dir'
        bogus.write_text('x', encoding='utf-8')  # 同名文件让 mkdir 失败
        log_file = app_logging.setup_logging(bogus, console=False)
        self.assertIsNone(log_file)
        self.assertTrue(app_logging.is_configured())

    def test_log_dir_location(self):
        self.assertEqual(app_logging.get_log_dir(self.config_dir), self.config_dir / 'logs')
        log_file = app_logging.setup_logging(self.config_dir, console=False)
        self.assertEqual(log_file.parent, self.config_dir / 'logs')
        self.assertEqual(log_file.name, 'omnibox.log')


if __name__ == '__main__':
    unittest.main()
