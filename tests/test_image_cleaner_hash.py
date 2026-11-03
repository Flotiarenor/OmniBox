"""image-cleaner 的「完全重复」判定安全回归测试。

历史缺陷（会导致真实照片被误删）：
`_file_full_hash()` 读取失败时 `except OSError: pass` 后照样返回
`hashlib.md5()` 的**空内容摘要** `d41d8cd98f00b204e9800998ecf8427e`。于是所有
读不到/读不全的文件共享同一个 digest，被 `duplicate_scan()` 归成"完全重复"分组，
而该插件的下一步就是 `delete_files()` → `unlink()` —— 用户点一次"删除重复"
就会永久删掉本不重复的图片。文件在扫描期间被写入（同步/复制进行中）最容易触发。

这里把"不可信摘要绝不参与分组"固化为断言。

组件化之后，摘要与分组实现在 `backend/duplicates.py`、dHash 在 `backend/hashes.py`、
相似分组在 `backend/similar.py`；本用例直接构造组件，不需要启动插件。

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
BACKEND = PROJECT_ROOT / 'plugins' / 'image-cleaner' / 'backend'


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f'image_cleaner_{name}_test', str(BACKEND / f'{name}.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cancel = _load('cancel')
duplicates = _load('duplicates')
hashes = _load('hashes')
similar = _load('similar')


class FullHashTests(unittest.TestCase):
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
        ha = duplicates.DuplicateFinder._file_full_hash(str(a))
        hb = duplicates.DuplicateFinder._file_full_hash(str(b))
        self.assertIsNotNone(ha)
        self.assertEqual(ha, hb, '内容相同的文件摘要应一致')
        self.assertNotEqual(ha, EMPTY_MD5)

    def test_unreadable_file_returns_none_not_empty_digest(self):
        """核心断言：读不到的文件必须返回 None，而不是空内容摘要。"""
        a = self._write('a.bin', b'hello')
        with mock.patch('builtins.open', side_effect=OSError('模拟占用/权限不足')):
            result = duplicates.DuplicateFinder._file_full_hash(str(a))
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
            result = duplicates.DuplicateFinder._file_full_hash(str(a))
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
            result = duplicates.DuplicateFinder._file_full_hash(str(a))
        self.assertIsNone(result, '读取期间文件被改动，摘要不可信')

    def test_quick_hash_returns_none_when_unreadable(self):
        a = self._write('a.bin', b'z' * 1024)
        with mock.patch('builtins.open', side_effect=OSError('boom')):
            self.assertIsNone(duplicates.DuplicateFinder._file_quick_hash(str(a), 1024))


class _Settings:
    """最小设置端口：相似判定的阈值总是显式传入，这里只兜住默认值。"""

    def setting(self, key, default=None):
        return default


class DHashTests(unittest.TestCase):
    """dHash 的"读图失败"与"哈希值恰好是 0"必须分开。

    历史缺陷：`_image_dhash` 读图失败时返回 0，调用方又用 `if h:` 过滤 —— 于是
    纯色/近纯色图（dHash 本来就是 0）永远进不了相似分组，而失败也返回 0 让两种
    含义混在一起。失败还会被写进缓存，mtime 不变就永远不再重算。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.store = hashes.DHashStore(lambda: self.dir, 2)
        self.finder = similar.SimilarFinder(_Settings(), self.store, cancel.check_cancel)

    def _write_images(self):
        from PIL import Image
        Image.new('RGB', (24, 24), (10, 120, 200)).save(self.dir / 'a.jpg')
        (self.dir / 'broken.jpg').write_bytes(b'not an image at all')

    def _entry(self, name: str) -> dict:
        path = self.dir / name
        return {'rel': name, 'abs': str(path), 'size': path.stat().st_size,
                'mtime': path.stat().st_mtime}

    def test_uniform_image_hash_is_zero_and_valid(self):
        """纯色图的 dHash 就是 0 —— 它是合法值，不是"读图失败"。"""
        self._write_images()
        self.assertEqual(self.store.get(str(self.dir / 'a.jpg'), 0), 0)

    def test_unreadable_image_returns_none_and_is_not_cached(self):
        self._write_images()
        broken = str(self.dir / 'broken.jpg')
        self.assertIsNone(self.store.get(broken, 0), '读图失败必须返回 None')
        self.assertEqual(self.store.entries, {}, '失败不能写进缓存（否则 mtime 不变就不再重算）')

    def test_legacy_cache_entry_without_version_is_recomputed(self):
        """旧版把失败写成 hash 0 存进缓存：缺 'v' 的条目一律重算。"""
        self._write_images()
        import hashlib
        key = 'dhash:' + hashlib.md5(str(self.dir / 'a.jpg').encode()).hexdigest()
        self.store.entries[key] = {'mtime': 0.0, 'hash': 12345}   # 旧格式（无 'v'）
        self.assertEqual(self.store.get(str(self.dir / 'a.jpg'), 0.0), 0,
                         '旧的 0 不能被当成合法哈希沿用')
        self.assertEqual(self.store.entries[key]['v'], 2, '重算后写成新格式')

    def test_uniform_images_are_grouped_as_similar(self):
        """端到端：两张纯色图必须进相似组（旧实现里它们永远被丢掉）。"""
        from PIL import Image
        for name in ('a.jpg', 'b.jpg'):
            Image.new('RGB', (24, 24), (10, 120, 200)).save(self.dir / name)
        files = [self._entry('a.jpg'), self._entry('b.jpg')]

        digests, valid = self.finder.dhash_all(files)
        groups = self.finder.groups(digests, valid, 0)

        self.assertEqual([sorted(g['files']) for g in groups],
                         [['a.jpg', 'b.jpg']], groups)

    def test_unreadable_image_is_not_grouped_as_similar(self):
        self._write_images()
        files = [self._entry('a.jpg'), self._entry('broken.jpg')]

        digests, valid = self.finder.dhash_all(files)
        groups = self.finder.groups(digests, valid, 16)

        self.assertEqual(groups, [], '读不了的图不能进相似分组（下一步可能是删除）')


class DuplicateScanGroupingTests(unittest.TestCase):
    """不可信摘要不得进入分组结果。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_unreadable_files_are_not_grouped_as_duplicates(self):
        finder = duplicates.DuplicateFinder(cancel.check_cancel)
        # 两个"读不到"的文件，大小相同 —— 旧实现会把它们判成完全重复
        files = [
            {'rel': 'x/a.jpg', 'abs': str(self.dir / 'a.jpg'), 'size': 100},
            {'rel': 'x/b.jpg', 'abs': str(self.dir / 'b.jpg'), 'size': 100},
        ]
        for f in files:
            Path(f['abs']).write_bytes(b'\x00' * 100)

        with mock.patch.object(
            duplicates.DuplicateFinder, '_file_full_hash', staticmethod(lambda p: None)
        ):
            groups = finder.groups(files)

        self.assertEqual(groups, [],
                         '摘要不可信的文件绝不能组成"完全重复"分组（下一步就是删除）')


if __name__ == '__main__':
    unittest.main()
