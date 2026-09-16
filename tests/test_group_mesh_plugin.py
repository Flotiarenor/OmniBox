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
import unittest
from pathlib import Path

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

    def test_remote_api_surface(self):
        """远端浏览/取回必须真的挂在 register_api 上。

        在此之前内核已有 `client.list_shares / list_directory / fetch_to_file`，
        但插件一条都没暴露 —— 于是"跨机联调通过"与"界面里拉不回一个文件"并存。
        """
        api = set(self.plugin.register_api())
        for name in ('list_peers', 'list_remote', 'download_remote', 'refresh_share_roots'):
            self.assertIn(name, api)

    def test_identity_dir_is_protected(self):
        """§4.1：设备私钥不导出。插件至少必须申报身份目录，禁止文件服务端出去。"""
        protected = [Path(p) for p in self.plugin.get_protected_paths()]
        self.assertIn(self.plugin.identity_dir.resolve(),
                      [p.resolve() for p in protected])

    def test_settings_schema_shape(self):
        keys = {item['key'] for item in self.plugin.settings_schema}
        self.assertEqual(keys, {'port', 'bind', 'group_name', 'principal_name',
                                'ttl_days', 'download_dir', 'max_fetch_mb'})
        for item in self.plugin.settings_schema:
            self.assertIn('label', item)
            self.assertIn('type', item)
        # 下载目录必须由 Shell 的目录选择器渲染（`directory` 类型），
        # 否则用户又得手敲一个绝对路径。
        download = next(i for i in self.plugin.settings_schema if i['key'] == 'download_dir')
        self.assertEqual(download['type'], 'directory')

    def test_status_before_identity(self):
        status = self.plugin.get_status()
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
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        result = self.plugin.init_identity({'name': 'unit-test'})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['identity']['name'], 'unit-test')

        # 重复 init 必须被拒（不能静默覆盖私钥）
        again = self.plugin.init_identity({'name': 'other'})
        self.assertFalse(again['success'])

        created = self.plugin.create_group({'group': 'unit-group'})
        self.assertTrue(created['success'], created)
        self.assertEqual(created['group'], 'unit-group')
        self.assertTrue(created['invite'].startswith('gm1:'))

        status = self.plugin.get_status()
        self.assertEqual(status['roster']['role'], 'owner')
        self.assertTrue(status['roster']['in_roster'])

    def test_positional_and_structured_arguments_agree(self):
        """两种调用形态都必须被接受（`/api` 会展开 args/kwargs）。"""
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        positional = self.plugin.init_identity(None, 'positional-name')
        self.assertTrue(positional['success'], positional)

    def test_get_device_keys(self):
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'keys'})
        keys = self.plugin.get_device_keys()
        self.assertTrue(keys['success'])
        self.assertTrue(keys['principal'])
        self.assertTrue(keys['device'])
        # 主体公钥与设备公钥必须是不同的两把（用途分离）
        self.assertNotEqual(keys['principal'], keys['device'])

    def test_share_root_must_exist(self):
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'shares'})
        result = self.plugin.add_share({'share_id': 'x', 'path': str(self.tmp / 'nope')})
        self.assertFalse(result['success'])

    def test_share_root_cannot_be_inside_data_root(self):
        """§6.5：共享目录必须与程序数据、身份目录分离。"""
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'guard'})
        inside = self.plugin.get_data_root()
        inside.mkdir(parents=True, exist_ok=True)
        result = self.plugin.add_share({'share_id': 'bad', 'path': str(inside)})
        self.assertFalse(result['success'])
        self.assertIn('不得位于', result['error'])

    def test_add_and_remove_share(self):
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'share-ok'})
        shared = self.tmp / 'shared'
        shared.mkdir()
        (shared / 'file.txt').write_text('hi', encoding='utf-8')

        added = self.plugin.add_share({'share_id': 'docs', 'path': str(shared),
                                       'read': 'group', 'write': 'owner', 'delete': 'owner'})
        self.assertTrue(added['success'], added)
        self.assertEqual(len(self.plugin.get_status()['shares']), 1)

        # 共享项声明必须能验签（内核在读取时会复核）
        reloaded = self.plugin._load_shares()
        self.assertTrue(reloaded['docs'].declaration.verify_signature())

        removed = self.plugin.remove_share({'share_id': 'docs'})
        self.assertTrue(removed['success'])
        self.assertEqual(self.plugin.get_status()['shares'], [])

    def test_bad_share_id_rejected(self):
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'bad-id'})
        shared = self.tmp / 'shared2'
        shared.mkdir()
        result = self.plugin.add_share({'share_id': '../evil', 'path': str(shared)})
        self.assertFalse(result['success'])

    def test_node_cannot_start_without_group(self):
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'no-group'})
        result = self.plugin.start_node()
        self.assertFalse(result['success'])
        self.assertIn('团体', result['error'])

    def test_node_start_and_stop(self):
        """真实监听一个随机端口，确认启停与状态回报。"""
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'node'})
        self.plugin.create_group({'group': 'node-group'})
        self.assertTrue(self.plugin.update_setting('bind', '127.0.0.1'))
        # port=0 让内核分配空闲端口，避免测试撞上真实占用的 19443
        self.assertTrue(self.plugin.update_setting('port', 0))

        started = self.plugin.start_node()
        self.assertTrue(started['success'], started)
        self.assertTrue(started['node']['running'])
        self.assertIsNotNone(started['node']['listening'])
        self.assertNotIn(':0', started['node']['listening'] or ':0',
                         '监听地址应回报内核实际绑定的端口，而不是设置里的 0')

        stopped = self.plugin.stop_node()
        self.assertTrue(stopped['success'])
        self.assertFalse(stopped['node']['running'])

    def test_join_group_rejects_bad_invite(self):
        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'joiner'})
        for bad in ('', 'not-an-invite', 'gm1:!!!!'):
            with self.subTest(bad=bad):
                result = self.plugin.join_group({'invite': bad})
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
        if not self._plugin('probe').get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner')
        member = self._plugin('member')
        owner.init_identity({'name': 'owner'})
        member.init_identity({'name': 'member'})

        # 群主创建团体（v1），成员用邀请串加入
        created = owner.create_group({'group': 'dist-group'})
        self.assertTrue(created['success'], created)
        joined = member.join_group({'invite': created['invite']})
        self.assertTrue(joined['success'], joined)
        self.assertFalse(joined['updated'], '首次加入不应标记为更新')
        self.assertEqual(joined['version'], 1)
        # v1 里还没有 member 本人，属于预期（群主还没登记它）
        self.assertFalse(joined['in_roster'])

        # 群主登记成员设备 -> 签发 v2
        member_keys = member.get_device_keys()
        added = owner.add_member({'principal': member_keys['principal'],
                                  'device': member_keys['device'],
                                  'name': 'member'})
        self.assertTrue(added['success'], added)
        self.assertEqual(added['version'], 2)

        # 成员侧此前看不到自己；拿到 v2 邀请串后才能对上
        before = member.get_status()
        self.assertFalse(before['roster']['in_roster'])
        self.assertEqual(before['roster']['version'], 1)

        updated = member.join_group({'invite': added['invite']})
        self.assertTrue(updated['success'], updated)
        self.assertTrue(updated['updated'], '应标记为"更新"而不是"加入"')
        self.assertEqual(updated['previous_version'], 1)
        self.assertEqual(updated['version'], 2)
        self.assertTrue(updated['in_roster'], 'v2 之后成员应能在名单里看到自己')
        self.assertEqual(updated['role'], 'member')

        after = member.get_status()
        self.assertEqual(after['roster']['version'], 2)
        self.assertTrue(after['roster']['in_roster'])
        self.assertEqual(after['roster']['member_count'], 2)

    def test_stale_invite_is_rejected_with_a_useful_message(self):
        """给出比本机更旧的邀请串时，要明确说明"本机已是 vN"。"""
        if not self._plugin('probe2').get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner2')
        member = self._plugin('member2')
        owner.init_identity({'name': 'owner'})
        member.init_identity({'name': 'member'})

        v1_invite = owner.create_group({'group': 'stale-group'})['invite']
        self.assertTrue(member.join_group({'invite': v1_invite})['success'])

        keys = member.get_device_keys()
        v2_invite = owner.add_member({'principal': keys['principal'],
                                      'device': keys['device'], 'name': 'member'})['invite']
        self.assertTrue(member.join_group({'invite': v2_invite})['success'])

        # 再贴一次 v1：版本未前进，必须被拒且提示里带上版本号
        again = member.join_group({'invite': v1_invite})
        self.assertFalse(again['success'])
        self.assertIn('本机已是 v2', again['error'])

    def test_roster_update_requires_direct_successor(self):
        """跳版（v1 -> v3）必须被拒：规则 2 要求 prev 指向当前名单哈希。"""
        if not self._plugin('probe3').get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')

        owner = self._plugin('owner3')
        member = self._plugin('member3')
        owner.init_identity({'name': 'owner'})
        member.init_identity({'name': 'member'})
        self.assertTrue(member.join_group(
            {'invite': owner.create_group({'group': 'chain-group'})['invite']})['success'])

        keys = member.get_device_keys()
        owner.add_member({'principal': keys['principal'], 'device': keys['device'],
                          'name': 'member'})
        # 群主再加一个成员 -> v3，跳过 v2
        other = self._plugin('other3')
        other.init_identity({'name': 'other'})
        other_keys = other.get_device_keys()
        v3 = owner.add_member({'principal': other_keys['principal'],
                               'device': other_keys['device'], 'name': 'other'})
        self.assertEqual(v3['version'], 3)

        skipped = member.join_group({'invite': v3['invite']})
        self.assertFalse(skipped['success'], '跳版名单不应被接受')
        self.assertIn('prev', skipped['error'])


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
        return self.plugin.add_share({'share_id': share_id, 'path': str(self.shared),
                                      'read': 'group', 'write': 'owner', 'delete': 'owner'})

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
        self.plugin.init_identity({'name': 'loc'})
        self.assertTrue(self._add()['success'])
        roots_file = self.plugin.identity_dir / 'share_roots.json'
        self.assertTrue(roots_file.is_file(), '位置应写进独立的 share_roots.json')
        roots = json.loads(roots_file.read_text(encoding='utf-8'))['roots']
        self.assertSamePath(roots['docs']['path'], self.shared)
        # 声明文件里不应再持有路径（那是本机事实，不该进签名对象）
        shares = json.loads((self.plugin.identity_dir / 'shares.json')
                            .read_text(encoding='utf-8'))['shares']
        self.assertIn('declaration', shares['docs'])
        self.assertEqual(shares['docs']['declaration']['acl']['read'], 'group')

    def test_location_reports_missing_directory(self):
        """共享根所在磁盘未接入时必须报"不可用"，而不是让对端看到空目录。"""
        self.plugin.init_identity({'name': 'missing'})
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
        self.plugin.init_identity({'name': 'usage'})
        (self.shared / 'a.bin').write_bytes(b'x' * 1000)
        self.assertTrue(self._add()['success'])
        result = self.plugin.refresh_share_roots()
        self.assertTrue(result['success'])
        item = result['roots'][0]
        self.assertTrue(item['available'])
        self.assertEqual(item['used_bytes'], 1000)
        self.assertEqual(item['entries'], 1)


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
        result = self.plugin.list_peers(refresh=False)
        self.assertFalse(result['success'])
        self.assertIn('身份', result['error'])

    def test_list_peers_without_registry_is_empty_and_honest(self):
        self.plugin.init_identity({'name': 'solo'})
        self.plugin.create_group({'group': 'solo-group'})
        result = self.plugin.list_peers(refresh=False)
        self.assertTrue(result['success'])
        # 名单里只有自己：本机不作为"对端"列出
        self.assertEqual(result['peers'], [])
        self.assertEqual(result['registry_size'], 0)

    def test_list_peers_lists_roster_members_without_registration(self):
        """名单里有、注册表里没有的设备也要列出来。

        否则用户会以为"名单里少了个人" —— 而实际只是那台设备没启动过节点。
        """
        owner = self.plugin
        owner.init_identity({'name': 'owner'})
        created = owner.create_group({'group': 'peer-group'})
        self.assertTrue(created['success'])

        member_dir = self.tmp / 'member'
        member_manifest = json.loads((PLUGIN_DIR / 'manifest.json').read_text(encoding='utf-8'))
        member = load_plugin_module().GroupMeshPlugin(
            member_manifest, {'directories': {'data_root': str(member_dir / 'data')}})
        member._settings_store = SettingsStore(str(member_dir / 'settings'))
        self.addCleanup(member.on_unload)
        member.init_identity({'name': 'member'})
        keys = member.get_device_keys()
        self.assertTrue(owner.add_member({'principal': keys['principal'],
                                          'device': keys['device'], 'name': 'member'})['success'])

        peers = owner.list_peers(refresh=False)['peers']
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]['name'], 'member')
        self.assertEqual(peers[0]['shares'], [])
        self.assertIn('注册记录', peers[0].get('note') or '')

    def test_list_remote_without_any_peer_address_is_explained(self):
        """发现必须先有起点：没有任何对端地址时要说清"去哪登记"，而不是空列表。"""
        self.plugin.init_identity({'name': 'req'})
        self.plugin.create_group({'group': 'req-group'})
        result = self.plugin.list_remote({})
        self.assertFalse(result['success'])
        self.assertIn('对端', result['error'])

    def test_list_remote_unknown_device_needs_an_address(self):
        """注册表里没有这台设备（且没登记过地址）时，错误要给出下一步动作。"""
        self.plugin.init_identity({'name': 'unknown'})
        self.plugin.create_group({'group': 'unknown-group'})
        result = self.plugin.list_remote({'device_id': 'ff' * 32})
        self.assertFalse(result['success'])
        self.assertIn('地址', result['error'])

    def test_list_remote_reports_unreachable_endpoint(self):
        """登记了地址但连不上：必须如实回报离线，不能假装成功。

        用 TEST-NET-1（192.0.2.0/24，RFC 5737 保留给文档用，不会真的有人监听）。
        """
        self.plugin.init_identity({'name': 'offline'})
        self.plugin.create_group({'group': 'offline-group'})
        added = self.plugin.peers({'action': 'add', 'endpoint': '192.0.2.1:19443',
                                   'name': 'nowhere'})
        self.assertTrue(added['success'])
        result = self.plugin.list_remote({})
        self.assertFalse(result['success'])
        self.assertTrue(result.get('offline'), result)
        self.assertIn('连不上', result['error'])

    def test_peers_endpoint_parsing(self):
        """IPv6 必须写方括号 —— 裸地址里有多个冒号，按最后一个切会得到假合法结果。"""
        self.plugin.init_identity({'name': 'parse'})
        for bad in ('', 'host-only', 'a:b:c', '127.0.0.1:0', '[::1]'):
            with self.subTest(bad=bad):
                self.assertFalse(self.plugin.peers({'action': 'add', 'endpoint': bad})['success'])
        good = self.plugin.peers({'action': 'add', 'endpoint': '[2409:8a60::1]:19443'})
        self.assertTrue(good['success'], good)
        self.assertEqual(good['peers'][0]['host'], '2409:8a60::1')
        self.assertEqual(good['peers'][0]['port'], 19443)
        removed = self.plugin.peers({'action': 'remove', 'endpoint': '[2409:8a60::1]:19443'})
        self.assertTrue(removed['success'])
        self.assertEqual(removed['removed'], 1)

    def test_download_remote_rejects_traversal_before_connecting(self):
        """对端给的相对路径可能带 `..`：必须在连之前就拒，不能靠对端自觉。"""
        self.plugin.init_identity({'name': 'trav'})
        self.plugin.create_group({'group': 'trav-group'})
        result = self.plugin.download_remote({'device_id': 'ff' * 32, 'share_id': 'x',
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

        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'probe-budget'})
        self.plugin.create_group({'group': 'budget-group'})
        added = self.plugin.peers({'action': 'add', 'endpoint': '192.0.2.11:19443',
                                   'name': 'dead'})
        self.assertTrue(added['success'], added)

        started = _time.monotonic()
        result = self.plugin.list_peers({'refresh': True})
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

        if not self.plugin.get_status()['kernel']['available']:
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'stalled'})
        self.plugin.create_group({'group': 'stalled-group'})
        self.plugin.update_setting('bind', '127.0.0.1')
        started = self.plugin.start_node()
        self.assertTrue(started['success'], started)
        host, port = started['node']['listening'].rsplit(':', 1)
        port = int(port)

        # 连上并发出 hello（走了协商的第一步），之后就什么也不做
        stalled = socket.create_connection((host, port), timeout=5)
        self.addCleanup(stalled.close)
        exchange_hello(stalled)
        _time.sleep(0.3)

        began = _time.monotonic()
        stopped = self.plugin.stop_node()
        elapsed = _time.monotonic() - began
        self.assertTrue(stopped['success'],
                        f'有僵死连接时停止必须成功，实际: {stopped.get("error")}')
        self.assertLess(elapsed, 8.0,
                        f'停止耗时 {elapsed:.1f}s —— accept 循环被僵死连接占住了')
        self.assertFalse(stopped['node']['running'])

        # 停完必须能立刻重新起（端口真的释放了）
        again = self.plugin.start_node()
        self.assertTrue(again['success'], f'停止后应当能重新启动: {again.get("error")}')

    def test_download_remote_requires_share_and_path(self):
        self.plugin.init_identity({'name': 'needargs'})
        self.plugin.create_group({'group': 'needargs-group'})
        result = self.plugin.download_remote({'device_id': 'ff' * 32})
        self.assertFalse(result['success'])
        self.assertIn('share_id', result['error'])


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
        return self.plugin.get_status()['kernel']['available']

    def test_node_does_not_start_before_identity_and_group(self):
        """没有身份/团体时不启动：那种状态下节点无法认证对端，起来只会报错。"""
        if not self._ready():
            self.skipTest('协议内核不可用')
        status = self.plugin.get_status()
        self.assertFalse(status['node']['running'])
        self.assertIsNone(status['node']['auto_start_error'])

    def test_node_auto_starts_and_publishes_registration(self):
        if not self._ready():
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'auto'})
        self.plugin.create_group({'group': 'auto-group'})

        status = self.plugin.get_status()
        node = status['node']
        self.assertTrue(node['running'], f'有身份与团体后节点应自动启动: {node}')
        self.assertIsNone(node['auto_start_error'], node)
        # 关键：节点起来还不够，必须真的把注册记录发布出去（别人靠它发现本机）
        self.assertTrue(node['published'], f'节点应发布注册记录: {node}')
        self.assertIsInstance(node['published_seq'], int)

        state_file = self.plugin.identity_dir / 'state.json'
        self.assertTrue(state_file.is_file(), '发布记录后应留下 state.json（seq 单调性依据）')
        state = json.loads(state_file.read_text(encoding='utf-8'))
        self.assertEqual(state['registration_seq'], node['published_seq'])
        self.assertTrue(state.get('endpoints'), 'state 里应记住本次发布的端点集合')

        registry = json.loads((self.plugin.identity_dir / 'registry.json')
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
        self.plugin.init_identity({'name': 'once'})
        self.plugin.create_group({'group': 'once-group'})
        first = self.plugin.get_status()['node']
        self.assertTrue(first['running'])
        thread = self.plugin._node_thread
        for _ in range(3):
            again = self.plugin.get_status()['node']
            self.assertTrue(again['running'])
        self.assertIs(self.plugin._node_thread, thread, '不应重建节点线程')

    def test_all_published_endpoints_are_tried(self):
        """设备发布了多个端点时必须逐个试，不能在第一个失败后放弃。

        §4.6.1 把 IPv4 定位为可达性兜底，因此设备常同时发布 IPv6 与 IPv4 端点；
        只试首选那个的话，IPv6 没路由就会把一台其实能连的设备判成离线。
        """
        if not self._ready():
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'multi-ep'})
        self.plugin.create_group({'group': 'multi-ep-group'})
        # 取一次状态让节点自启并载入注册表对象（_registry 在此之前是 None）
        self.assertTrue(self.plugin.get_status()['node']['running'])

        # 造一条"对端设备"的注册记录：两个端点，第一个一定连不上
        cp = sys.modules['shell.groupmesh.crypto_prims']
        registry_mod = sys.modules['shell.groupmesh.registry']
        peer_priv, peer_pub = cp.generate_sign_keypair()
        endpoints = [('2001:db8::bad', 19443), ('192.0.2.7', 19443)]
        self.plugin._registry.add(registry_mod.new_registration(
            device_key=peer_pub, device_private_key=peer_priv,
            seq=1, endpoints=endpoints, shares=['pub']))
        # 让这台设备出现在名单里（list_peers 只列名单内成员）
        me = self.plugin._load_identity()
        self.plugin.add_member({'principal': base64.b64encode(me.principal.public_key).decode(),
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

        self.plugin._fetch_registry_from = fake_fetch
        result = self.plugin.list_peers({'refresh': True})
        self.assertTrue(result['success'])
        self.assertEqual(len(peer_attempts), 2,
                         f'对端的两个端点都该被尝试，实际: {peer_attempts}')
        self.assertEqual(peer_attempts[0][0], '2001:db8::bad',
                         'IPv6 应当优先尝试（主路径）')
        self.assertFalse(result['errors'], f'第二个端点成功后不该留下错误: {result["errors"]}')

    def test_endpoint_change_republishes_registration(self):
        """本机地址集合变化时必须递增 seq 重新发布（设计文档 §7.4）。

        不重发的后果：对端留着旧端点，表现为"两边都开着却连不上"，且没有提示。
        """
        if not self._ready():
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'republish'})
        self.plugin.create_group({'group': 'republish-group'})
        first_seq = self.plugin.get_status()['node']['published_seq']
        self.assertIsNotNone(first_seq)

        # 模拟"地址变了"：把 state 里记的端点改成别的
        state_file = self.plugin.identity_dir / 'state.json'
        state = json.loads(state_file.read_text(encoding='utf-8'))
        state['endpoints'] = [['10.0.0.99', 1]]
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')

        self.plugin.list_peers(refresh=True)
        new_seq = self.plugin.get_status()['node']['published_seq']
        self.assertGreater(new_seq, first_seq, '端点变化后应重新发布并递增 seq')

    def test_unchanged_endpoints_do_not_republish(self):
        """端点没变时不做无谓重发（否则每次刷新都递增 seq，对端反复合并）。"""
        if not self._ready():
            self.skipTest('协议内核不可用')
        self.plugin.init_identity({'name': 'stable'})
        self.plugin.create_group({'group': 'stable-group'})
        first_seq = self.plugin.get_status()['node']['published_seq']
        self.plugin.list_peers(refresh=True)
        self.assertEqual(self.plugin.get_status()['node']['published_seq'], first_seq)


if __name__ == '__main__':
    unittest.main()
