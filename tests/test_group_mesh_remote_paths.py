"""远端路径不可信：物化 / 镜像 / 对账删除都不得作用到根之外。

背景（审计项 P1-5）：`_walk_remote` 直接采用对端应答里的 `entries[].name` 组装相对
路径，唯一的过滤是"非空且不是 `.` / `..`"，随后调用方把它拼成本机路径（`root / rel`）。
Python 的 `Path` 除法在 rel 是绝对路径时**直接返回该路径**，`..` 段也不被拒绝 ——
于是名单内的任一设备（甚至先用 `op=registry push` 把自己的设备塞进受害者对端列表的
名单外设备）可以让受害者在一次「缓存」/「取回」里把根外文件覆盖成攻击者内容，或删掉
根外文件。

本文件用假对端应答锁住三件事：
  1. `safe_rel` 的归一与拒绝规则（绝对路径、盘符、`..` 段、NUL、反斜杠）；
  2. `_walk_remote` 丢弃越界条目，而不是把它带进 `walked`；
  3. 写（`_mirror_files`）与删（`_prune_absent`）之前各自还有一道根内复核。

运行：
    python -m unittest tests.test_group_mesh_remote_paths -v
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.groupmesh import client as mesh_client
from tests.harness.support import same_path


def _load(name: str):
    path = PROJECT_ROOT / 'plugins' / 'group-mesh' / 'backend' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'gm_remote_paths_{name}', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_materialize = _load('materialize')
_mirror = _load('mirror')
_const = _load('const')


def _host(component):
    """组件实例的"只装依赖"版本：这些纯路径方法不碰连接、存储与网络。

    组件之间不互相 import，依赖一律由入口注入，因此用例只要把 `MeshRules` 挂上就能
    单独验证归一与拒绝规则（这正是组件化的目的一：不启动插件就能测一个能力）。
    """
    instance = component.__new__(component)
    instance._rules = _const.RULES
    return instance


class SafeRelTests(unittest.TestCase):
    """归一规则本身：这是物化与镜像共用的唯一一处判定。"""

    def test_legal_paths_pass_through(self):
        for value, expected in (
            ('img.jpg', 'img.jpg'),
            ('作者/作品/1.jpg', '作者/作品/1.jpg'),
            ('a//b', 'a/b'),
            ('a/./b', 'a/b'),
            ('目录\\文件.jpg', '目录/文件.jpg'),      # Windows 分隔符归一
        ):
            with self.subTest(value=value):
                self.assertEqual(_const.RULES.safe_rel(value), expected)

    def test_illegal_paths_are_rejected(self):
        for value in (
            '', '   ', None, 123,
            '/etc/passwd',                     # POSIX 绝对路径
            'C:/Windows/System32/config/SAM',  # 盘符绝对路径
            '\\\\server\\share\\x',            # UNC
            '..', '../x', 'a/../../x', 'x/..',
            'a\x00b',
        ):
            with self.subTest(value=value):
                self.assertIsNone(_const.RULES.safe_rel(value))

    def test_within_root_rejects_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            outside = Path(tmp) / 'outside.txt'
            outside.write_text('x', encoding='utf-8')

            # 比对指向同一位置而不是逐字比字符串：临时目录在 Windows 上可能是 8.3 短名
            # （CI 的 C:\Users\RUNNER~1\…），而 within_root 走 resolve() 得到长名，
            # 逐字比较会假失败。同款判定见 tests/harness/support.py::same_path。
            self.assertTrue(same_path(_const.RULES.within_root(root, 'a/b.txt'), root / 'a' / 'b.txt'))
            self.assertIsNone(_const.RULES.within_root(root, '../outside.txt'))

            link = root / 'link.txt'
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                return                          # 平台不支持符号链接：上面两条已覆盖
            self.assertIsNone(_const.RULES.within_root(root, 'link.txt'),
                              '符号链接把目标引到了根外，仍被判成根内')


class WalkRemoteTests(unittest.TestCase):
    """对端应答里的条目名：越界条目必须在进入 walked 之前被丢掉。"""

    def setUp(self):
        self._real = mesh_client.list_directory
        self.addCleanup(lambda: setattr(mesh_client, 'list_directory', self._real))

    def _patch(self, payload):
        def fake(connection, share_id, path):
            return payload if path in ('.', '') else {'dir': True, 'entries': []}
        mesh_client.list_directory = fake

    def test_malicious_entry_names_are_dropped(self):
        self._patch({'dir': True, 'entries': [
            {'name': '正常.jpg', 'dir': False, 'size': 3, 'mtime_ns': 1},
            {'name': '../../../tmp/cubby_pwn', 'dir': False, 'size': 3},
            {'name': '/tmp/cubby_abs', 'dir': False, 'size': 3},
            {'name': '..', 'dir': True},
            {'name': 'C:/evil.txt', 'dir': False, 'size': 3},
        ]})
        walked = _host(_materialize.Materialize).walk_remote(object(), 'album', '.', 4, 100)
        self.assertEqual(list(walked), ['正常.jpg'],
                         f'越界条目进入了 walked: {sorted(walked)}')

    def test_file_path_from_peer_is_validated_too(self):
        """调用方要的是个文件时，路径来自对端应答的 `path` 字段，同样要过归一。"""
        self._patch({'dir': False, 'path': '../../etc/passwd', 'size': 10})
        walked = _host(_materialize.Materialize).walk_remote(object(), 'album', '.', 4, 100)
        self.assertEqual(walked, {})


class PruneAbsentTests(unittest.TestCase):
    """对账删除：索引里的越界键不得删掉根外文件。"""

    def test_out_of_root_keys_are_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            victim = Path(tmp) / 'outside.txt'
            victim.write_text('DO NOT DELETE', encoding='utf-8')

            removed = _host(_materialize.Materialize).prune_absent(
                root, {'../outside.txt': {'dir': False}}, {})
            self.assertEqual(removed, 0)
            self.assertTrue(victim.exists(), '根外文件被对账删除删掉了')

    def test_in_root_entries_are_still_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            (root / '相册').mkdir(parents=True)
            stale = root / '相册' / 'old.jpg'
            stale.write_bytes(b'x')

            removed = _host(_materialize.Materialize).prune_absent(root, {'相册/old.jpg': {'dir': False}}, {})
            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())


class _MaterializeStub:
    """`mirror_files` 只用到的物化能力：写对端 mtime（与落盘断言无关，吞掉）。"""

    def apply_remote_mtime(self, target, mtime_ns):
        pass


class _MirrorHost(_mirror.Mirror):
    """镜像用例的宿主：路径判定走真实实现，"已达一致"固定为否（每次都写）。"""

    def __init__(self):
        self._rules = _const.RULES
        self._materialize = _MaterializeStub()

    def mirror_file_current(self, target, info):
        return False


class MirrorFilesTests(unittest.TestCase):
    """镜像写盘：越界目标必须在调用 fetch_to_file 之前被拦下。"""

    def setUp(self):
        self._real = mesh_client.fetch_to_file
        self.calls = []
        self.addCleanup(lambda: setattr(mesh_client, 'fetch_to_file', self._real))

    def _patch_fetch(self):
        def fake(connection, share_id, rel, target):
            self.calls.append(Path(target))
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(b'ATTACKER')
            return 7
        mesh_client.fetch_to_file = fake

    def test_out_of_root_target_is_never_written(self):
        self._patch_fetch()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            outside = Path(tmp) / 'outside.txt'
            outside.write_bytes(b'ORIGINAL')

            result = _MirrorHost().mirror_files(
                object(), 'album', root,
                {'../outside.txt': {'dir': False, 'size': 7}}, 0)

            self.assertEqual(self.calls, [], '越界目标被交给了 fetch_to_file')
            self.assertEqual(outside.read_bytes(), b'ORIGINAL')
            self.assertEqual(result['fetched'], 0)
            self.assertTrue(result['error_count'] >= 1, result)

    def test_in_root_target_is_still_written(self):
        self._patch_fetch()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            result = _MirrorHost().mirror_files(
                object(), 'album', root, {'相册/新.jpg': {'dir': False, 'size': 7}}, 0)
            self.assertEqual(result['fetched'], 1, result)
            self.assertEqual((root / '相册' / '新.jpg').read_bytes(), b'ATTACKER')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
