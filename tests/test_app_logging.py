"""统一日志的回归测试。

发行版打包 console=False，此时 CPython 的 sys.stdout/sys.stderr 是 None，
`print` 会静默丢弃一切诊断信息 —— 发布版等于闭眼运行。因此日志的**文件通道**
必须真的落盘，并且在没有控制台时不能因为写 None 而报错。

运行：
    python -m unittest tests.test_app_logging -v
"""

import io
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def _file_handler(self):
        for handler in logging.getLogger().handlers:
            if isinstance(handler, app_logging.SafeRotatingFileHandler):
                return handler
        self.fail('没有挂上 SafeRotatingFileHandler')
        raise AssertionError  # 让类型检查看到"这里不会正常返回"

    def test_rollover_keeps_writing_when_log_file_is_locked(self):
        """日志文件被别的进程占着时，滚动失败也必须继续写、不丢这一条。

        缺陷背景：标准库的 doRollover 是 `os.rename(omnibox.log, omnibox.log.1)`，
        而 Windows 不允许重命名被**任何**进程持有句柄的文件。本仓库里这是常态 ——
        应用在跑，同时又开着 --web-only 实例或 e2e 用例，两者写同一个 logs/omnibox.log，
        先到上限的那个进程一滚动就抛 PermissionError，logging 把它打到 stderr
        （用户看到的那段 "--- Logging error ---"），而且那条日志本身也没落盘。

        这里模拟"替换目标被占用"：让 `os.replace` 抛 PermissionError，断言
        （1）不抛异常、（2）后续日志仍然写得进去。
        """
        log_file = app_logging.setup_logging(self.config_dir, console=False)
        handler = self._file_handler()
        handler.maxBytes = 1  # 下一次写入必然触发滚动

        real_replace = os.replace

        def locked(source, target):
            if str(target).endswith('omnibox.log'):
                raise PermissionError(32, '另一个程序正在使用此文件')
            return real_replace(source, target)

        logger = logging.getLogger('omnibox.locked')
        with mock.patch.object(app_logging.os, 'replace', side_effect=locked):
            logger.warning('占用时仍要落盘')
        self.assertIn('占用时仍要落盘', self._read_log(log_file))

    def test_rollover_rotates_and_keeps_backups(self):
        """正常滚动：内容进 .1，当前文件重新开始，语义与标准库一致。"""
        log_file = app_logging.setup_logging(self.config_dir, console=False)
        handler = self._file_handler()
        logger = logging.getLogger('omnibox.rotate')
        logger.warning('第一批内容')
        handler.maxBytes = 1
        logger.warning('第二批内容')

        first = Path(f'{log_file}.1')
        self.assertTrue(first.exists(), '滚动后应生成 omnibox.log.1')
        self.assertIn('第一批内容', first.read_text(encoding='utf-8'))
        self.assertIn('第二批内容', self._read_log(log_file))

    def test_rollover_failure_does_not_print_traceback(self):
        """滚动失败时不能往 stderr 打 "--- Logging error ---" + 调用栈。"""
        app_logging.setup_logging(self.config_dir, console=False)
        handler = self._file_handler()
        handler.maxBytes = 1
        stderr = io.StringIO()
        with (
            mock.patch.object(app_logging.os, 'replace',
                              side_effect=PermissionError(32, '占用')),
            mock.patch.object(sys, 'stderr', stderr),
        ):
            logging.getLogger('omnibox.quiet').warning('不吵')
        self.assertNotIn('Logging error', stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
