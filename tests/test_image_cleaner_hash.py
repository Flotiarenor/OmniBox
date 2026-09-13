"""image-cleaner 的「完全重复」判定安全回归测试。

历史缺陷（会导致真实照片被误删）：
`_file_full_hash()` 读取失败时 `except OSError: pass` 后照样返回
`hashlib.md5()` 的**空内容摘要** `d41d8cd98f00b204e9800998ecf8427e`。于是所有
读不到/读不全的文件共享同一个 digest，被 `duplicate_scan()` 归成"完全重复"分组，
而该插件的下一步就是 `delete_files()` → `unlink()` —— 用户点一次"删除重复"
就会永久删掉本不重复的图片。文件在扫描期间被写入（同步/复制进行中）最容易触发。

这里把"不可信摘要绝不参与分组"固化为断言。

运行：
    python -m unittest tests.test_image_cleaner_hash -v
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

EMPTY_MD5 = 'd41d8cd98f00b204e9800998ecf8427e'


def _load_plugin_module():
    main_path = PROJECT_ROOT / 'plugins' / 'image-cleaner' / 'backend' / 'main.py'
    spec = importlib.util.spec_from_file_location('image_cleaner_main_test', str(main_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FullHashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_plugin_module()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, name, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_readable_file_hash_is_stable_and_not_empty_digest(self):
        a = self._write('a.bin', b'x' * 4096)
        b = self._write('b.bin', b'x' * 4096)
        ha = self.module.ImageCleanerPlugin._file_full_hash(str(a))
        hb = self.module.ImageCleanerPlugin._file_full_hash(str(b))
        self.assertIsNotNone(ha)
        self.assertEqual(ha, hb, '内容相同的文件摘要应一致')
        self.assertNotEqual(ha, EMPTY_MD5)

    def test_unreadable_file_returns_none_not_empty_digest(self):
        """核心断言：读不到的文件必须返回 None，而不是空内容摘要。"""
        a = self._write('a.bin', b'hello')
        with mock.patch('builtins.open', side_effect=OSError('模拟占用/权限不足')):
            result = self.module.ImageCleanerPlugin._file_full_hash(str(a))
        self.assertIsNone(result, '读取失败必须返回 None，不能返回空内容摘要')

    def test_oversized_empty_read_is_rejected(self):
        """声明有内容却一个字节都读不出来（正在被写入）→ 不可信。"""
        a = self._write('a.bin', b'hello world')
        real_open = open

        class _EmptyReader:
            def __init__(self, fh):
                self._fh = fh

            def read(self, *args):
                return b''

            def fileno(self):
                return self._fh.fileno()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._fh.close()
                return False

        def fake_open(path, *args, **kwargs):
            return _EmptyReader(real_open(path, *args, **kwargs))

        with mock.patch('builtins.open', side_effect=fake_open):
            result = self.module.ImageCleanerPlugin._file_full_hash(str(a))
        self.assertIsNone(result)

    def test_file_changed_during_read_is_rejected(self):
        """读取期间文件大小发生变化 → 摘要不可信。"""
        a = self._write('a.bin', b'y' * 2048)
        real_fstat = __import__('os').fstat
        calls = {'n': 0}

        def fake_fstat(fd):
            calls['n'] += 1
            real = real_fstat(fd)
            if calls['n'] == 1:
                return real
            # 第二次读到的大小与第一次不同
            return type(real)((real.st_mode, real.st_ino, real.st_dev, real.st_nlink,
                               real.st_uid, real.st_gid, real.st_size + 1,
                               real.st_atime, real.st_mtime, real.st_ctime))

        with mock.patch('os.fstat', side_effect=fake_fstat):
            result = self.module.ImageCleanerPlugin._file_full_hash(str(a))
        self.assertIsNone(result, '读取期间文件被改动，摘要不可信')

    def test_quick_hash_returns_none_when_unreadable(self):
        a = self._write('a.bin', b'z' * 1024)
        with mock.patch('builtins.open', side_effect=OSError('boom')):
            self.assertIsNone(self.module.ImageCleanerPlugin._file_quick_hash(str(a), 1024))


class DuplicateScanGroupingTests(unittest.TestCase):
    """不可信摘要不得进入分组结果。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.module = _load_plugin_module()

    def test_unreadable_files_are_not_grouped_as_duplicates(self):
        plugin = self.module.ImageCleanerPlugin.__new__(self.module.ImageCleanerPlugin)
        # 两个"读不到"的文件，大小相同 —— 旧实现会把它们判成完全重复
        files = [
            {'rel': 'x/a.jpg', 'abs': str(self.dir / 'a.jpg'), 'size': 100},
            {'rel': 'x/b.jpg', 'abs': str(self.dir / 'b.jpg'), 'size': 100},
        ]
        for f in files:
            Path(f['abs']).write_bytes(b'\x00' * 100)

        plugin._all_album_files = lambda: files
        plugin._save_scan_result = lambda *a, **k: None

        with mock.patch.object(
            self.module.ImageCleanerPlugin, '_file_full_hash', staticmethod(lambda p: None)
        ):
            result = plugin.duplicate_scan()

        self.assertEqual(result['groups'], [],
                         '摘要不可信的文件绝不能组成"完全重复"分组（下一步就是删除）')


if __name__ == '__main__':
    unittest.main()
