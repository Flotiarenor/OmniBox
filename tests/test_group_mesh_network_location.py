"""「网络位置」后端：把共享项完整取到用户指定目录（而不是留占位）。

镜像目标是一个普通本地文件夹，消费方按本地文件工作、**没有** `ensure_file` 钩子可
依赖，所以这里的每一条（整取、幂等增量、目录边界）都是"网络位置"能不能用的前提。

运行：
    venv/Scripts/python -m unittest tests.test_group_mesh_network_location -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.harness.app_instance import MeshCluster, boot_prerequisites
from tests.harness.support import cleanup_tree, write_shared

_BOOT_OK, _BOOT_REASON = boot_prerequisites()
@unittest.skipUnless(_BOOT_OK, f'本机无法启动应用实例（{_BOOT_REASON}）')
class NetworkLocationTest(unittest.TestCase):
    """「网络位置」后端：把共享项**完整取到用户指定目录**，而不是留占位。

    这条区别是"网络位置"能不能用的关键：镜像目标是一个普通本地文件夹，消费方
    （image-viewer 等）按本地文件工作、**没有** `ensure_file` 钩子可依赖 ——
    留 0 字节占位就是宽高 0×0 与整片 404 缩略图。
    """

    SHARE_ID = 'mirror'
    MTIME_NS = 1_577_836_800_000_000_000          # 2020-01-01T00:00:00Z

    @classmethod
    def setUpClass(cls):
        cls._root = Path(tempfile.mkdtemp(prefix='omnibox-mirror-'))
        cls.addClassCleanup(cleanup_tree, cls._root)
        cls.shared = cls._root / 'shared'
        cls.files = {
            '相册/宽幅.jpg': bytes(range(256)) * 4,
            '相册/竖幅.jpg': bytes(reversed(range(256))) * 8,
            '说明.txt': b'mirror me\n',
        }
        write_shared(cls.shared, cls.files)
        for name in cls.files:
            os.utime(cls.shared / name, ns=(cls.MTIME_NS, cls.MTIME_NS))

        cls.cluster = MeshCluster(cls._root / 'instances', names=('owner', 'member'))
        cls.addClassCleanup(cls.cluster.stop)
        cls.cluster.start()
        cls.cluster.form_group()
        cls.cluster.call(cls.cluster.owner, 'add_share',
                         {'share_id': cls.SHARE_ID, 'path': str(cls.shared), 'read': 'group'})
        cls.cluster.link(cls.cluster.members[0], cls.cluster.start_nodes()[0], name='owner')
        # 目标目录由"用户"提供：这里就是测试进程自己的临时目录，与实例数据根无关
        cls.destination = cls._root / 'mirror-target'
        cls.destination.mkdir()

    @property
    def consumer(self):
        return self.cluster.members[0]

    def _mirror(self, destination=None, **extra):
        return self.cluster.call(self.consumer, 'mirror_share',
                                 {'device_id': '', 'share_id': self.SHARE_ID,
                                  'destination': str(destination or self.destination), **extra})

    def test_shell_offers_the_provider_to_the_folder_picker(self):
        """壳按 placement 就能发现提供方，且提供方页面拿到壳的引导脚本。

        这是「网络位置」这条路唯一的装配契约：共享目录组件用
        `system_get_plugin_extensions(null, 'network-location')` 发现提供方，再按
        `embedUrl` 嵌 iframe。两处任一断掉，用户看到的都是"点了没反应 / 弹出一个白屏"，
        而单元测试各自都能过 —— 所以这里把「发现 → 取页面 → 页面里有 Bridge」串起来验。
        """
        found = self.consumer.system('system_get_plugin_extensions', None, 'network-location')
        provider = next((ext for ext in (found or []) if ext.get('id') == 'group-mesh'), None)
        self.assertIsNotNone(provider, f'壳没有把 group-mesh 列为网络位置提供方: {found}')

        status, body, _headers = self.consumer.get(str(provider['embedUrl']))
        self.assertEqual(status, 200, '提供方页面必须能取到')
        html = body.decode('utf-8')
        self.assertIn("Bridge.setPrefix('group-mesh')", html,
                      '子页面缺少壳注入的 Bridge —— 点开只会是白屏')
        self.assertIn('/shell/folder-picker.js', html, '提供方复用壳的目录选择器')

    def test_mirror_is_a_plain_local_folder_matching_the_peer(self):
        result = self._mirror()
        self.assertTrue(result['success'], result)
        self.assertEqual(result['error_count'], 0, result)
        for rel in self.files:
            target = self.destination / rel
            # 与**对端当前内容**比（而不是与写死的初始内容比），这样与用例执行顺序无关
            self.assertEqual(target.read_bytes(), (self.shared / rel).read_bytes(), rel)
            self.assertLess(abs(target.stat().st_mtime_ns - self.MTIME_NS), 2_000_000_000,
                            f'{rel} 的 mtime 应当写成对端的值（消费方缓存失效靠它）')

    def test_second_run_only_fetches_what_changed(self):
        self._mirror()
        settled = self._mirror()
        self.assertEqual(settled['fetched'], 0, f'内容没变就不该重传: {settled}')
        self.assertEqual(settled['unchanged'], len(self.files))

        # 对端换掉一个文件（等长内容 + 明显不同的 mtime）
        changed = self.shared / '相册/宽幅.jpg'
        replacement = bytes(reversed(range(256))) * 4
        changed.write_bytes(replacement)
        new_mtime = self.MTIME_NS + 86_400_000_000_000
        os.utime(changed, ns=(new_mtime, new_mtime))

        again = self._mirror()
        self.assertEqual(again['fetched'], 1, f'应当只重取那一个文件: {again}')
        self.assertEqual(again['unchanged'], len(self.files) - 1)
        self.assertEqual((self.destination / '相册/宽幅.jpg').read_bytes(), replacement)

    def test_creates_the_leaf_directory_but_not_a_missing_parent(self):
        """目标目录不存在时的两条边界：补建最后一级，但绝不在缺失的上级下乱造。"""
        def raw(destination):
            return self.consumer.call('group-mesh', 'mirror_share',
                                      {'device_id': '', 'share_id': self.SHARE_ID,
                                       'destination': str(destination)})

        # 上级存在、叶子不存在 → 建出来并取回（用户"放到图库下这个新文件夹"是常见意图）
        leaf = self._root / 'parent-exists' / '新相册'
        leaf.parent.mkdir()
        result = raw(leaf)
        self.assertTrue(result['success'], result)
        self.assertTrue(leaf.is_dir(), '最后一级目录应当被创建')
        self.assertEqual((leaf / '说明.txt').read_bytes(), self.files['说明.txt'])

        # 上级也不存在 → 拒绝，并指出缺的是哪一级（挡住"粘贴丢了分隔符"这类路径）
        orphan = self._root / 'no-such-parent' / 'x' / 'y'
        result = raw(orphan)
        self.assertFalse(result['success'])
        self.assertIn('上级目录', result['error'])
        self.assertFalse(orphan.exists())

        # 驱动器相对路径（`C:Users...`）不是绝对路径：必须在 resolve 之前就拒掉。
        # 【已实测】分隔符被吃光的路径就是这样进来的，resolve 之后会落在盘符根上。
        if os.name == 'nt':
            result = raw(r'C:UsersADMINI~1AppDataLocalTempnot-a-real-dir')
            self.assertFalse(result['success'])
            self.assertIn('绝对路径', result['error'])

        # 程序数据与身份目录之内一律拒绝（与 add_share 同一套边界）
        result = raw(Path(self.consumer.data_root) / 'group-mesh' / 'x')
        self.assertFalse(result['success'])
        self.assertIn('不得位于', result['error'])



if __name__ == '__main__':
    unittest.main()
