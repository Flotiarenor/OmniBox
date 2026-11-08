"""group-mesh 插件后端（plugins/group-mesh）的骨架测试。

覆盖的是**插件层**的行为，不是协议内核（内核测试在 tests/test_group_mesh_mvp.py）：

  * manifest 字段与目录结构符合规范；
  * 插件能脱离 GUI 被实例化，register_api 暴露的方法都是可调用的；
  * 契约要求：get_protected_paths 必须覆盖身份目录（里面是长期私钥）；
  * 共享根的安全约束：不得落在数据根或身份目录之内；
  * 身份/团体/共享项/节点状态这条链路能跑通，且参数兼容「结构化对象」与
    「逐个位置参数」两种调用形态（`/api` 会把 args/kwargs 直接展开）。

运行：
    python -m unittest tests.test_group_mesh_plugin -v
"""

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar, Dict, List
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling
from shell.backend.settings_store import SettingsStore
from shell.groupmesh.transport import TransportError

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'group-mesh'


def load_plugin_module():
    """按 PluginManager 的方式加载插件后端（importlib 直接加载入口文件）。"""
    return load_sibling(str(PLUGIN_DIR / 'backend' / 'main.py'), 'main', 'group-mesh')


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))

    def test_required_fields(self):
        for field in ('name', 'version', 'displayName', 'icon'):
            self.assertIn(field, self.manifest)
        self.assertEqual(self.manifest['name'], 'group-mesh')
        self.assertRegex(self.manifest['version'], r'^\d+\.\d+\.\d+$')

    def test_backend_and_frontend_entries_exist(self):
        self.assertTrue((PLUGIN_DIR / self.manifest['backend']['entry']).is_file())
        self.assertTrue((PLUGIN_DIR / self.manifest['frontend']['entry']).is_file())

    def test_core_plugin_declares_no_dependency_on_itself(self):
        # group-mesh 是核心插件：Companion 插件依赖它，它不依赖别人
        self.assertEqual(self.manifest.get('dependencies', []), [])


class PluginContractTest(unittest.TestCase):
    """插件在只有配置字典的情况下就能加载 —— 这是脱离 GUI 可测的前提。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        module = load_plugin_module()
        self.manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        config = {'directories': {'data_root': str(self.tmp / 'data')}}
        self.plugin = module.GroupMeshPlugin(self.manifest, config)

    def tearDown(self):
        self.plugin.on_unload()
        self._tmp.cleanup()

    def test_is_plugin_base_and_declares_api(self):
        self.assertIsInstance(self.plugin, PluginBase)
        api = self.plugin.register_api()
        self.assertTrue(api)
        for name, fn in api.items():
            self.assertTrue(callable(fn), f'{name} 不是可调用对象')

    def test_expected_api_surface(self):
        expected = {'get_status', 'init_identity', 'get_device_keys', 'create_group',
                    'join_group', 'get_invite', 'add_member', 'add_share', 'remove_share',
                    'start_node', 'stop_node', 'get_node_status'}
        self.assertTrue(expected <= set(self.plugin.register_api()))

    def test_settings_api_surface(self):
        """设置 API 必须登记：前端的「设置」按钮走壳的统一设置弹窗。

        `openSettingsModal()`（plugins/group-mesh/frontend/js/app.js）内部用
        `Bridge.call('get_settings')` 取值、`Bridge.call('save_settings')` 落盘，即
        `POST /api/group-mesh__<方法>`；而 PluginManager 只额外登记
        `<插件>__get_settings_schema`。此前两个都没登记，于是弹窗里全是 schema 默认值
        （port / bind / ttl_days 都改不了），点保存必然"保存失败"且无报错。
        """
        api = set(self.plugin.register_api())
        self.assertIn('get_settings', api)
        self.assertIn('save_settings', api)

    def test_remote_api_surface(self):
        """远端浏览/取回必须真的挂在 register_api 上。

        在此之前内核已有 `client.list_shares / list_directory / fetch_to_file`，
        但插件一条都没暴露 —— 于是"跨机联调通过"与"界面里拉不回一个文件"并存。
        """
        api = set(self.plugin.register_api())
        for name in ('list_peers', 'list_remote', 'download_remote', 'upload_remote',
                     'upload_status', 'cancel_upload', 'refresh_share_roots',
                     'materialize_remote', 'mirror_share'):
            self.assertIn(name, api)

    def test_upload_status_and_cancel_are_honest_without_a_task(self):
        """没有任务时不能假装成功：进度查询与取消都要给出明确结论。"""
        self.plugin.identity.init_identity({'name': 'up-status'})
        self.plugin.identity.create_group({'group': 'up-group'})
        status = self.plugin.uploads.status()
        self.assertTrue(status['success'])
        self.assertEqual(status['tasks'], [])
        missing = self.plugin.uploads.status({'task_id': 'nope'})
        self.assertFalse(missing['success'])
        self.assertIn('没有这个上传任务', missing['error'])
        cancelled = self.plugin.uploads.cancel({'task_id': 'nope'})
        self.assertFalse(cancelled['success'])
        no_id = self.plugin.uploads.cancel({})
        self.assertFalse(no_id['success'])
        self.assertIn('task_id', no_id['error'])

    def test_upload_task_is_persisted_to_disk(self):
        """任务表要落盘：否则插件一重载，用户的上传就"凭空消失"。"""
        self.plugin.identity.init_identity({'name': 'up-persist'})
        self.plugin.identity.create_group({'group': 'up-group'})
        source = self.tmp / 'persist.bin'
        source.write_bytes(b'x' * 32)
        started = self.plugin.upload.upload_remote({'share_id': 'drop', 'local_path': str(source),
                                             'remote_path': 'persist.bin'})
        self.assertTrue(started['success'], started)
        task_id = started['task_id']

        # 没有登记任何对端地址 → 任务很快以 failed 结束（不碰网络）
        task = {}
        for _ in range(100):
            task = self.plugin.uploads.status({'task_id': task_id})['task']
            if task['state'] in ('done', 'failed', 'cancelled', 'interrupted'):
                break
            time.sleep(0.05)
        self.assertEqual(task['state'], 'failed', task)

        on_disk = json.loads((self.plugin.get_data_root() / 'upload-tasks.json')
                             .read_text(encoding='utf-8'))
        self.assertIn(task_id, on_disk)
        self.assertEqual(on_disk[task_id]['state'], 'failed')
        self.assertEqual(on_disk[task_id]['remote_path'], 'persist.bin')

    def test_reload_marks_running_tasks_as_interrupted(self):
        """重载后未结束的任务必须标记为"被中断"，已结束的历史照旧保留。

        这里的"重载"是壳真正做的事：**同一个数据根、新的插件对象、走 on_load()**。
        进程没了线程就没了，把上次的 `running` 当成"正在上传"，界面会一直转圈等一个
        不存在的进度。
        """
        self.plugin.on_load()
        self.plugin.identity.init_identity({'name': 'up-restore'})
        path = self.plugin.get_data_root() / 'upload-tasks.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            'cut-off': {'task_id': 'cut-off', 'state': 'running', 'sent': 4096,
                        'total': 8192, 'remote_path': 'b.bin', 'started_at': 3},
            'old-done': {'task_id': 'old-done', 'state': 'done', 'sent': 10, 'total': 10,
                         'remote_path': 'a.bin', 'started_at': 1, 'finished_at': 2},
        }, ensure_ascii=False), encoding='utf-8')

        reloaded = self._reload_plugin()

        tasks = {task['task_id']: task for task in reloaded.uploads.status()['tasks']}
        self.assertEqual(tasks['old-done']['state'], 'done')
        self.assertEqual(tasks['cut-off']['state'], 'interrupted')
        self.assertEqual(tasks['cut-off']['sent'], 4096, '已传字节数要保留（界面据此说明续传点）')
        self.assertIn('续传', tasks['cut-off']['error'])
        # 纠正后的状态要写回文件，否则每次加载都会重复同一句"发现中断"
        on_disk = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(on_disk['cut-off']['state'], 'interrupted')

    def _reload_plugin(self):
        """同一个数据根上再起一个插件对象并走 on_load —— 等价于壳重载本插件。"""
        module = load_plugin_module()
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        config = {'directories': {'data_root': str(self.tmp / 'data')}}
        instance = module.GroupMeshPlugin(manifest, config)
        instance._settings_store = SettingsStore(str(self.tmp / 'settings'))
        instance.on_load()
        self.addCleanup(instance.on_unload)
        return instance

    def test_unloaded_plugin_does_not_overwrite_the_task_file(self):
        """卸载后老实例不得再写任务表。

        取消是"到分块边界才生效"，所以老实例的上传线程会在卸载后继续收尾一段时间；
        而同一个数据根已经交给新实例了 —— 让老线程把它的旧快照写回去，会把新实例
        刚记下的任务覆盖掉。
        """
        self.plugin.on_load()
        path = self.plugin.get_data_root() / 'upload-tasks.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'newer': {'task_id': 'newer', 'state': 'done'}}),
                        encoding='utf-8')

        self.plugin.on_unload()
        self.plugin.uploads.persist()          # 老线程迟到的落盘

        self.assertEqual(set(json.loads(path.read_text(encoding='utf-8'))), {'newer'})

    def test_declares_network_location_provider(self):
        """注册成「网络位置」提供方，且 embedUrl 指向**真实存在**的页面。

        壳的共享目录组件（FolderPicker）按这条 URL 去嵌 iframe，写错路径不会在任何
        地方报错 —— 只会在用户点「网络位置」时弹出一个白屏。所以用文件存在性钉住它。
        """
        extensions = self.plugin.get_extensions()
        provider = next((e for e in extensions if e.get('placement') == 'network-location'), None)
        self.assertIsNotNone(provider, f'没有注册网络位置提供方: {extensions}')
        # 不写 host：组件出现在**任意**插件的设置里，提供方要对所有宿主可用
        self.assertNotIn('host', provider)
        url = str(provider.get('embedUrl') or '')
        self.assertTrue(url.startswith('/plugins/group-mesh/frontend/'), url)
        page = PLUGIN_DIR / 'frontend' / url.split('/frontend/', 1)[1]
        self.assertTrue(page.is_file(), f'提供方页面不存在: {page}')

    def test_identity_dir_is_protected(self):
        """设备私钥不导出。插件至少必须申报身份目录，禁止文件服务端出去。"""
        protected = [Path(p) for p in self.plugin.get_protected_paths()]
        self.assertIn(self.plugin.storage.identity_dir.resolve(),
                      [p.resolve() for p in protected])

    def test_settings_schema_shape(self):
        keys = {item['key'] for item in self.plugin.settings_schema}
        self.assertEqual(keys, {'port', 'bind', 'group_name', 'principal_name',
                                'ttl_days', 'download_dir', 'max_fetch_mb',
                                'sync_interval_seconds'})
        for item in self.plugin.settings_schema:
            self.assertIn('label', item)
            self.assertIn('type', item)
        # 下载目录必须由 Shell 的目录选择器渲染（`directory` 类型），
        # 否则用户又得手敲一个绝对路径。
        download = next(i for i in self.plugin.settings_schema if i['key'] == 'download_dir')
        self.assertEqual(download['type'], 'directory')

    def test_status_before_identity(self):
        status = self.plugin.report.get_status()
        self.assertIn('kernel', status)
        self.assertIsNone(status['identity'])
        self.assertIsNone(status['roster'])
        self.assertEqual(status['shares'], [])


class PluginWorkflowTest(unittest.TestCase):
    """身份 -> 团体 -> 共享项 这条链路。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        module = load_plugin_module()
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        config = {'directories': {'data_root': str(self.tmp / 'data')}}
        self.plugin = module.GroupMeshPlugin(manifest, config)
        # 真正的 SettingsStore：没有它时 update_setting() 会静默失败（返回 False 但没人看），
        # 于是"改设置再启动节点"这类用例会假通过。
        self.plugin._settings_store = SettingsStore(str(self.tmp / 'settings'))

    def tearDown(self):
        self.plugin.on_unload()
        self._tmp.cleanup()

    def test_init_identity_then_create_group(self):
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        result = self.plugin.identity.init_identity({'name': 'unit-test'})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['identity']['name'], 'unit-test')

        # 重复 init 必须被拒（不能静默覆盖私钥）
        again = self.plugin.identity.init_identity({'name': 'other'})
        self.assertFalse(again['success'])

        created = self.plugin.identity.create_group({'group': 'unit-group'})
        self.assertTrue(created['success'], created)
        self.assertEqual(created['group'], 'unit-group')
        self.assertTrue(created['invite'].startswith('gm1:'))

        status = self.plugin.report.get_status()
        self.assertEqual(status['roster']['role'], 'owner')
        self.assertTrue(status['roster']['in_roster'])

    def test_positional_and_structured_arguments_agree(self):
        """两种调用形态都必须被接受（`/api` 会展开 args/kwargs）。"""
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        positional = self.plugin.identity.init_identity(None, 'positional-name')
        self.assertTrue(positional['success'], positional)

    def test_get_device_keys(self):
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'keys'})
        keys = self.plugin.identity.get_device_keys()
        self.assertTrue(keys['success'])
        self.assertTrue(keys['principal'])
        self.assertTrue(keys['device'])
        # 主体公钥与设备公钥必须是不同的两把（用途分离）
        self.assertNotEqual(keys['principal'], keys['device'])

    def test_share_root_must_exist(self):
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'shares'})
        result = self.plugin.shares.add_share({'share_id': 'x', 'path': str(self.tmp / 'nope')})
        self.assertFalse(result['success'])

    def test_share_root_cannot_be_inside_data_root(self):
        """共享目录必须与程序数据、身份目录分离。"""
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'guard'})
        inside = self.plugin.get_data_root()
        inside.mkdir(parents=True, exist_ok=True)
        result = self.plugin.shares.add_share({'share_id': 'bad', 'path': str(inside)})
        self.assertFalse(result['success'])
        self.assertIn('不得位于', result['error'])

    def test_add_and_remove_share(self):
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'share-ok'})
        shared = self.tmp / 'shared'
        shared.mkdir()
        (shared / 'file.txt').write_text('hi', encoding='utf-8')

        added = self.plugin.shares.add_share({'share_id': 'docs', 'path': str(shared),
                                       'read': 'group', 'write': 'owner'})
        self.assertTrue(added['success'], added)
        self.assertEqual(len(self.plugin.report.get_status()['shares']), 1)

        # 共享项声明必须能验签（内核在读取时会复核）
        reloaded = self.plugin.storage.load_shares()
        self.assertTrue(reloaded['docs'].declaration.verify_signature())

        removed = self.plugin.shares.remove_share({'share_id': 'docs'})
        self.assertTrue(removed['success'])
        self.assertEqual(self.plugin.report.get_status()['shares'], [])

    def test_new_share_has_no_capacity_limit(self):
        """**界面挂载共享项不设容量上限**（按实测放宽，见 shares.DEFAULT_MAX_BYTES）。

        为什么要专门锁这条：协议侧的容量判定是"每条连接量一次基线再推算"，实测
        8 条并发连接能在"上限 1000 字节"的共享项上写进 3200 字节且全部成功 ——
        它给不出可信保证，却会让人以为有防线。因此默认不设限，且**界面不再发送
        硬编码的 1 GiB**（那既不是用户选的，也挡不住并发）。
        """
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'no-cap'})
        shared = self.tmp / 'shared-nocap'
        shared.mkdir()

        added = self.plugin.shares.add_share({'share_id': 'open', 'path': str(shared)})
        self.assertTrue(added['success'], added)
        self.assertIsNone(added['max_bytes'], '不传 max_bytes 必须落成"不限制"')
        self.assertIsNone(self.plugin.storage.load_shares()['open'].max_bytes)
        # 状态载荷里也要是"不限制"，否则界面会显示成 0 MiB
        roots = self.plugin.get_share_roots()
        self.assertIsNone(roots[0]['max_bytes'])

        # 显式给 0 / None 同样是"不限制"
        self.assertIsNone(self.plugin.shares.add_share(
            {'share_id': 'open0', 'path': str(shared), 'max_bytes': 0})['max_bytes'])
        self.assertIsNone(self.plugin.storage.load_shares()['open0'].max_bytes)

    def test_explicit_capacity_limit_is_still_honoured(self):
        """保留显式设置上限的通道：它是"尽力而为"的防线，不是不可用的功能。"""
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'with-cap'})
        shared = self.tmp / 'shared-cap'
        shared.mkdir()

        added = self.plugin.shares.add_share({'share_id': 'capped', 'path': str(shared),
                                       'max_bytes': 2 * 1024 * 1024})
        self.assertTrue(added['success'], added)
        self.assertEqual(added['max_bytes'], 2 * 1024 * 1024)
        self.assertEqual(self.plugin.storage.load_shares()['capped'].max_bytes, 2 * 1024 * 1024)
        # 非法取值必须明确拒绝，而不是静默当成"不限制"
        bad = self.plugin.shares.add_share({'share_id': 'bad-cap', 'path': str(shared),
                                     'max_bytes': 'many'})
        self.assertFalse(bad['success'])
        self.assertIn('max_bytes', bad['error'])

    def test_bad_share_id_rejected(self):
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'bad-id'})
        shared = self.tmp / 'shared2'
        shared.mkdir()
        result = self.plugin.shares.add_share({'share_id': '../evil', 'path': str(shared)})
        self.assertFalse(result['success'])

    def test_node_cannot_start_without_group(self):
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'no-group'})
        result = self.plugin.node.start()
        self.assertFalse(result['success'])
        self.assertIn('团体', result['error'])

    def test_node_start_and_stop(self):
        """真实监听一个随机端口，确认启停与状态回报。"""
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'node'})
        self.plugin.identity.create_group({'group': 'node-group'})
        self.assertTrue(self.plugin.update_setting('bind', '127.0.0.1'))
        # port=0 让内核分配空闲端口，避免测试撞上真实占用的 19443
        self.assertTrue(self.plugin.update_setting('port', 0))

        started = self.plugin.node.start()
        self.assertTrue(started['success'], started)
        self.assertTrue(started['node']['running'])
        self.assertIsNotNone(started['node']['listening'])
        self.assertNotIn(':0', started['node']['listening'] or ':0',
                         '监听地址应回报内核实际绑定的端口，而不是设置里的 0')

        stopped = self.plugin.node.stop()
        self.assertTrue(stopped['success'])
        self.assertFalse(stopped['node']['running'])

    def test_join_group_rejects_bad_invite(self):
        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'joiner'})
        for bad in ('', 'not-an-invite', 'gm1:!!!!'):
            with self.subTest(bad=bad):
                result = self.plugin.identity.join_group({'invite': bad})
                self.assertFalse(result['success'])


class RosterDistributionTest(unittest.TestCase):
    """群主加人后，成员必须能把本机名单更新到新版本。

    这是一条真实的失效路径：群主在 A 机执行 roster add 签发 v2，B 机仍停在 v1，
    于是 B 看不到新成员、也连不上任何人（对端用 v2 判 B 在名单里，B 用 v1 判对端
    不在名单里，双向都拒）。名单靠**带外分发**，因此"更新名单"必须是可操作的入口，
    而不是只有第一次加入时才用得到。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        module = load_plugin_module()
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        self.module = module
        self.manifest = manifest

    def tearDown(self):
        self._tmp.cleanup()

    def _plugin(self, name: str):
        """在独立数据目录里造一个插件实例（模拟一台机器）。"""
        config = {'directories': {'data_root': str(self.tmp / name / 'data')}}
        plugin = self.module.GroupMeshPlugin(self.manifest, config)
        plugin._settings_store = SettingsStore(str(self.tmp / name / 'settings'))
        return plugin

    def test_member_can_update_from_v1_to_v2(self):
        if not self._plugin('probe').report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner')
        member = self._plugin('member')
        owner.identity.init_identity({'name': 'owner'})
        member.identity.init_identity({'name': 'member'})

        # 群主创建团体（v1），成员用邀请串加入
        created = owner.identity.create_group({'group': 'dist-group'})
        self.assertTrue(created['success'], created)
        joined = member.identity.join_group({'invite': created['invite']})
        self.assertTrue(joined['success'], joined)
        self.assertFalse(joined['updated'], '首次加入不应标记为更新')
        self.assertEqual(joined['version'], 1)
        # v1 里还没有 member 本人，属于预期（群主还没登记它）
        self.assertFalse(joined['in_roster'])

        # 群主登记成员设备 -> 签发 v2
        member_keys = member.identity.get_device_keys()
        added = owner.identity.add_member({'principal': member_keys['principal'],
                                  'device': member_keys['device'],
                                  'name': 'member'})
        self.assertTrue(added['success'], added)
        self.assertEqual(added['version'], 2)

        # 成员侧此前看不到自己；拿到 v2 邀请串后才能对上
        before = member.report.get_status()
        self.assertFalse(before['roster']['in_roster'])
        self.assertEqual(before['roster']['version'], 1)

        updated = member.identity.join_group({'invite': added['invite']})
        self.assertTrue(updated['success'], updated)
        self.assertTrue(updated['updated'], '应标记为"更新"而不是"加入"')
        self.assertEqual(updated['previous_version'], 1)
        self.assertEqual(updated['version'], 2)
        self.assertTrue(updated['in_roster'], 'v2 之后成员应能在名单里看到自己')
        self.assertEqual(updated['role'], 'member')

        after = member.report.get_status()
        self.assertEqual(after['roster']['version'], 2)
        self.assertTrue(after['roster']['in_roster'])
        self.assertEqual(after['roster']['member_count'], 2)

    def test_stale_invite_is_rejected_with_a_useful_message(self):
        """给出比本机更旧的邀请串时，要明确说明"本机已是 vN"。"""
        if not self._plugin('probe2').report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner2')
        member = self._plugin('member2')
        owner.identity.init_identity({'name': 'owner'})
        member.identity.init_identity({'name': 'member'})

        v1_invite = owner.identity.create_group({'group': 'stale-group'})['invite']
        self.assertTrue(member.identity.join_group({'invite': v1_invite})['success'])

        keys = member.identity.get_device_keys()
        v2_invite = owner.identity.add_member({'principal': keys['principal'],
                                      'device': keys['device'], 'name': 'member'})['invite']
        self.assertTrue(member.identity.join_group({'invite': v2_invite})['success'])

        # 再贴一次 v1：版本未前进，必须被拒且提示里带上版本号
        again = member.identity.join_group({'invite': v1_invite})
        self.assertFalse(again['success'])
        self.assertIn('本机已是 v2', again['error'])

    def test_roster_update_requires_direct_successor(self):
        """跳版（v1 -> v3）必须被拒：规则 2 要求 prev 指向当前名单哈希。"""
        if not self._plugin('probe3').report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner3')
        member = self._plugin('member3')
        owner.identity.init_identity({'name': 'owner'})
        member.identity.init_identity({'name': 'member'})
        self.assertTrue(member.identity.join_group(
            {'invite': owner.identity.create_group({'group': 'chain-group'})['invite']})['success'])

        keys = member.identity.get_device_keys()
        owner.identity.add_member({'principal': keys['principal'], 'device': keys['device'],
                          'name': 'member'})
        # 群主再加一个成员 -> v3，跳过 v2
        other = self._plugin('other3')
        other.identity.init_identity({'name': 'other'})
        other_keys = other.identity.get_device_keys()
        v3 = owner.identity.add_member({'principal': other_keys['principal'],
                               'device': other_keys['device'], 'name': 'other'})
        self.assertEqual(v3['version'], 3)

        skipped = member.identity.join_group({'invite': v3['invite']})
        self.assertFalse(skipped['success'], '跳版名单不应被接受')
        self.assertIn('prev', skipped['error'])


    def test_roster_is_adopted_automatically_when_peers_push_it(self):
        """成员不必再手工贴邀请串：从对端拉到的名单会被验签、采纳并落盘。

        这条覆盖的是"自动分发"在插件层的接线：`_adopt_remote_roster`
        要跑规则 1–6、要落盘、要让 `get_status()` 能显示出"已从对方升级"。
        为什么必须锁落盘：只更新内存副本的话，插件重载（改设置、升级、壳重启）
        会退回旧名单 —— 表现成"刚升级完又变回去了"。
        """
        if not self._plugin('probe4').report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner4')
        member = self._plugin('member4')
        owner.identity.init_identity({'name': 'owner'})
        member.identity.init_identity({'name': 'member'})
        created = owner.identity.create_group({'group': 'auto-dist'})
        self.assertTrue(member.identity.join_group({'invite': created['invite']})['success'])

        keys = member.identity.get_device_keys()
        added = owner.identity.add_member({'principal': keys['principal'],
                                  'device': keys['device'], 'name': 'member'})
        self.assertEqual(added['version'], 2)
        owner_roster = owner.storage.load_roster()
        assert owner_roster is not None

        # 成员侧：拉一次"对端名单"，走的就是 `_fetch_registry_from` 里那条自动路径
        class _FakeConnection:
            def __init__(self, roster):
                self.roster = roster

            def request(self, payload):
                assert payload['op'] == 'roster'
                return {'status': 'ok', 'roster': self.roster.to_dict(),
                        'version': self.roster.version, 'group': self.roster.group}

        member.discovery.adopt_remote_roster(_FakeConnection(owner_roster), ('127.0.0.1', 19443))

        after = member.report.get_status()
        self.assertEqual(after['roster']['version'], 2, '成员应已升到 v2')
        self.assertTrue(after['roster']['in_roster'], 'v2 之后成员能看到自己')
        notice = after['roster']['adopted_notice']
        self.assertIsNotNone(notice, '界面要能显示"已从对方升级名单"')
        self.assertEqual((notice['previous_version'], notice['version']), (1, 2))
        # 落盘：重新读盘仍是 v2（插件重载不会退回旧名单）
        reloaded = member.storage.load_roster()
        assert reloaded is not None
        self.assertEqual(reloaded.version, 2)
        # 对端名单版本也被记下来，供界面做对照
        self.assertEqual(member.facts.roster_notes[('127.0.0.1', 19443)]['version'], 2)

    def test_stale_roster_from_a_peer_is_ignored(self):
        """对端报的名单不比本机新时直接跳过：不该白白写盘，也不该把已采纳的清掉。"""
        if not self._plugin('probe5').report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner5')
        member = self._plugin('member5')
        owner.identity.init_identity({'name': 'owner'})
        member.identity.init_identity({'name': 'member'})
        v1 = owner.identity.create_group({'group': 'stale-dist'})['invite']
        self.assertTrue(member.identity.join_group({'invite': v1})['success'])
        v1_roster = owner.storage.load_roster()
        assert v1_roster is not None

        class _FakeConnection:
            def __init__(self, roster):
                self.roster = roster

            def request(self, payload):
                return {'status': 'ok', 'roster': self.roster.to_dict(),
                        'version': self.roster.version, 'group': self.roster.group}

        member.discovery.adopt_remote_roster(_FakeConnection(v1_roster), ('127.0.0.1', 19443))
        self.assertIsNone(member.report.get_status()['roster']['adopted_notice'])


    def test_group_operations_record_roster_history(self):
        """群主加人与成员加入都必须把名单并入历史 —— 准入判定靠它。

        为什么锁在插件层：本机"认不认得某个旧版本"完全取决于这些入口有没有写历史。
        少了任何一处，落后多版的设备就会过不了准入，表现为"我明明被加进去了，
        却拉不到新名单"，而原因藏在另一台机器上。
        """
        if not self._plugin('probe6').report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner6')
        member = self._plugin('member6')
        owner.identity.init_identity({'name': 'owner'})
        member.identity.init_identity({'name': 'member'})
        created = owner.identity.create_group({'group': 'history-group'})          # v1
        self.assertEqual([r.version for r in owner.storage.load_roster_history()], [1])

        keys = member.identity.get_device_keys()
        added = owner.identity.add_member({'principal': keys['principal'],
                                  'device': keys['device'], 'name': 'member'})   # v2
        self.assertEqual([r.version for r in owner.storage.load_roster_history()], [2, 1],
                         '群主签发的新版本必须进历史')
        # 历史落盘：换一个实例（模拟插件重载）仍读得到
        reloaded = self._plugin('owner6')
        self.assertEqual([r.version for r in reloaded.storage.load_roster_history()], [2, 1])

        # 成员用 v1 加入、再用 v2 更新：两次都要进它自己的历史
        self.assertTrue(member.identity.join_group({'invite': created['invite']})['success'])
        self.assertEqual([r.version for r in member.storage.load_roster_history()], [1])
        self.assertTrue(member.identity.join_group({'invite': added['invite']})['success'])
        self.assertEqual([r.version for r in member.storage.load_roster_history()], [2, 1],
                         '成员收到的每个版本都必须进历史')

    def test_history_survives_plugin_reload(self):
        """历史落盘：插件重载（改设置、升级、壳重启）后仍然认得出旧版本。"""
        if not self._plugin('probe7').report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner7')
        owner.identity.init_identity({'name': 'owner'})
        owner.identity.create_group({'group': 'reload-group'})
        member = self._plugin('member7')
        member.identity.init_identity({'name': 'member'})
        self.assertTrue(member.identity.join_group(
            {'invite': owner.identity.get_invite()['invite']})['success'])

        reloaded = self._plugin('member7')
        self.assertEqual([r.version for r in reloaded.storage.load_roster_history()], [1])


class _EngineSpy:
    """只记录 `reset_throttle` 的替身引擎（共享项增删应当触发一次）。"""

    def __init__(self) -> None:
        self.resets = 0

    def reset_throttle(self) -> None:
        self.resets += 1


class ShareLocationTest(unittest.TestCase):
    """共享根是一个**有状态的位置**，而不是共享项里的一个字符串。

    位置（`share_roots.json`）与对外声明（`shares.json` 里签名过的 declaration）
    分开存：前者是本机事实、从不发给对端，后者是要签名的协议对象。混在一坨里，
    "目录还在不在盘上"这类本机状态就会污染协议对象。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        module = load_plugin_module()
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        config = {'directories': {'data_root': str(self.tmp / 'data')}}
        self.plugin = module.GroupMeshPlugin(manifest, config)
        self.plugin._settings_store = SettingsStore(str(self.tmp / 'settings'))
        self.shared = self.tmp / 'shared'
        self.shared.mkdir()

    def tearDown(self):
        self.plugin.on_unload()
        self._tmp.cleanup()

    def _add(self, share_id='docs'):
        return self.plugin.shares.add_share({'share_id': share_id, 'path': str(self.shared),
                                      'read': 'group', 'write': 'owner'})

    def assertSamePath(self, actual, expected, msg=''):
        """比较两条路径是否指向同一位置。

        不能逐字比字符串：Windows 上同一目录可能以 8.3 短名
        （`C:\\Users\\ADMINI~1\\…`）或长名出现，`resolve()` 返回哪一种取决于该目录
        当时是否已存在 —— 逐字比较会随机假失败（`test_plugin_host_contract` 里同款）。
        """
        try:
            self.assertTrue(os.path.samefile(actual, expected), msg)
        except OSError:
            self.assertEqual(os.path.normcase(str(Path(actual).resolve())),
                             os.path.normcase(str(Path(expected).resolve())), msg)

    def test_location_is_stored_separately_from_declaration(self):
        self.plugin.identity.init_identity({'name': 'loc'})
        self.assertTrue(self._add()['success'])
        roots_file = self.plugin.storage.identity_dir / 'share_roots.json'
        self.assertTrue(roots_file.is_file(), '位置应写进独立的 share_roots.json')
        roots = json.loads(roots_file.read_text(encoding='utf-8'))['roots']
        self.assertSamePath(roots['docs']['path'], self.shared)
        # 声明文件里不应再持有路径（那是本机事实，不该进签名对象）
        shares = json.loads((self.plugin.storage.identity_dir / 'shares.json')
                            .read_text(encoding='utf-8'))['shares']
        self.assertIn('declaration', shares['docs'])
        self.assertEqual(shares['docs']['declaration']['acl']['read'], 'group')

    def test_location_reports_missing_directory(self):
        """共享根所在磁盘未接入时必须报"不可用"，而不是让对端看到空目录。"""
        self.plugin.identity.init_identity({'name': 'missing'})
        self.assertTrue(self._add()['success'])
        roots = self.plugin.get_share_roots()
        self.assertEqual(len(roots), 1)
        self.assertTrue(roots[0]['available'])

        import shutil
        shutil.rmtree(self.shared)
        roots = self.plugin.get_share_roots()
        self.assertFalse(roots[0]['available'], '目录没了却仍报可用会让对端看空目录')
        self.assertIn('不存在', roots[0]['reason'] or '')

    def test_downloads_dir_follows_setting(self):
        default_dir = self.plugin.downloads_dir
        self.assertEqual(default_dir.name, 'downloads')
        custom = self.tmp / 'my-downloads'
        custom.mkdir()
        self.assertTrue(self.plugin.update_setting('download_dir', str(custom)))
        self.assertSamePath(self.plugin.downloads_dir, custom)
        # 缓存与下载必须是两个位置：前者可随时删，后者是用户要的文件
        self.assertNotEqual(str(self.plugin.cache_dir.resolve()),
                            str(self.plugin.downloads_dir.resolve()))

    def test_refresh_reports_usage(self):
        self.plugin.identity.init_identity({'name': 'usage'})
        (self.shared / 'a.bin').write_bytes(b'x' * 1000)
        self.assertTrue(self._add()['success'])
        result = self.plugin.shares.refresh_share_roots()
        self.assertTrue(result['success'])
        item = result['roots'][0]
        self.assertTrue(item['available'])
        self.assertEqual(item['used_bytes'], 1000)
        self.assertEqual(item['entries'], 1)

    def test_tree_usage_flags_lower_bound_when_stopped_early(self):
        """命中配额就停下时数字只是下界，必须标截断（界面据此显示"≥"）。

        回归对象：该分支原先返回 `truncated=False`，与"截断表示下界"的约定自相矛盾，
        界面会把"扫到配额就停了"显示成精确用量。
        """
        (self.shared / 'a.bin').write_bytes(b'x' * 1000)

        used, truncated = self.plugin.storage.tree_usage(self.shared, limit=10)

        self.assertGreaterEqual(used, 10)
        self.assertTrue(truncated, '提前停下必须标截断')
        full, truncated_full = self.plugin.storage.tree_usage(self.shared)
        self.assertEqual(full, 1000)
        self.assertFalse(truncated_full)

    def test_verify_populates_usage_cache(self):
        """用量由统一刷新的「校验」算出来并缓存（旧入口没有任何调用方）。"""
        from shell.backend.freshness import engine_for

        self.plugin.identity.init_identity({'name': 'usage-verify'})
        (self.shared / 'b.bin').write_bytes(b'y' * 512)
        self.assertTrue(self._add()['success'])
        engine = engine_for(self.plugin)
        self.assertIsNotNone(engine, 'group-mesh 必须声明 freshness_spec()')
        engine.spec.min_sync_interval = 0.0

        report = engine.verify()

        self.assertEqual(report['pass_end'].get('shares'), 1)
        self.assertEqual(report['pass_end'].get('used_bytes'), 512)
        self.assertTrue(self.plugin.shares.usage_cache)

    # ===== 用量失效、根集合变化、状态序列化 =====

    def _root_path(self, share_id: str) -> str:
        """共享根在插件里记录的路径。

        `add_share` 保存的是 `resolve()` 之后的形式，临时目录的短名（`ADMINI~1`）与它
        不同；事件路径必须用同一个形式，否则比较的是两个字符串而不是两个位置。
        """
        return str({item['share_id']: item['path']
                    for item in self.plugin.get_share_roots()}[share_id])

    def _seed_usage(self, share_id: str, used: int = 10) -> None:
        self.plugin.shares.usage_cache[share_id] = {
            'path': self._root_path(share_id), 'used_bytes': used,
            'truncated': False, 'entries': 1, 'at': 1,
        }

    def test_usage_invalidates_by_event_path(self):
        """条目事件带绝对路径：落在共享根下才作废该根的用量。"""
        self.plugin.identity.init_identity({'name': 'usage-derive'})
        (self.shared / 'sub').mkdir()
        (self.shared / 'sub' / 'a.bin').write_bytes(b'x' * 10)
        self.assertTrue(self._add()['success'])
        self._seed_usage('docs')
        root = Path(self._root_path('docs'))

        outside = self.tmp / 'outside' / 'b.bin'
        self.assertEqual(self.plugin.shares.derive([{'key': 'b.bin', 'path': str(outside)}])['dropped'], 0)
        self.assertIn('docs', self.plugin.shares.usage_cache)

        dropped = self.plugin.shares.derive([
            {'key': 'sub/a.bin', 'path': str(root / 'sub' / 'a.bin')}])
        self.assertEqual(dropped['dropped'], 1)
        self.assertNotIn('docs', self.plugin.shares.usage_cache)

    def test_nested_shares_invalidate_only_the_innermost(self):
        """嵌套共享根：事件路径归最内层，外层用量不受影响。"""
        self.plugin.identity.init_identity({'name': 'usage-nested'})
        inner = self.shared / 'inner'
        inner.mkdir()
        (inner / 'x.bin').write_bytes(b'x' * 4)
        self.assertTrue(self._add('outer')['success'])
        self.assertTrue(self.plugin.shares.add_share(
            {'share_id': 'inner', 'path': str(inner)})['success'])
        self._seed_usage('outer')
        self._seed_usage('inner')
        inner_root = Path(self._root_path('inner'))

        self.plugin.shares.derive([{'key': 'x.bin', 'path': str(inner_root / 'x.bin')}])

        self.assertIn('outer', self.plugin.shares.usage_cache)
        self.assertNotIn('inner', self.plugin.shares.usage_cache)

    def test_share_changes_reset_sync_throttle(self):
        """共享根集合变了：下一次同步不该被最小间隔挡掉。"""
        module = load_plugin_module()
        original = module.engine_for
        spy = _EngineSpy()

        def fake_engine(_plugin):
            return spy

        module.engine_for = fake_engine
        try:
            self.plugin.identity.init_identity({'name': 'throttle'})
            self.assertTrue(self._add()['success'])
            self.assertTrue(self.plugin.shares.remove_share({'share_id': 'docs'})['success'])
        finally:
            module.engine_for = original
        self.assertEqual(spy.resets, 2, '挂载与移除共享项各应让下一趟同步立即生效')

    def test_share_roots_report_cached_usage_without_scanning(self):
        """首屏读缓存；用量作废后才由 `share_roots_report` 现算。"""
        from shell.backend.freshness import engine_for

        self.plugin.identity.init_identity({'name': 'usage-cache'})
        (self.shared / 'a.bin').write_bytes(b'x' * 512)
        self.assertTrue(self._add()['success'])
        engine = engine_for(self.plugin)
        self.assertIsNotNone(engine)
        engine.verify()

        (self.shared / 'b.bin').write_bytes(b'y' * 1024)
        cached = self.plugin.get_share_roots()[0]
        self.assertEqual(cached['used_bytes'], 512, '首屏不应重新遍历共享根')
        self.assertFalse(cached['usage_stale'])

        self.plugin.shares.usage_cache.clear()
        fresh = self.plugin.shares.share_roots_report()[0]
        self.assertEqual(fresh['used_bytes'], 1536)
        self.assertGreater(fresh['usage_at'], 0)
        again = self.plugin.shares.share_roots_report()[0]
        self.assertEqual(again['used_bytes'], 1536, '算过的用量复用缓存，不重扫')

        self.plugin.shares.derive([
            {'key': 'b.bin', 'path': str(Path(self._root_path('docs')) / 'b.bin')}])
        stale = self.plugin.get_share_roots()[0]
        self.assertIsNone(stale['used_bytes'])
        self.assertIsNone(stale['entries'])
        self.assertTrue(stale['usage_stale'])

    def test_status_carries_usage_errors_and_offline_facts(self):
        """`get_status` 要能被 JSON 序列化，并带上用量与"跑得好不好"。"""
        from shell.backend.freshness import engine_for

        self.plugin.identity.init_identity({'name': 'status'})
        (self.shared / 'a.bin').write_bytes(b'x' * 512)
        self.assertTrue(self._add()['success'])
        engine = engine_for(self.plugin)
        self.assertIsNotNone(engine)
        engine.verify()
        self.plugin.facts.mark_unreachable(('fe80::1', 47810), '连不上: 超时')

        status = self.plugin.report.get_status()
        json.dumps(status, ensure_ascii=False)   # 元组键会让序列化直接失败

        share = status['share_roots'][0]
        self.assertEqual(share['used_bytes'], 512)
        self.assertEqual(share['entries'], 1)
        self.assertFalse(share['usage_stale'])
        self.assertIsInstance(share['usage_at'], int)
        self.assertEqual(status['shares'][0]['used_bytes'], 512)

        self.assertIsInstance(status['sync']['errors'], list)
        self.assertEqual(status['sync']['unreachable'],
                         [{'endpoint': 'fe80::1:47810', 'error': '连不上: 超时'}])


class RemoteApiTest(unittest.TestCase):
    """远端 API 的**离线**行为：参数校验、无注册表时的诚实回报。

    真正跨机的部分由 `shell/groupmesh/tools/lan-test.ps1` / `ipv6-test.ps1` 与
    手动联调覆盖；这里钉住的是"没有对端时也不能假装成功"。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        module = load_plugin_module()
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        config = {'directories': {'data_root': str(self.tmp / 'data')}}
        self.plugin = module.GroupMeshPlugin(manifest, config)
        self.plugin._settings_store = SettingsStore(str(self.tmp / 'settings'))

    def tearDown(self):
        self.plugin.on_unload()
        self._tmp.cleanup()

    def test_list_peers_needs_identity_and_group(self):
        result = self.plugin.peers.list_peers(refresh=False)
        self.assertFalse(result['success'])
        self.assertIn('身份', result['error'])

    def test_list_peers_without_registry_is_empty_and_honest(self):
        self.plugin.identity.init_identity({'name': 'solo'})
        self.plugin.identity.create_group({'group': 'solo-group'})
        result = self.plugin.peers.list_peers(refresh=False)
        self.assertTrue(result['success'])
        # 名单里只有自己：本机不作为"对端"列出
        self.assertEqual(result['peers'], [])
        self.assertEqual(result['registry_size'], 0)

    def test_list_peers_lists_roster_members_without_registration(self):
        """名单里有、注册表里没有的设备也要列出来。

        否则用户会以为"名单里少了个人" —— 而实际只是那台设备没启动过节点。
        """
        owner = self.plugin
        owner.identity.init_identity({'name': 'owner'})
        created = owner.identity.create_group({'group': 'peer-group'})
        self.assertTrue(created['success'])

        member_dir = self.tmp / 'member'
        member_manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        member = load_plugin_module().GroupMeshPlugin(
            member_manifest, {'directories': {'data_root': str(member_dir / 'data')}})
        member._settings_store = SettingsStore(str(member_dir / 'settings'))
        self.addCleanup(member.on_unload)
        member.identity.init_identity({'name': 'member'})
        keys = member.identity.get_device_keys()
        self.assertTrue(owner.identity.add_member({'principal': keys['principal'],
                                          'device': keys['device'], 'name': 'member'})['success'])

        peers = owner.peers.list_peers(refresh=False)['peers']
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]['name'], 'member')
        self.assertEqual(peers[0]['shares'], [])
        self.assertIn('注册记录', peers[0].get('note') or '')

    def test_list_remote_without_any_peer_address_is_explained(self):
        """发现必须先有起点：没有任何对端地址时要说清"去哪登记"，而不是空列表。"""
        self.plugin.identity.init_identity({'name': 'req'})
        self.plugin.identity.create_group({'group': 'req-group'})
        result = self.plugin.remote.list_remote({})
        self.assertFalse(result['success'])
        self.assertIn('对端', result['error'])

    def test_list_remote_unknown_device_needs_an_address(self):
        """注册表里没有这台设备（且没登记过地址）时，错误要给出下一步动作。"""
        self.plugin.identity.init_identity({'name': 'unknown'})
        self.plugin.identity.create_group({'group': 'unknown-group'})
        result = self.plugin.remote.list_remote({'device_id': 'ff' * 32})
        self.assertFalse(result['success'])
        self.assertIn('地址', result['error'])

    def test_list_remote_reports_unreachable_endpoint(self):
        """登记了地址但连不上：必须如实回报离线，不能假装成功。

        用 TEST-NET-1（192.0.2.0/24，RFC 5737 保留给文档用，不会真的有人监听）。
        """
        self.plugin.identity.init_identity({'name': 'offline'})
        self.plugin.identity.create_group({'group': 'offline-group'})
        added = self.plugin.peers.action({'action': 'add', 'endpoint': '192.0.2.1:19443',
                                   'name': 'nowhere'})
        self.assertTrue(added['success'])
        result = self.plugin.remote.list_remote({})
        self.assertFalse(result['success'])
        self.assertTrue(result.get('offline'), result)
        self.assertIn('连不上', result['error'])

    def test_peers_endpoint_parsing(self):
        """IPv6 必须写方括号 —— 裸地址里有多个冒号，按最后一个切会得到假合法结果。"""
        self.plugin.identity.init_identity({'name': 'parse'})
        for bad in ('', 'host-only', 'a:b:c', '127.0.0.1:0', '[::1]'):
            with self.subTest(bad=bad):
                self.assertFalse(self.plugin.peers.action({'action': 'add', 'endpoint': bad})['success'])
        good = self.plugin.peers.action({'action': 'add', 'endpoint': '[2409:8a60::1]:19443'})
        self.assertTrue(good['success'], good)
        self.assertEqual(good['peers'][0]['host'], '2409:8a60::1')
        self.assertEqual(good['peers'][0]['port'], 19443)
        removed = self.plugin.peers.action({'action': 'remove', 'endpoint': '[2409:8a60::1]:19443'})
        self.assertTrue(removed['success'])
        self.assertEqual(removed['removed'], 1)

    def test_download_remote_rejects_traversal_before_connecting(self):
        """对端给的相对路径可能带 `..`：必须在连之前就拒，不能靠对端自觉。"""
        self.plugin.identity.init_identity({'name': 'trav'})
        self.plugin.identity.create_group({'group': 'trav-group'})
        result = self.plugin.remote.download_remote({'device_id': 'ff' * 32, 'share_id': 'x',
                                              'path': '../../escape.bin'})
        self.assertFalse(result['success'])
        self.assertIn('越界', result['error'])

    def test_probe_with_dead_endpoint_respects_overall_budget(self):
        """有一条**一定连不上**的端点时，刷新也要在总预算内返回。

        这是"界面一直停在「正在读取设备」"那条故障的确定性守卫：设备会同时发布
        IPv6 与 IPv4 端点，而 IPv6 在本机常常没有路由 —— 用内核默认的 20 秒超时
        串行试几条，实测就是 80 秒。修复后：单次探测 3 秒、整次刷新总预算 4 秒，
        并且**谁先成功就用谁**。

        用 TEST-NET-1（192.0.2.0/24，RFC 5737 保留给文档用，不会有人真在上面）
        制造"确定连不上"，因此耗时只由超时策略决定，不受网络环境影响。
        """
        import time as _time

        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'probe-budget'})
        self.plugin.identity.create_group({'group': 'budget-group'})
        added = self.plugin.peers.action({'action': 'add', 'endpoint': '192.0.2.11:19443',
                                   'name': 'dead'})
        self.assertTrue(added['success'], added)

        started = _time.monotonic()
        result = self.plugin.peers.list_peers({'refresh': True})
        elapsed = _time.monotonic() - started
        self.assertTrue(result['success'])
        # 预算 4 秒 + 余量；旧实现（20 秒超时、串行）会远超这个值
        self.assertLess(elapsed, 10.0,
                        f'刷新耗时 {elapsed:.1f}s —— 不可达端点被串行等满超时了')
        # 用户刚填的地址连不上，必须如实回报（不能静默）
        self.assertTrue(any('dead' in e.get('device', '') for e in result['errors']),
                        f'手动登记的地址失败应当回报: {result["errors"]}')

    def test_stalled_inbound_connection_does_not_break_stop(self):
        """一个"连上并发了 hello 就不再说话"的对端，不得让停止节点失败或拖慢状态。

        这是线上日志的复现形态：节点起来后，对端在**握手途中**连过来；处理连接原先
        在 accept 循环里同步跑并阻塞在 recv（socket 超时 60 秒），于是循环回不到检查
        停止标志的地方 —— 日志里连续出现"停止节点超时：监听线程没有在 8 秒内退出"，
        端口也一直放不出来。插件层这条用例把它钉住：起节点、制造僵死连接、停止节点，
        必须在超时预算内成功返回且状态如实。
        """
        import socket
        import time as _time

        from shell.groupmesh.transport import exchange_hello

        if not self.plugin.report.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'stalled'})
        self.plugin.identity.create_group({'group': 'stalled-group'})
        self.plugin.update_setting('bind', '127.0.0.1')
        started = self.plugin.node.start()
        self.assertTrue(started['success'], started)
        host, port = started['node']['listening'].rsplit(':', 1)
        port = int(port)

        # 连上并发出 hello（走了协商的第一步），之后就什么也不做
        stalled = socket.create_connection((host, port), timeout=5)
        self.addCleanup(stalled.close)
        exchange_hello(stalled)
        _time.sleep(0.3)

        began = _time.monotonic()
        stopped = self.plugin.node.stop()
        elapsed = _time.monotonic() - began
        self.assertTrue(stopped['success'],
                        f'有僵死连接时停止必须成功，实际: {stopped.get("error")}')
        self.assertLess(elapsed, 8.0,
                        f'停止耗时 {elapsed:.1f}s —— accept 循环被僵死连接占住了')
        self.assertFalse(stopped['node']['running'])

        # 停完必须能立刻重新起（端口真的释放了）
        again = self.plugin.node.start()
        self.assertTrue(again['success'], f'停止后应当能重新启动: {again.get("error")}')

    def test_download_remote_requires_share_and_path(self):
        self.plugin.identity.init_identity({'name': 'needargs'})
        self.plugin.identity.create_group({'group': 'needargs-group'})
        result = self.plugin.remote.download_remote({'device_id': 'ff' * 32})
        self.assertFalse(result['success'])
        self.assertIn('share_id', result['error'])

    # ── 上传：本地校验必须在连接之前完成 ────────────────────────────────────
    #
    # 这一组全部离线可测：被拒的理由都是本机就能判定的（缺参数、本地文件不存在、
    # 来源不该外传、目标路径越界），不该因为"设备不可达"而改变结论。

    def test_upload_remote_requires_share_and_local_path(self):
        self.plugin.identity.init_identity({'name': 'up-needargs'})
        self.plugin.identity.create_group({'group': 'up-group'})
        result = self.plugin.upload.upload_remote({'device_id': 'ff' * 32})
        self.assertFalse(result['success'])
        self.assertIn('share_id', result['error'])
        result = self.plugin.upload.upload_remote({'device_id': 'ff' * 32, 'share_id': 'x'})
        self.assertFalse(result['success'])
        self.assertIn('本机文件', result['error'])

    def test_upload_remote_rejects_missing_local_file(self):
        self.plugin.identity.init_identity({'name': 'up-missing'})
        self.plugin.identity.create_group({'group': 'up-group'})
        result = self.plugin.upload.upload_remote({'device_id': 'ff' * 32, 'share_id': 'x',
                                            'local_path': str(self.tmp / 'nope.bin')})
        self.assertFalse(result['success'])
        self.assertIn('不存在', result['error'])

    def test_upload_remote_rejects_target_traversal(self):
        """目标相对路径带 `..` 或绝对路径必须在连之前拒掉。"""
        self.plugin.identity.init_identity({'name': 'up-trav'})
        self.plugin.identity.create_group({'group': 'up-group'})
        source = self.tmp / 'src.bin'
        source.write_bytes(b'x')
        for bad in ('../../escape.bin', '/etc/passwd', 'sub/../../escape.bin', 'sub/'):
            result = self.plugin.upload.upload_remote({'device_id': 'ff' * 32, 'share_id': 'x',
                                                'local_path': str(source),
                                                'remote_path': bad})
            self.assertFalse(result['success'], f'{bad} 竟被接受')
            self.assertIn('非法', result['error'])

    def test_upload_remote_refuses_identity_and_cache_sources(self):
        """身份目录（明文私钥）与远端物化缓存（别人的数据）都不许传出去。"""
        self.plugin.identity.init_identity({'name': 'up-guard'})
        self.plugin.identity.create_group({'group': 'up-group'})
        secret = self.plugin.storage.identity_dir / 'principal.json'
        self.assertTrue(secret.is_file(), '前置条件：身份文件存在')
        result = self.plugin.upload.upload_remote({'device_id': 'ff' * 32, 'share_id': 'x',
                                            'local_path': str(secret)})
        self.assertFalse(result['success'])
        self.assertIn('不允许上传', result['error'])

        cached = self.plugin.storage.remote_cache_dir / ('aa' * 16) / 'share' / 'x.bin'
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(b'other member data')
        result = self.plugin.upload.upload_remote({'device_id': 'ff' * 32, 'share_id': 'x',
                                            'local_path': str(cached)})
        self.assertFalse(result['success'])
        self.assertIn('不允许上传', result['error'])


class AutoDiscoveryTest(unittest.TestCase):
    """有身份与团体时节点应当**自己跑起来并发布注册记录**。

    为什么这条必须有用例：节点不跑 → 不发布注册记录 → 其他成员永远发现不了本机。
    而"要先去点一次启动节点"这件事没有任何提示，表现出来就是"明明都在同一个团体
    里却看不到对方"—— 也就是把自动发现退化成了手动配置。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        module = load_plugin_module()
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        config = {'directories': {'data_root': str(self.tmp / 'data')}}
        self.plugin = module.GroupMeshPlugin(manifest, config)
        self.plugin._settings_store = SettingsStore(str(self.tmp / 'settings'))
        # 端口 0 = 让内核分配空闲端口，避免用例撞上真实占用的 19443
        self.plugin.update_setting('port', 0)
        self.plugin.update_setting('bind', '127.0.0.1')

    def tearDown(self):
        self.plugin.on_unload()
        self._tmp.cleanup()

    def _ready(self):
        return self.plugin.report.get_status()['kernel']['available']

    def test_node_does_not_start_before_identity_and_group(self):
        """没有身份/团体时不启动：那种状态下节点无法认证对端，起来只会报错。"""
        if not self._ready():
            self.skipTest('协议内核不可用')
        status = self.plugin.report.get_status()
        self.assertFalse(status['node']['running'])
        self.assertIsNone(status['node']['auto_start_error'])

    def test_node_auto_starts_and_publishes_registration(self):
        if not self._ready():
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'auto'})
        self.plugin.identity.create_group({'group': 'auto-group'})

        status = self.plugin.report.get_status()
        node = status['node']
        self.assertTrue(node['running'], f'有身份与团体后节点应自动启动: {node}')
        self.assertIsNone(node['auto_start_error'], node)
        # 关键：节点起来还不够，必须真的把注册记录发布出去（别人靠它发现本机）
        self.assertTrue(node['published'], f'节点应发布注册记录: {node}')
        self.assertIsInstance(node['published_seq'], int)

        state_file = self.plugin.storage.identity_dir / 'state.json'
        self.assertTrue(state_file.is_file(), '发布记录后应留下 state.json（seq 单调性依据）')
        state = json.loads(state_file.read_text(encoding='utf-8'))
        self.assertEqual(state['registration_seq'], node['published_seq'])
        self.assertTrue(state.get('endpoints'), 'state 里应记住本次发布的端点集合')

        registry = json.loads((self.plugin.storage.identity_dir / 'registry.json')
                              .read_text(encoding='utf-8'))
        self.assertEqual(len(registry['records']), 1, '注册表里应有本机那条记录')
        record = registry['records'][0]
        self.assertEqual(record['seq'], node['published_seq'])
        # 端点必须是内核真正绑上的端口（设置里写 0 时不能发布 0）
        self.assertNotEqual(record['endpoints'][0][1], 0)

    def test_auto_start_is_attempted_only_once(self):
        """重复取状态不得反复起线程（失败时尤其不能变成死循环重试）。"""
        if not self._ready():
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'once'})
        self.plugin.identity.create_group({'group': 'once-group'})
        first = self.plugin.report.get_status()['node']
        self.assertTrue(first['running'])
        thread = self.plugin.node._node_thread
        for _ in range(3):
            again = self.plugin.report.get_status()['node']
            self.assertTrue(again['running'])
        self.assertIs(self.plugin.node._node_thread, thread, '不应重建节点线程')

    def test_all_published_endpoints_are_tried(self):
        """设备发布了多个端点时必须逐个试，不能在第一个失败后放弃。

        IPv4 是可达性兜底，因此设备常同时发布 IPv6 与 IPv4 端点；
        只试首选那个的话，IPv6 没路由就会把一台其实能连的设备判成离线。
        """
        if not self._ready():
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'multi-ep'})
        self.plugin.identity.create_group({'group': 'multi-ep-group'})
        # 取一次状态让节点自启并载入注册表对象（_registry 在此之前是 None）
        self.assertTrue(self.plugin.report.get_status()['node']['running'])

        # 造一条"对端设备"的注册记录：两个端点，第一个一定连不上
        cp = sys.modules['shell.groupmesh.crypto_prims']
        registry_mod = sys.modules['shell.groupmesh.registry']
        peer_priv, peer_pub = cp.generate_sign_keypair()
        endpoints = [('2001:db8::bad', 19443), ('192.0.2.7', 19443)]
        self.plugin.registry_store.registry.add(registry_mod.new_registration(
            device_key=peer_pub, device_private_key=peer_priv,
            seq=1, endpoints=endpoints, shares=['pub']))
        # 让这台设备出现在名单里（list_peers 只列名单内成员）
        me = self.plugin.storage.load_identity()
        self.plugin.identity.add_member({'principal': base64.b64encode(me.principal.public_key).decode(),
                                'device': base64.b64encode(peer_pub).decode(), 'name': 'peer'})

        attempted: list = []
        # 只观察"对端设备"的端点：自举那一步也会调 _fetch_registry_from（用本机自己
        # 发布的地址），把它算进来会让顺序断言失去意义。
        peer_attempts: list = []

        def fake_fetch(endpoint, identity, roster):
            attempted.append(endpoint)
            if endpoint[1] == 19443:          # 本用例里对端的端口
                peer_attempts.append(endpoint)
                if len(peer_attempts) == 1:
                    raise TransportError('第一个端点刻意失败')
            return 1

        self.plugin.discovery.fetch_registry_from = fake_fetch
        result = self.plugin.peers.list_peers({'refresh': True})
        self.assertTrue(result['success'])
        self.assertEqual(len(peer_attempts), 2,
                         f'对端的两个端点都该被尝试，实际: {peer_attempts}')
        self.assertEqual(peer_attempts[0][0], '2001:db8::bad',
                         'IPv6 应当优先尝试（主路径）')
        self.assertFalse(result['errors'], f'第二个端点成功后不该留下错误: {result["errors"]}')

    def test_endpoint_change_republishes_registration(self):
        """本机地址集合变化时必须递增 seq 重新发布。

        不重发的后果：对端留着旧端点，表现为"两边都开着却连不上"，且没有提示。
        """
        if not self._ready():
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'republish'})
        self.plugin.identity.create_group({'group': 'republish-group'})
        first_seq = self.plugin.report.get_status()['node']['published_seq']
        self.assertIsNotNone(first_seq)

        # 模拟"地址变了"：把 state 里记的端点改成别的
        state_file = self.plugin.storage.identity_dir / 'state.json'
        state = json.loads(state_file.read_text(encoding='utf-8'))
        state['endpoints'] = [['10.0.0.99', 1]]
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')

        self.plugin.peers.list_peers(refresh=True)
        new_seq = self.plugin.report.get_status()['node']['published_seq']
        self.assertGreater(new_seq, first_seq, '端点变化后应重新发布并递增 seq')

    def test_unchanged_endpoints_do_not_republish(self):
        """端点没变时不做无谓重发（否则每次刷新都递增 seq，对端反复合并）。"""
        if not self._ready():
            self.skipTest('协议内核不可用')
        self.plugin.identity.init_identity({'name': 'stable'})
        self.plugin.identity.create_group({'group': 'stable-group'})
        first_seq = self.plugin.report.get_status()['node']['published_seq']
        self.plugin.peers.list_peers(refresh=True)
        self.assertEqual(self.plugin.report.get_status()['node']['published_seq'], first_seq)


class SyncStateTest(unittest.TestCase):
    """后台同步的"变更标记 + push"逻辑。

    不启动后台线程，也不连任何真实对端：这里只验证"名单/注册变更一定会被标记，
    并进入待推送队列"，网络轮询由 `BackgroundSyncThreadTest` 与多实例夹具覆盖。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        module = load_plugin_module()
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        config = {'directories': {'data_root': str(self.tmp / 'data')}}
        self.plugin = module.GroupMeshPlugin(manifest, config)
        self.plugin._settings_store = SettingsStore(str(self.tmp / 'settings'))

    def tearDown(self):
        self.plugin.on_unload()
        self._tmp.cleanup()

    def test_sync_interval_setting_is_reported_and_bounded(self):
        status = self.plugin.report.get_status()
        self.assertEqual(status['settings']['sync_interval_seconds'], 60)
        self.assertTrue(status['sync']['running'] is False)  # 没走 on_load 时不启线程
        self.plugin.update_setting('sync_interval_seconds', 5)
        self.assertEqual(self.plugin.get_sync_status()['interval'], 5)
        self.plugin.update_setting('sync_interval_seconds', 99999)
        self.assertEqual(self.plugin.get_sync_status()['interval'], 3600)

    def test_roster_change_marks_roster_dirty(self):
        self.plugin.identity.init_identity({'name': 'sync-roster'})
        self.plugin.pulse.clear(roster=True, registry=True)
        self.plugin.pulse.wake_event.clear()

        self.plugin.identity.create_group({'group': 'sync-group'})

        sync = self.plugin.get_sync_status()
        self.assertTrue(sync['roster_dirty'], '建团/改名单后应标记待推送')
        self.assertTrue(self.plugin.pulse.wake_event.is_set(), '应立即唤醒后台循环')

    def test_share_change_marks_registry_dirty(self):
        self.plugin.identity.init_identity({'name': 'sync-share'})
        self.plugin.identity.create_group({'group': 'sync-share-group'})
        self.plugin.pulse.clear(roster=True, registry=True)
        self.plugin.pulse.wake_event.clear()

        shared = self.tmp / 'shared'
        shared.mkdir()
        result = self.plugin.shares.add_share({'share_id': 'docs', 'path': str(shared)})
        self.assertTrue(result['success'], result)

        self.assertTrue(self.plugin.get_sync_status()['registry_dirty'],
                        '共享清单变化后应重发注册记录并标记待推送')
        self.assertTrue(self.plugin.pulse.wake_event.is_set())

    def test_push_roster_sends_and_clears_dirty(self):
        self.plugin.identity.init_identity({'name': 'sync-push'})
        self.plugin.identity.create_group({'group': 'sync-push-group'})
        identity = self.plugin.storage.load_identity()
        roster = self.plugin.storage.load_roster()
        self.assertIsNotNone(identity)
        self.assertIsNotNone(roster)
        self.plugin.pulse.clear(roster=True, registry=True)
        self.plugin.pulse.wake_event.clear()
        self.plugin.pulse.request(roster_dirty=True)

        endpoint = ('203.0.113.9', 19443)
        calls = []

        def fake_push(connection, pushed_roster, history=None):
            calls.append((connection, pushed_roster.version))
            return {'status': 'ok', 'version': pushed_roster.version}

        fake_connection = mock.Mock()
        with mock.patch.object(self.plugin.connections, 'open_dedicated',
                               return_value=fake_connection), \
                mock.patch.object(self.plugin.storage, 'load_roster_history', return_value=[]), \
                mock.patch('shell.groupmesh.client.push_roster', side_effect=fake_push):
            self.plugin.discovery.push_pending_changes(identity, roster, [(endpoint, 'peer')])

        self.assertEqual(len(calls), 1, '应把名单推给对端')
        self.assertEqual(calls[0][1], roster.version)
        self.assertFalse(self.plugin.get_sync_status()['roster_dirty'])
        self.assertEqual(self.plugin.facts.roster_notes[endpoint]['version'], roster.version)


class BackgroundSyncThreadTest(unittest.TestCase):
    """后台同步线程随 on_load / on_unload 启停。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        module = load_plugin_module()
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        config = {'directories': {'data_root': str(self.tmp / 'data')}}
        self.plugin = module.GroupMeshPlugin(manifest, config)
        self.plugin._settings_store = SettingsStore(str(self.tmp / 'settings'))
        # 拉长间隔，确保测试只验证"线程在跑/能停"，不触发网络轮询。
        self.plugin.update_setting('sync_interval_seconds', 3600)
        self.plugin.on_load()

    def tearDown(self):
        self.plugin.on_unload()
        self._tmp.cleanup()

    def test_sync_thread_starts_and_stops_with_lifecycle(self):
        status = self.plugin.get_sync_status()
        self.assertTrue(status['running'])
        self.assertIsNotNone(self.plugin.discovery._thread)
        self.plugin.on_unload()
        self.assertIsNone(self.plugin.discovery._thread)
        self.assertFalse(self.plugin.get_sync_status()['running'])


class ApiTableTest(unittest.TestCase):
    """`register_api` 的键、顺序与绑定对象在组件化之后必须逐字不变。

    迁移把接口实现从"入口类上的方法"移到了组件，`register_api` 从"绑定自己"变成
    "绑定组件"。键名或绑定对象写错时，前端经 `Bridge.call` 打到的是 404 或错误方法，
    而**没有任何静态检查能发现**（表是运行时字典、键是字符串）。迁移前这条路是被隐式
    覆盖的（用例直接调实例方法）；迁移后必须单独钉住。

    同一张表锁住入口上必须留着的壳钩子：它们不经 `register_api`，由壳按名字取，
    漏一个不会报错，只会静默退化（占位文件永远不取回、统一刷新不参与）。
    """

    #: 迁移前的键与顺序（逐字固定）：前端与壳按名字调用，改名即静默 404。
    EXPECTED_KEYS: ClassVar[List[str]] = [
        'get_status', 'init_identity', 'get_device_keys', 'create_group', 'join_group',
        'get_invite', 'add_member', 'add_share', 'remove_share', 'refresh_share_roots',
        'peers', 'my_endpoint', 'list_peers', 'list_remote', 'download_remote',
        'upload_remote', 'upload_status', 'cancel_upload', 'materialize_remote',
        'mirror_share', 'remote_cache', 'clear_remote_cache', 'start_node', 'stop_node',
        'get_node_status', 'get_settings', 'save_settings',
    ]

    #: 键 -> 入口上承载它的组件属性名。`get_settings` / `save_settings` 留在入口
    #: （基类的脱敏与 schema 校验），因此不在这张表里。
    EXPECTED_OWNER: ClassVar[Dict[str, str]] = {
        'get_status': 'report',
        'init_identity': 'identity',
        'get_device_keys': 'identity',
        'create_group': 'identity',
        'join_group': 'identity',
        'get_invite': 'identity',
        'add_member': 'identity',
        'add_share': 'shares',
        'remove_share': 'shares',
        'refresh_share_roots': 'shares',
        'peers': 'peers',
        'my_endpoint': 'peers',
        'list_peers': 'peers',
        'list_remote': 'remote',
        'download_remote': 'remote',
        'upload_remote': 'upload',
        'upload_status': 'uploads',
        'cancel_upload': 'uploads',
        'materialize_remote': 'materialize',
        'mirror_share': 'mirror',
        'remote_cache': 'fetch',
        'clear_remote_cache': 'fetch',
        'start_node': 'node',
        'stop_node': 'node',
        'get_node_status': 'node',
    }

    #: 壳按名从实例上取、又不经 `register_api` 的成员。
    SHELL_HOOKS: ClassVar[tuple] = ('get_data_root', 'get_protected_paths', 'get_extensions',
                   'freshness_spec', 'is_content_placeholder', 'ensure_file',
                   'on_settings_changed', 'on_load', 'on_unload')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        module = load_plugin_module()
        manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        config = {'directories': {'data_root': str(self.tmp / 'data')}}
        self.plugin = module.GroupMeshPlugin(manifest, config)
        self.api = self.plugin.register_api()

    def tearDown(self):
        self.plugin.on_unload()
        self._tmp.cleanup()

    def test_keys_and_order_are_unchanged(self):
        self.assertEqual(list(self.api), self.EXPECTED_KEYS,
                         'register_api 的键或顺序变了：前端与壳按名字调用，改名即 404')

    def test_every_entry_is_callable(self):
        for key, value in self.api.items():
            self.assertTrue(callable(value), f'{key} 绑定的对象不可调用: {value!r}')

    def test_entries_are_bound_to_their_component(self):
        for key, component in self.EXPECTED_OWNER.items():
            owner = self.api[key].__self__
            self.assertIs(owner, getattr(self.plugin, component),
                          f'{key} 应绑定到 {component}，实际是 {owner!r}')

    def test_shell_hooks_stay_on_the_entry_class(self):
        for name in self.SHELL_HOOKS:
            self.assertTrue(callable(getattr(self.plugin, name, None)),
                            f'入口类缺少壳按名调用的成员 {name}')
        # 路径访问器同样按实例读取（壳与用例都按名字取，不经过 API 表）。
        self.assertEqual(self.plugin.get_data_root(), self.plugin.storage.root)
        self.assertEqual(self.plugin.cache_dir, self.plugin.storage.cache_dir)
        self.assertEqual(self.plugin.downloads_dir, self.plugin.storage.downloads_dir)
        self.assertIn(self.plugin.storage.identity_dir,
                      [Path(p) for p in self.plugin.get_protected_paths()])

    def test_entry_does_not_override_base_get_settings(self):
        """入口不能覆盖基类的无参 `get_settings()`：那会改变它的签名与脱敏语义。"""
        self.assertIs(type(self.plugin).get_settings, PluginBase.get_settings,
                      '入口覆盖了 PluginBase.get_settings()：基类的脱敏与默认值语义会失效')
        self.assertEqual(self.plugin.get_settings().get('port'),
                         self.plugin.settings_schema[0]['default'])


if __name__ == '__main__':
    unittest.main()
