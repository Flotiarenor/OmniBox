"""物化目录的变更语义：占位、对端 mtime、对账失效、暂存取字节。

这些行为单进程桩测不了 —— 它们全都要求"对端真的改了文件"，那正是多实例夹具存在的
意义。`PlaceholderAgainstImageViewerTest` 是同一件事的另一面：把物化目录挂成
image-viewer 的额外图片目录之后，界面到底会怎样。

运行：
    venv/Scripts/python -m unittest tests.test_group_mesh_materialize -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.harness.app_instance import MeshCluster, boot_prerequisites
from tests.harness.support import cache_root_of, cleanup_tree, same_path, write_shared

_BOOT_OK, _BOOT_REASON = boot_prerequisites()
@unittest.skipUnless(_BOOT_OK, f'本机无法启动应用实例（{_BOOT_REASON}）')
class MaterializedDirectoryTest(unittest.TestCase):
    """物化目录的变更语义：mtime 取自对端、同大小替换会失效、对端删了本地也删。

    这些行为用单进程桩测不了 —— 它们全都要求"对端真的改了文件"，而那正是多实例
    夹具存在的意义。三个文件各管一条断言，互不干扰。
    """

    SHARE_ID = 'album'
    MUTATED = '同大小替换.bin'
    VANISHED = '将被删除.bin'
    STAMPED = '时间戳.bin'
    FETCHED = '取回.bin'
    STAMPED_MTIME_NS = 1_577_836_800_000_000_000          # 2020-01-01T00:00:00Z
    FETCHED_MTIME_NS = 1_577_840_400_000_000_000          # 2020-01-01T01:00:00Z
    # 文件系统粒度差异（FAT/exFAT 的 mtime 只有 2 秒）用一个窗口吸收；
    # 逐 ns 相等的要求只对"两端报的值"成立，对"本地存下的值"不成立。
    MTIME_WINDOW_NS = 2_000_000_000

    @classmethod
    def setUpClass(cls):
        cls._root = Path(tempfile.mkdtemp(prefix='omnibox-materialize-'))
        cls.addClassCleanup(cleanup_tree, cls._root)

        cls.original = bytes(range(256)) * 4
        cls.replacement = bytes(reversed(range(256))) * 4      # 与 original **等长**
        cls.shared = cls._root / 'shared'
        # 每个文件只服务一条断言：互相之间不共享"取回过/被删掉"这类状态，
        # 用例顺序就影响不到结果（踩过一次：一个用例取回之后，另一个用例的
        # "物化阶段应当是 0 字节占位"直接失败）。
        write_shared(cls.shared, {
            cls.MUTATED: cls.original,
            cls.VANISHED: b'to be deleted\n',
            cls.STAMPED: b'stamp\n',
            cls.FETCHED: b'fetched content\n',
        })
        for name, mtime_ns in ((cls.STAMPED, cls.STAMPED_MTIME_NS),
                               (cls.FETCHED, cls.FETCHED_MTIME_NS)):
            os.utime(cls.shared / name, ns=(mtime_ns, mtime_ns))

        cls.cluster = MeshCluster(cls._root / 'instances', names=('owner', 'member'))
        cls.addClassCleanup(cls.cluster.stop)
        cls.cluster.start()
        cls.cluster.form_group()
        cls.cluster.call(cls.cluster.owner, 'add_share',
                         {'share_id': cls.SHARE_ID, 'path': str(cls.shared), 'read': 'group'})
        cls.cluster.link(cls.cluster.members[0], cls.cluster.start_nodes()[0], name='owner')
        listed = cls.cluster.members[0].call('group-mesh', 'list_remote', {'device_id': ''})
        cls.peer_id = listed['peer_device_id']
        cls.cache_root = cache_root_of(cls.cluster.members[0], cls.peer_id, cls.SHARE_ID)

    @property
    def consumer(self):
        return self.cluster.members[0]

    def _materialize(self) -> dict:
        """重新物化整个共享项（对账、mtime 保真都发生在这里）。"""
        return self.cluster.call(self.consumer, 'materialize_remote',
                                 {'device_id': '', 'share_id': self.SHARE_ID})

    def _fetch(self, name: str):
        """按 `/file` 取一个物化条目（会触发按需取字节）。"""
        return self.consumer.get(self.consumer.file_url(self.cache_root / name), timeout=90.0)

    def test_placeholder_mtime_comes_from_peer(self):
        """占位文件的 mtime 必须是对端的值，不能是"物化时刻"。

        消费方（image-viewer / media-player）的缓存失效读的是**本地**文件系统元数据：
        占位文件若留着物化时刻的时间，等于告诉它们"对端从来没变过"。
        """
        self._materialize()
        stamped = self.cache_root / self.STAMPED
        self.assertEqual(stamped.stat().st_size, 0, '物化阶段应当是 0 字节占位')
        self.assertLess(abs(stamped.stat().st_mtime_ns - self.STAMPED_MTIME_NS),
                        self.MTIME_WINDOW_NS,
                        '占位文件的 mtime 不是对端报的值')
        self.assertGreater(time.time() * 1e9 - stamped.stat().st_mtime_ns,
                           10 * 24 * 3600 * 10**9,
                           '占位文件的 mtime 看起来是"刚刚物化"的时间，不是对端的')

    def test_index_records_peer_mtime_ns(self):
        """索引里存的是对端报的纳秒整数 —— 下次物化靠它判"换了没有"。"""
        self._materialize()
        index_file = self.cache_root.parent / f'{self.SHARE_ID}.index.json'
        index = json.loads(index_file.read_text(encoding='utf-8'))
        meta = index['entries'][self.STAMPED]
        self.assertEqual(meta['mtime_ns'], self.STAMPED_MTIME_NS)
        self.assertEqual(meta['size'], len(b'stamp\n'))
        self.assertIsInstance(meta['mtime_ns'], int)

    def test_same_size_replacement_invalidates_local_bytes(self):
        """等长替换必须被发现：这是"只比大小"永远做不到的那一半。

        判定用的是**对端报过的 mtime** 与**对端现在报的 mtime**。两者都来自同一个
        栈，因此可以精确比较；一旦拿本地 stat 去比（文件系统会量化 os.utime 写下的
        ns），就会永远不相等、每次读都重新取回。
        """
        self._materialize()
        status, body, _headers = self._fetch(self.MUTATED)
        self.assertEqual((status, body), (200, self.original), '前置条件：先取回原始内容')

        # 对端换成等长的另一份内容，并把 mtime 拨到明显不同的值
        target = self.shared / self.MUTATED
        target.write_bytes(self.replacement)
        new_mtime_ns = self.STAMPED_MTIME_NS + 86_400_000_000_000
        os.utime(target, ns=(new_mtime_ns, new_mtime_ns))

        result = self._materialize()
        self.assertEqual(result['invalidated'], 1, f'应当丢弃 1 份过期字节: {result}')
        self.assertEqual((self.cache_root / self.MUTATED).stat().st_size, 0,
                         '过期的真字节必须被丢掉，退回 0 字节占位')

        status, body, _headers = self._fetch(self.MUTATED)
        self.assertEqual((status, body), (200, self.replacement),
                         '取回的应当是替换之后的内容')

    def test_peer_deletion_removes_the_local_entry(self):
        """对端删掉的文件，本地物化目录里也必须消失（改动前只增不减）。"""
        self._materialize()
        local = self.cache_root / self.VANISHED
        self.assertTrue(local.exists(), '前置条件：先物化出来')
        status, _body, _headers = self._fetch(self.VANISHED)
        self.assertEqual(status, 200, '前置条件：它本来可以按需取回')

        (self.shared / self.VANISHED).unlink()

        result = self._materialize()
        self.assertEqual(result['removed'], 1, f'应当删掉 1 条本地条目: {result}')
        self.assertFalse(local.exists(), '对端已删除的文件不该留在本地物化目录里')
        status, _body, _headers = self._fetch(self.VANISHED)
        self.assertEqual(status, 404, '本地删掉之后也不该再从对端"复活"')

    def test_lazy_fetch_uses_staging_and_leaves_no_trace(self):
        """按需取字节走 `.cache/staging`：不污染下载目录、成功不留暂存文件，
        且取回的真字节带的是**对端**的 mtime（不是取回时刻）。"""
        self._materialize()
        group_mesh_data = Path(self.consumer.data_root) / 'group-mesh'
        downloads = group_mesh_data / 'downloads'
        staging = group_mesh_data / '.cache' / 'staging'
        before = sorted(p.name for p in downloads.rglob('*')) if downloads.exists() else []

        status, body, _headers = self._fetch(self.FETCHED)
        self.assertEqual((status, body), (200, b'fetched content\n'), '前提：按需取字节要成功')

        after = sorted(p.name for p in downloads.rglob('*')) if downloads.exists() else []
        self.assertEqual(after, before, '按需取字节不该在用户下载目录里留下文件')
        leftovers = [p.name for p in staging.rglob('*') if p.is_file()] if staging.exists() else []
        self.assertEqual(leftovers, [], '成功的取回应当把暂存文件 os.replace 走，不留 .part')

        fetched = self.cache_root / self.FETCHED
        self.assertEqual(fetched.stat().st_size, len(b'fetched content\n'))
        self.assertLess(abs(fetched.stat().st_mtime_ns - self.FETCHED_MTIME_NS),
                        self.MTIME_WINDOW_NS,
                        '取回的真字节也必须写成对端的 mtime（否则消费方看不出内容换过）')

@unittest.skipUnless(_BOOT_OK, f'本机无法启动应用实例（{_BOOT_REASON}）')
class PlaceholderAgainstImageViewerTest(unittest.TestCase):
    """把物化目录挂成 image-viewer 的额外图片目录，看到底会发生什么。

    这是全篇设计里唯一一条只有**读码推断**的结论：0 字节占位落到"会解码源文件"的
    消费方手里会怎样。它决定了一个方向性问题 —— 到底是"补一层根/依赖路由"就够，
    还是必须让字节先落到本地。所以必须有实测，而且要有**正反对照**：

    * 0 字节占位 → 宽高 0×0、缩略图不可用（实测是 404，不是"200 + 0 字节"）；
    * 真字节到位 → 宽高正确、`/thumbs` 返回真缩略图。

    没有后半段，前半段的"0 字节"可能只是环境问题；没有前半段，后半段证明不了任何事。
    实测同时纠正了两处读码推断。
    """

    SHARE_ID = 'album'
    PHOTO = 'pic.jpg'
    SIZE = (64, 48)

    @classmethod
    def setUpClass(cls):
        # PIL 只在方法内 import：放在模块顶层的话，缺 Pillow 的机器上会在**收集阶段**
        # ImportError（error），而不是按设计 skip。
        from PIL import Image

        cls._root = Path(tempfile.mkdtemp(prefix='omnibox-viewer-'))
        cls.addClassCleanup(cleanup_tree, cls._root)

        cls.shared = cls._root / 'shared'
        cls.shared.mkdir(parents=True)
        Image.new('RGB', cls.SIZE, (200, 30, 30)).save(cls.shared / cls.PHOTO, 'JPEG')

        cls.cluster = MeshCluster(cls._root / 'instances', names=('owner', 'member'))
        cls.addClassCleanup(cls.cluster.stop)
        cls.cluster.start()
        cls.cluster.form_group()
        cls.cluster.call(cls.cluster.owner, 'add_share',
                         {'share_id': cls.SHARE_ID, 'path': str(cls.shared), 'read': 'group'})
        cls.cluster.link(cls.cluster.members[0], cls.cluster.start_nodes()[0], name='owner')
        listed = cls.cluster.members[0].call('group-mesh', 'list_remote', {'device_id': ''})
        cls.cache_root = cache_root_of(cls.cluster.members[0], listed['peer_device_id'],
                                        cls.SHARE_ID)

        # 必须先物化再登记额外根：`_extra_roots()` 每次调用都重算，并且要求目录
        # 当时就存在 —— 目录还不存在时这个根会被**静默忽略**（第一版就踩了：夹具
        # 里先登记后物化，于是 list_roots 里根本找不到它）。
        cls.cluster.call(cls.cluster.members[0], 'materialize_remote',
                         {'device_id': '', 'share_id': cls.SHARE_ID})

        # 通过插件自己的设置 API 把物化目录加成额外图片目录（不重启实例）：
        # `save_folder_settings` 接受"只传一个 dict"的形态 → 存为全局设置并触发
        # on_settings_changed，之后 `_roots()` 就会带上它。
        saved = cls.cluster.members[0].call('image-viewer', 'save_settings',
                                           {'extra_roots': str(cls.cache_root)})
        assert saved.get('success'), saved
        roots = cls.cluster.members[0].call('image-viewer', 'list_roots')
        entry = next(r for r in roots if same_path(r['path'], cls.cache_root))
        # `list_roots` 给的是**裸 token**，而虚拟路径要带命名空间标记（`__`）：
        # `_split_virtual` 只把 `__<token>` 当命名空间，裸 token 会被当成第一根下的
        # 普通目录（于是扫到 0 张图，而且不报错）。
        cls.namespace = f'__{entry["namespace"]}'
        cls.virtual_photo = f'{cls.namespace}/{cls.PHOTO}'

    @property
    def consumer(self):
        return self.cluster.members[0]

    def _local_photo(self) -> Path:
        return self.cache_root / self.PHOTO

    def _ensure_placeholder(self) -> None:
        """把本地这份恢复成 0 字节占位，并清掉会**掩盖**它的缓存（全用真代码）。

        为什么必须显式清缓存：占位文件的 mtime 被刻意写成**对端的值**，与真字节那份
        完全一样，因此按 (路径, mtime) 键控的尺寸/缩略图缓存**分不出这两者**。
        现实里走到占位只有一条路——对账发现对端 mtime 变了 → 丢掉本地字节 → 新 mtime
        让旧缓存自然失效；本方法是人为造出"占位"这个状态而没有让对端变化，
        所以要用插件自己的 `regenerate_thumbs`（删缩略图行 + 丢尺寸缓存）把它们清掉，
        否则测的是缓存而不是占位。
        """
        local = self._local_photo()
        local.unlink(missing_ok=True)
        self.cluster.call(self.consumer, 'materialize_remote',
                          {'device_id': '', 'share_id': self.SHARE_ID})
        self.consumer.call('image-viewer', 'regenerate_thumbs',
                           rel_paths=[self.virtual_photo])
        self.consumer.call('image-viewer', 'refresh')
        self.assertEqual(local.stat().st_size, 0, '前置条件：本地应当是 0 字节占位')

    def _fetch_real_bytes(self) -> bytes:
        """经 group-mesh 自己的根把真字节取到本地（插件根内的路径，403 不会出现）。"""
        status, body, _headers = self.consumer.get(
            self.consumer.file_url(self._local_photo()), timeout=90.0)
        self.assertEqual(status, 200, '前提：占位文件应当能按需取回真字节')
        return body

    def _thumb(self):
        """走真实 `/thumbs` 路由取缩略图（`Bridge.thumbUrl` 的同一形状）。"""
        return self.consumer.get(f'/thumbs/{self.virtual_photo}?plugin=image-viewer')

    def test_zero_byte_placeholder_yields_no_dimensions_and_no_thumbnail(self):
        """0 字节占位：宽高是 0×0，`/thumbs` 回 0 字节，并在 thumb_dir 里留下垃圾。

        三条断言对应三处独立后果 —— 布局退化、缩略图不可用、以及一个会一直躺在
        `thumb_dir` 里的 0 字节文件。
        """
        self._ensure_placeholder()

        listing = self.consumer.call('image-viewer', 'list_images', rel_path=self.namespace)
        images = listing.get('images') or []
        self.assertEqual(len(images), 1, f'额外根里应当只看到那一张: {listing}')
        self.assertEqual((images[0]['width'], images[0]['height']), (0, 0),
                         'Pillow 打不开 0 字节文件，宽高只能是 0×0（瀑布流布局因此退化）')

        status, _body, _headers = self._thumb()
        self.assertEqual(status, 404,
                         '源是 0 字节时缩略图不可用：Pillow 打不开，而散文件兜底写的是 '
                         'thumb_dir/<根内路径>、路由查的是 thumb_dir/<虚拟路径>，两者对不上')

        # 兜底分支的副作用：`ensure_thumbnail()` 在 Pillow 失败后 `shutil.copy` 源文件，
        # 目的路径是 `thumb_dir / <根内相对路径>`（**不带** `__命名空间` 前缀）。
        # 所以它在 `thumb_dir` 里留一个 0 字节文件，且这个路径与第一根的同名图片
        # 共用 —— 是一处潜在的跨根覆盖，本文档只记录、未修（修它会动 image-viewer
        # 的散文件布局，超出本次物化改动的范围）。
        stray = Path(self.consumer.data_root) / '.cache' / 'thumbs' / self.PHOTO
        self.assertTrue(stray.is_file(), '散文件兜底应当留下一个未加命名空间前缀的文件')
        self.assertEqual(stray.stat().st_size, 0)

    def test_fetched_bytes_restore_dimensions_and_thumbnail(self):
        """真字节到位 + 刷新之后，宽高与缩略图都恢复正常（失败是可自愈的）。

        这条同时纠正一个更悲观的判断：image-viewer 的 `/thumbs` 是**先**问
        `get_thumb_data()`（SQLite 缩略图库）再看 `thumb_dir` 散文件，所以那份 0 字节
        垃圾会被遮住，不会永久劫持缩略图。真正致命的只是"第一次"：宽高 0×0 与 0 字节
        缩略图 —— 而这已经足够让这一屏不可用。
        """
        from PIL import Image

        body = self._fetch_real_bytes()
        with Image.open(io.BytesIO(body)) as image:
            self.assertEqual(image.size, self.SIZE, '取回的必须是那张真图')
        self.consumer.call('image-viewer', 'refresh')

        listing = self.consumer.call('image-viewer', 'list_images', rel_path=self.namespace)
        images = listing.get('images') or []
        self.assertEqual((images[0]['width'], images[0]['height']), self.SIZE,
                         '真字节到位后宽高应当正确')

        status, thumb, _headers = self._thumb()
        self.assertEqual(status, 200)
        self.assertGreater(len(thumb), 0, '真字节到位后缩略图应当有内容')
        with Image.open(io.BytesIO(thumb)) as image:
            self.assertLessEqual(max(image.size), 300, '缩略图应当被缩放到 300 以内')


if __name__ == '__main__':
    unittest.main()
