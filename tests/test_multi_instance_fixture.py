"""多实例夹具本身的用例：实例隔离、跨实例握手/取字节、停机不泄漏。

`tests/harness/app_instance.py` 让同一台机器上能起多台**空白**实例（各自一份
`OMNIBOX_HOME`），于是"两台设备互相发现、按需取字节"这条链在 CI 与本机都能验，
不必再搬一台 Linux 机器并手工同步代码。

运行：
    venv/Scripts/python -m unittest tests.test_multi_instance_fixture -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.harness.app_instance import (
    PORT_RELEASE_TIMEOUT,
    AppInstance,
    MeshCluster,
    boot_prerequisites,
    wait_until,
)
from tests.harness.support import (
    cleanup_tree,
    port_is_free,
    same_path,
)

_BOOT_OK, _BOOT_REASON = boot_prerequisites()
@unittest.skipUnless(_BOOT_OK, f'本机无法启动应用实例（{_BOOT_REASON}）')
class InstanceShutdownTest(unittest.TestCase):
    """停掉实例之后**不能留下孤儿进程**（端口释放、目录可删）。

    这条守卫抓到过一个真实泄漏，而且是我自己夹具的 bug：Windows 上
    `venv\\Scripts\\python.exe` 是启动器，会再起一个真正的解释器去跑 `main.py`，
    只 `terminate()` 启动器会留下孤儿服务继续占着端口、SQLite 与日志文件 ——
    症状是临时目录删不掉（实测一次演示后留下 17 个删不掉的目录），
    而我最初把它当成"杀毒软件短暂占用文件"，给清理加了重试，那只是盖住了症状。
    """

    def test_stop_releases_the_port_and_leaves_a_deletable_directory(self):
        root = Path(tempfile.mkdtemp(prefix='omnibox-shutdown-'))
        self.addClassCleanup(cleanup_tree, root)
        instance = AppInstance('solo', root)
        instance.start()
        port = instance.port

        instance.stop()

        self.assertFalse(instance.alive, 'stop() 之后实例仍标记为存活')
        self.assertTrue(wait_until(lambda: port_is_free(port), PORT_RELEASE_TIMEOUT),
                        f'端口 {port} 仍被占用：有孤儿实例活着（只杀了启动器？）')
        # 孤儿会把 `.config/logs/omnibox.log` 与 SQLite 握在手里，于是整个目录删不掉。
        # 这就是"有孤儿"最直接的用户可见后果，也是夹具清理路径上真正要保证的事。
        cleanup_tree(root, attempts=3)
        self.assertFalse(root.exists(), f'实例目录删不掉，多半有孤儿进程握着里面的文件: {root}')


@unittest.skipUnless(_BOOT_OK, f'本机无法启动应用实例（{_BOOT_REASON}）')
class MultiInstanceMeshTest(unittest.TestCase):
    """两台设备（群主 + 成员）在同一台机器上跑通共享与按需取字节。"""

    SHARE_ID = 'photos'
    DROP_ID = 'dropbox'
    # 文件名刻意带中文与空格：这条链会经过 JSON、HTTP、内核的相对路径解析、
    # download_remote 的目标路径拼接与 os.replace —— 用纯 ASCII 名字测不出问题。
    # 标注 ClassVar 是必须的：它是类级常量，不是每个实例各自持有的可变状态。
    FILES: ClassVar[Dict[str, bytes]] = {
        '顶层.bin': bytes(range(256)) * 8,
        '相册/照片 1.bin': bytes(reversed(range(256))) * 16,
    }

    @classmethod
    def setUpClass(cls):
        # 用 mkdtemp 而不是 TemporaryDirectory：后者的清理在 GC 时还会再跑一次，
        # 与下面"重试删除"的清理互相干扰；这里只留一份清理逻辑。
        cls._root = Path(tempfile.mkdtemp(prefix='omnibox-mesh-'))
        cls.addClassCleanup(cleanup_tree, cls._root)
        root = cls._root

        # 共享根必须在实例数据根**之外**：add_share 会拒绝数据根与身份目录之内的
        # 路径（§6.5 的硬约束），放在 data_root 下面会直接失败。
        cls.shared = root / 'shared'
        for rel, payload in cls.FILES.items():
            target = cls.shared / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

        cls.cluster = MeshCluster(root / 'instances', names=('owner', 'member'))
        # 先登记清理再启动：启动失败时也要把已经起来的子进程收掉。
        cls.addClassCleanup(cls.cluster.stop)
        cls.cluster.start()
        cls.cluster.form_group()
        cls.cluster.call(cls.cluster.owner, 'add_share',
                         {'share_id': cls.SHARE_ID, 'path': str(cls.shared), 'read': 'group'})
        # 第二个共享项专门给上传用：`photos` 的 write 是 owner（成员只能读），
        # 上传必须有对端明确授予的写权限。
        cls.drop = root / 'dropbox'
        cls.drop.mkdir()
        cls.cluster.call(cls.cluster.owner, 'add_share',
                         {'share_id': cls.DROP_ID, 'path': str(cls.drop),
                          'read': 'group', 'write': 'group'})
        endpoints = cls.cluster.start_nodes()
        cls.owner_endpoint = endpoints[0]
        cls.cluster.link(cls.cluster.members[0], cls.owner_endpoint, name='owner')

    @property
    def consumer(self):
        return self.cluster.members[0]

    def _remote_listing(self) -> dict:
        """消费者按地址发现群主（`device_id` 留空：引导期只有地址）。"""
        result = self.consumer.call('group-mesh', 'list_remote', {'device_id': ''})
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get('success'), f'list_remote 失败: {result}')
        return result

    def _cache_root(self, peer_id: str) -> Path:
        """物化根 = `<消费者数据根>/group-mesh/.cache/remote/<真实设备ID>/<共享标识>`。"""
        return (Path(self.consumer.data_root) / 'group-mesh' / '.cache'
                / 'remote' / peer_id / self.SHARE_ID)

    def test_instances_are_isolated_devices(self):
        """实例隔离：两份数据目录、两个令牌、两把设备密钥。"""
        owner, member = self.cluster.owner, self.consumer
        self.assertNotEqual(owner.user_data, member.user_data)
        self.assertTrue(owner.data_root.is_dir(), '群主的数据根应当已创建')
        self.assertTrue(member.data_root.is_dir(), '成员的数据根应当已创建')
        self.assertNotEqual(owner.token, member.token,
                            '令牌必须按实例隔离（同一份 .config 会共用令牌文件）')

        owner_device = self.cluster.call(owner, 'get_device_keys')['device']
        member_device = self.cluster.call(member, 'get_device_keys')['device']
        self.assertNotEqual(owner_device, member_device, '两个实例应当是两台不同的设备')

        # 环境变量不只是"文件落在别处"，它必须真的改到应用自己的配置上：
        # 默认的 `./data` 会被锚定到本实例的用户数据目录（resolve_data_root 的语义）。
        config = member.system('system_get_config')
        self.assertTrue(same_path(config['directories']['data_root'], member.data_root),
                        f"数据根没有锚定到实例目录: {config['directories']['data_root']}")

    def test_explicit_data_root_argument_is_honored(self):
        """`--data-root` 覆盖生效，且相对路径仍锚定到本实例的用户数据目录。

        单独起第三个实例而不是复用集群：这条断言改的是数据根，混进集群会让
        "共享根必须在数据根之外"那条前提变得难以推理。
        """
        extra_root = self._root / 'instances'
        with AppInstance('datasplit', extra_root,
                         extra_args=['--data-root', 'custom-data']) as instance:
            config = instance.system('system_get_config')
            expected = Path(instance.user_data) / 'custom-data'
            self.assertTrue(same_path(config['directories']['data_root'], expected),
                            f"数据根没有被 --data-root 覆盖: {config['directories']['data_root']}")
            self.assertTrue(expected.is_dir(), '入口应当创建被覆盖的数据根')

    def test_consumer_discovers_owner_shares(self):
        """成员经手工对端发现群主的共享项，并拿到握手里的真实设备 ID。"""
        listed = self._remote_listing()
        share_ids = [item.get('share_id') for item in listed.get('shares') or []]
        self.assertIn(self.SHARE_ID, share_ids, f'看不到共享项: {listed}')
        self.assertTrue(listed.get('peer_device_id'), '应答必须带对端真实设备 ID')

    def test_lazy_materialize_then_fetch_real_bytes(self):
        """物化只落占位，字节在第一次读取时取回，且与对端口径一致。"""
        listed = self._remote_listing()
        peer_id = listed['peer_device_id']
        cache_root = self._cache_root(peer_id)
        cache_file = cache_root / '相册' / '照片 1.bin'
        payload = self.FILES['相册/照片 1.bin']

        # 1) 物化之前：本地没有这个文件，`/file` 是 404 而不是 200 + 空
        self.assertFalse(cache_file.exists(), '物化之前本地不该有占位文件')
        status, _body, _headers = self.consumer.get(self.consumer.file_url(cache_file))
        self.assertEqual(status, 404)

        # 2) 物化：目录结构与 0 字节占位落地，字节一个都不搬
        materialized = self.cluster.call(self.consumer, 'materialize_remote',
                                        {'device_id': '', 'share_id': self.SHARE_ID})
        self.assertTrue(same_path(materialized['root'], cache_root),
                        '物化根必须用握手得到的真实设备 ID')
        self.assertTrue((cache_root / '相册').is_dir(), '子目录应当被真的建出来')
        self.assertTrue((cache_root / '顶层.bin').exists(), '顶层文件应当有占位')
        self.assertEqual(cache_file.stat().st_size, 0, '物化阶段文件必须是 0 字节占位')

        # 3) 第一次真读：`/file` 走 is_content_placeholder → ensure_file 取回真字节
        status, body, _headers = self.consumer.get(self.consumer.file_url(cache_file), timeout=90.0)
        self.assertEqual(status, 200, f'按需取字节失败，本地大小 {cache_file.stat().st_size}')
        self.assertEqual(body, payload, '取回的字节与对端不一致')
        self.assertEqual(cache_file.stat().st_size, len(payload), '取回后本地大小应当与对端一致')

        # 4) 第二次读：已落位，结果一致
        status, body, _headers = self.consumer.get(self.consumer.file_url(cache_file))
        self.assertEqual((status, body), (200, payload))

        # 5) 同一个共享项里的顶层文件也应当能按需取回
        top = cache_root / '顶层.bin'
        status, body, _headers = self.consumer.get(self.consumer.file_url(top), timeout=90.0)
        self.assertEqual((status, body), (200, self.FILES['顶层.bin']))

    def test_paths_outside_the_plugin_roots_stay_forbidden(self):
        """作用域校验没有被这条链放松：数据根之外的路径一律 403。"""
        outside = self.shared / '顶层.bin'
        self.assertTrue(outside.is_file(), '前置条件：这个文件确实存在于插件根之外')
        status, _body, _headers = self.consumer.get(self.consumer.file_url(outside))
        self.assertEqual(status, 403)

    # ── 上传：成员 → 群主 ──────────────────────────────────────────────────
    #
    # 上传是异步的（upload_remote 返回 task_id），因此这一组统一用 `_run_upload`
    # 起任务 + 等终态。

    def _upload(self, local: Path, remote: str, share_id: str = '', **extra):
        payload = {'device_id': '', 'share_id': share_id or self.DROP_ID,
                   'local_path': str(local), 'remote_path': remote}
        payload.update(extra)
        # 走 instance.call 而不是 cluster.call：后者会断言 success，而这一组里
        # "被对端拒绝"正是要断言的正常结果。
        return self.consumer.call('group-mesh', 'upload_remote', payload)

    def _task(self, task_id: str) -> dict:
        result = self.consumer.call('group-mesh', 'upload_status', {'task_id': task_id})
        return (result or {}).get('task') or {}

    def _wait_upload(self, task_id: str, timeout: float = 90.0) -> dict:
        deadline = time.monotonic() + timeout
        task: dict = {}
        while time.monotonic() < deadline:
            task = self._task(task_id)
            if task.get('state') in ('done', 'failed', 'cancelled'):
                return task
            time.sleep(0.05)
        self.fail(f'上传任务 {task_id} 在 {timeout}s 内没有结束: {task}')
        return task          # 上一行必抛；写在这里是为了让静态检查看到返回路径

    def _run_upload(self, local: Path, remote: str, share_id: str = '', **extra) -> dict:
        started = self._upload(local, remote, share_id, **extra)
        self.assertTrue(started.get('success'), f'启动上传失败: {started}')
        return self._wait_upload(started['task_id'])

    def _source(self, name: str, payload: bytes) -> Path:
        """在本机（成员实例）造一个待上传的文件。

        名字带中文与空格：这条链会经过 HTTP JSON、内核的相对路径解析、
        对端的目录创建与 `os.replace`，纯 ASCII 名字测不出编码问题。
        """
        path = Path(self.consumer.user_data) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def _record_file(self) -> Path:
        """成员实例的续传记录（`<数据根>/group-mesh/uploads.json`）。"""
        return Path(self.consumer.data_root) / 'group-mesh' / 'uploads.json'

    def test_member_uploads_file_into_owner_share(self):
        payload = bytes(range(256)) * 40          # 10 KB，跨多个分块
        source = self._source('要上传 的文件.bin', payload)
        task = self._run_upload(source, '子目录/上传 结果.bin')
        self.assertEqual(task['state'], 'done', f'上传失败: {task}')
        self.assertEqual(task['sent'], len(payload))
        self.assertEqual(task['percent'], 100)
        self.assertEqual(task['resumed_from'], 0, '首次上传不该报续传')
        self.assertEqual(task['remote_path'], '子目录/上传 结果.bin')

        landed = self.drop / '子目录' / '上传 结果.bin'
        self.assertTrue(landed.is_file(), '对端没有落盘')
        self.assertEqual(landed.read_bytes(), payload, '对端收到的字节不一致')
        self.assertFalse((self.drop / '子目录' / '上传 结果.bin.part').exists(),
                         '提交之后不该留下 .part')
        self.assertNotIn('子目录/上传 结果.bin',
                         self._record_file().read_text(encoding='utf-8')
                         if self._record_file().is_file() else '',
                         '上传成功后不该留下续传记录')

    def test_upload_respects_the_acl_of_the_owner_share(self):
        """`photos` 只给了读权限：上传必须被对端拒绝，且不得落盘。"""
        payload = b'nope' * 8
        source = self._source('无权限.bin', payload)
        task = self._run_upload(source, 'nope.bin', share_id=self.SHARE_ID)
        self.assertEqual(task['state'], 'failed', f'没有写权限却上传成功了: {task}')
        self.assertIn('权限', task.get('error') or '')
        self.assertFalse((self.shared / 'nope.bin').exists())

    def test_upload_refuses_to_clobber_unless_asked(self):
        source = self._source('覆盖.bin', b'first version')
        first = self._run_upload(source, 'dup.bin')
        self.assertEqual(first['state'], 'done', f'首次上传失败: {first}')

        source.write_bytes(b'second version')
        again = self._run_upload(source, 'dup.bin')
        self.assertEqual(again['state'], 'failed', f'同名上传没有被拒: {again}')
        self.assertIn('已存在', again.get('error') or '')
        self.assertEqual((self.drop / 'dup.bin').read_bytes(), b'first version',
                         '被拒绝的覆盖不得改动对端文件')

        replaced = self._run_upload(source, 'dup.bin', overwrite=True)
        self.assertEqual(replaced['state'], 'done', f'显式覆盖失败: {replaced}')
        self.assertEqual((self.drop / 'dup.bin').read_bytes(), b'second version')

    def test_cancel_then_resume_finishes_the_file(self):
        """取消 → 已传字节留在对端 `.part` → 再传一次接着传完。

        不依赖时序断言"取消时正好传到一半"：取消在分块边界生效，落点是 [0, total]
        里的任意一点；这里断言的是**不变量**——任务报的 `sent` 必须与对端暂存文件
        的实际大小一致，然后从那个点续传到完成、内容完整。
        """
        payload = bytes(range(256)) * 2048        # 512 KB，跨多个分块
        source = self._source('可续传.bin', payload)
        started = self._upload(source, 'resume-race.bin')
        self.assertTrue(started.get('success'), f'启动上传失败: {started}')
        task_id = started['task_id']

        cancelled = self.consumer.call('group-mesh', 'cancel_upload', {'task_id': task_id})
        self.assertTrue(cancelled.get('success'), f'取消失败: {cancelled}')
        task = self._wait_upload(task_id)
        self.assertIn(task['state'], ('cancelled', 'done'), task)

        if task['state'] == 'cancelled':
            staged = self.drop / 'resume-race.bin.part'
            on_disk = staged.stat().st_size if staged.is_file() else 0
            self.assertEqual(on_disk, task['sent'],
                             '任务报的进度与对端暂存文件大小必须一致')

        done = self._run_upload(source, 'resume-race.bin')
        self.assertEqual(done['state'], 'done', f'续传失败: {done}')
        self.assertEqual(done['sent'], len(payload))
        self.assertEqual((self.drop / 'resume-race.bin').read_bytes(), payload)
        self.assertFalse((self.drop / 'resume-race.bin.part').exists())

    def test_resume_from_a_seeded_partial_upload(self):
        """确定性续传：对端已有前 128 KiB，本机也有对应记录 → 只补后半段。"""
        payload = bytes(range(256)) * 1024        # 256 KB
        source = self._source('断点.bin', payload)
        split = 128 * 1024
        (self.drop / '断点.bin.part').write_bytes(payload[:split])

        stat = source.stat()
        # 记录格式与 `_upload_key` 一致：device_id|share_id|remote_path|local_path。
        # device_id 为空是这一组的常态（用"按地址登记"引导，还没学到设备 ID）。
        key = '|'.join(('', self.DROP_ID, '断点.bin', str(source)))
        record_file = self._record_file()
        record_file.parent.mkdir(parents=True, exist_ok=True)
        record_file.write_text(json.dumps({
            key: {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sent': split,
                  'ts': 1700000000}}), encoding='utf-8')

        task = self._run_upload(source, '断点.bin')
        self.assertEqual(task['state'], 'done', f'续传失败: {task}')
        self.assertEqual(task['resumed_from'], split, '没有从断点续传')
        self.assertEqual(task['sent'], len(payload))
        self.assertEqual((self.drop / '断点.bin').read_bytes(), payload)
        self.assertFalse((self.drop / '断点.bin.part').exists())

    def test_resume_is_refused_when_the_local_file_changed(self):
        """本地文件改过之后不许续传：那份 `.part` 与现在的文件对不上，续了就是拼接垃圾。"""
        payload = bytes(range(256)) * 512         # 128 KB
        source = self._source('改过.bin', payload)
        split = 64 * 1024
        (self.drop / '改过.bin.part').write_bytes(b'X' * split)

        stat = source.stat()
        key = '|'.join(('', self.DROP_ID, '改过.bin', str(source)))
        record_file = self._record_file()
        record_file.parent.mkdir(parents=True, exist_ok=True)
        record_file.write_text(json.dumps({
            # 记录里的 mtime 与当前文件不一致 —— 等价于"本地文件在上次上传后被改过"
            key: {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns - 1, 'sent': split,
                  'ts': 1700000000}}), encoding='utf-8')

        task = self._run_upload(source, '改过.bin')
        self.assertEqual(task['state'], 'done', f'上传失败: {task}')
        self.assertEqual(task['resumed_from'], 0, '本地文件变了还续传了')
        self.assertEqual((self.drop / '改过.bin').read_bytes(), payload,
                         '从头传的内容必须完整（而不是拼上那份对不上的 .part）')



if __name__ == '__main__':
    unittest.main()
