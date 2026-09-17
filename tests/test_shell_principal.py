"""壳侧主体上下文（设计文档 group-mesh §12 第 1/2 项）的契约用例。

这组用例锁的是**验收标准本身**，不是实现细节：

  * 插件能通过 `PluginBase.current_principal()` 拿到**由壳注入**的主体标识；
  * 该值**无法被请求参数影响**（伪造 `principal` 字段的请求必须与不带的等价）；
  * 未登记的凭据不放行（401），且**不降级成匿名主体**；
  * 后台线程**没有**主体（设计文档 §12：涉及主体的后台任务必须显式携带主体信息）。

为什么必须有"伪造参数被忽略"和"后台线程没有主体"这两条：所有"从请求体读主体"
或"没有主体就当成本机"的实现都能通过"能拿到主体"的用例，而它们在局域网多用户
场景下等于没有鉴权。

运行：
    python -m unittest tests.test_shell_principal -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend import principal as principal_mod
from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import create_app
from shell.backend.principal import (
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
    PrincipalContext,
    PrincipalScope,
    PrincipalStore,
    current_principal,
    hash_secret,
    principals_file,
    require_principal,
    use_principal,
)


class PrincipalStoreTest(unittest.TestCase):
    """凭据表的读写、查表与失效。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_dir = Path(self._tmp.name) / '.config'
        self.store = PrincipalStore(self.config_dir)

    def test_resolve_returns_registered_principal(self):
        self.store.add('alice', 'token-alice', role=ROLE_MEMBER, name='Alice')
        context = self.store.resolve('token-alice')
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual((context.id, context.name, context.role),
                         ('alice', 'Alice', ROLE_MEMBER))
        self.assertFalse(context.is_admin)

    def test_resolve_rejects_unknown_and_empty_credentials(self):
        self.store.add('alice', 'token-alice')
        self.assertIsNone(self.store.resolve('token-bob'))
        self.assertIsNone(self.store.resolve(''))
        self.assertIsNone(self.store.resolve('token-alice '))

    def test_credentials_are_stored_as_digests_only(self):
        """明文令牌不得出现在磁盘上（凭据表会被备份、同步、误读）。"""
        self.store.add('alice', 'super-secret-token')
        raw = principals_file(self.config_dir).read_text(encoding='utf-8')
        self.assertNotIn('super-secret-token', raw)
        self.assertIn(hash_secret('super-secret-token'), raw)

    def test_add_overwrites_existing_principal(self):
        self.store.add('alice', 'token-1', role=ROLE_MEMBER)
        self.store.add('alice', 'token-2', role=ROLE_ADMIN)
        self.assertIsNone(self.store.resolve('token-1'), '旧凭据必须立即失效')
        context = self.store.resolve('token-2')
        assert context is not None
        self.assertEqual(context.role, ROLE_ADMIN)

    def test_remove_revokes_credential(self):
        self.store.add('alice', 'token-alice')
        self.assertTrue(self.store.remove('alice'))
        self.assertIsNone(self.store.resolve('token-alice'))
        self.assertFalse(self.store.remove('alice'), '重复移除应返回 False')

    def test_bootstrap_registers_legacy_token_as_owner(self):
        """老部署只有全局令牌：升级后它必须继续可用，且**只登记一次**。"""
        store = PrincipalStore(self.config_dir, bootstrap_token='legacy-token')
        context = store.ensure_bootstrap()
        assert context is not None
        self.assertEqual(context.role, ROLE_OWNER)
        self.assertEqual(context.source, principal_mod.SOURCE_AUTH_TOKEN)
        self.assertIsNotNone(store.resolve('legacy-token'))

        # 二次自举不得覆盖用户改过的角色与凭据
        store.add('owner', 'new-token', role=ROLE_ADMIN)
        self.assertIsNone(store.ensure_bootstrap())
        self.assertIsNone(store.resolve('legacy-token'))
        context = store.resolve('new-token')
        assert context is not None
        self.assertEqual(context.role, ROLE_ADMIN)

    def test_broken_record_does_not_invalidate_the_whole_table(self):
        """单条脏记录不能让整份凭据表作废（否则用户只能手工改文件才进得来）。"""
        self.store.add('alice', 'token-alice', role=ROLE_ADMIN)
        path = principals_file(self.config_dir)
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['principals'].append({'id': 'broken', 'secret_sha256': 'too-short'})
        payload['principals'].append('not-a-dict')
        path.write_text(json.dumps(payload), encoding='utf-8')

        store = PrincipalStore(self.config_dir)
        self.assertIsNotNone(store.resolve('token-alice'), '好记录必须仍然可用')
        self.assertIsNone(store.get('broken'))

    def test_unreadable_table_keeps_last_known_good(self):
        self.store.add('alice', 'token-alice')
        principals_file(self.config_dir).write_text('{ not json', encoding='utf-8')
        self.store.reload()
        self.assertIsNotNone(self.store.resolve('token-alice'),
                             '读盘失败必须沿用上一次成功读取的结果')

    def test_external_edit_takes_effect_without_restart(self):
        """撤销某个主体的凭据必须立即生效，不能等重启。"""
        self.store.add('alice', 'token-alice')
        self.assertIsNotNone(self.store.resolve('token-alice'))
        PrincipalStore(self.config_dir).remove('alice')
        self.assertIsNone(self.store.resolve('token-alice'))

    def test_rejects_invalid_arguments(self):
        with self.assertRaises(ValueError):
            self.store.add('', 'token')
        with self.assertRaises(ValueError):
            self.store.add('alice', '')
        with self.assertRaises(ValueError):
            self.store.add('alice', 'token', role='superuser')


class PrincipalContextTest(unittest.TestCase):
    """ContextVar 语义：注入、不跨线程继承、可显式携带。"""

    def test_default_is_none(self):
        self.assertIsNone(current_principal())

    def test_use_principal_restores_previous_value(self):
        alice = PrincipalContext(id='alice', name='Alice', role=ROLE_MEMBER,
                                 source=principal_mod.SOURCE_ENROLLED)
        with use_principal(alice):
            self.assertEqual(current_principal(), alice)
        self.assertIsNone(current_principal())

    def test_scope_restores_even_on_exception(self):
        alice = PrincipalContext(id='alice', name='Alice', role=ROLE_OWNER,
                                 source=principal_mod.SOURCE_ENROLLED)
        with self.assertRaises(RuntimeError), PrincipalScope(alice):
            raise RuntimeError('boom')
        self.assertIsNone(current_principal())

    def test_require_principal_raises_without_context(self):
        with self.assertRaises(PermissionError):
            require_principal()
        alice = PrincipalContext(id='alice', name='Alice', role=ROLE_OWNER,
                                 source=principal_mod.SOURCE_ENROLLED)
        with use_principal(alice):
            self.assertEqual(require_principal().id, 'alice')

    def test_background_thread_does_not_inherit_principal(self):
        """设计文档 §12：插件自起的后台线程不继承请求上下文。

        这不是缺陷而是约束：后台任务若要用主体，必须**显式**携带
        （`with use_principal(p):`），否则读到 None —— 而 None 会被
        `require_principal()` 拒绝，不会静默当成"本机主体"。
        """
        alice = PrincipalContext(id='alice', name='Alice', role=ROLE_OWNER,
                                 source=principal_mod.SOURCE_ENROLLED)
        seen = []
        with use_principal(alice):
            thread = threading.Thread(target=lambda: seen.append(current_principal()))
            thread.start()
            thread.join()
        self.assertEqual(seen, [None])

    def test_background_thread_can_carry_principal_explicitly(self):
        alice = PrincipalContext(id='alice', name='Alice', role=ROLE_OWNER,
                                 source=principal_mod.SOURCE_ENROLLED)
        seen = []

        def worker():
            with use_principal(alice):
                seen.append(current_principal())

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(seen, [alice])

    def test_principal_context_has_no_credential_field(self):
        """主体对象不得携带凭据：它会被日志、界面、插件到处传。"""
        self.assertNotIn('secret', PrincipalContext.__dataclass_fields__)
        self.assertNotIn('token', PrincipalContext.__dataclass_fields__)
        alice = PrincipalContext(id='alice', name='Alice', role=ROLE_MEMBER,
                                 source=principal_mod.SOURCE_ENROLLED)
        self.assertNotIn('secret', alice.to_dict())


class _StubPluginManager:
    """`create_app` 需要的最小形状：本组用例只走壳自身的端点，因此全部返回空。"""

    def get_api_methods(self):
        return []

    def get_frontend_manifests(self):
        return []

    def get_plugin_extensions(self, *_args, **_kwargs):
        return []

    def get_plugin_status(self):
        return []

    def get_settings_panels(self):
        return []

    def save_settings_panel(self, *_args, **_kwargs):
        return {'success': True}

    def get_plugin_instance(self, _name):
        return None

    def get_protected_paths(self):
        return []


class ShellInjectionTest(unittest.TestCase):
    """真实 Flask 壳：主体由壳在鉴权通过后注入，伪造的请求参数无效。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / '.config'
        self.data_root = self.home / 'data'
        self.data_root.mkdir()

        config = {'server': {'host': '127.0.0.1', 'port': 18080},
                  'directories': {'data_root': str(self.data_root)}}
        # 把壳的凭据目录指到临时目录：`create_app` 里的 `get_config_dir()` 用
        # patch 覆盖，这样不会往工作区的真实 `.config/` 里写令牌与凭据表
        # （与 tests/test_file_server_paths.py 走真实目录的做法不同，这里刻意隔离）。
        with mock.patch('shell.backend.file_server.get_config_dir',
                        return_value=self.config_dir):
            self.app = create_app(config, _StubPluginManager())
        self.client = self.app.test_client()
        self.token = get_or_create_token(self.config_dir)
        self._observed = None
        self._install_observer()
        # 一条只回"当前主体"的探针端点。为什么要它：验收标准关心的是"壳给插件/端点
        # 注入了哪个主体"，而不关心某个具体端点的业务语义 —— 借用
        # `system_get_config` 会因为该端点后来被加进管理员专属清单而误红。
        @self.app.route('/api/_probe_principal', methods=['POST'])
        def _probe_principal():
            principal = current_principal()
            return {'result': {'id': principal.id if principal else None,
                               'role': principal.role if principal else None}}

    def _post(self, method, payload, headers=None):
        return self.client.post(f'/api/{method}', json=payload,
                                headers={TOKEN_HEADER: self.token} if headers is None else headers)

    def test_unauthenticated_request_is_rejected(self):
        response = self.client.post('/api/system_get_config', json={'args': [], 'kwargs': {}})
        self.assertEqual(response.status_code, 401)

    def test_legacy_token_still_works_and_yields_a_principal(self):
        """向后兼容：升级后既有令牌继续可用，而且"这次调用是谁"从此有答案。"""
        self.assertIsNotNone(PrincipalStore(self.config_dir).resolve(self.token))
        seen = []
        with self._observe_principal(seen):
            response = self._post('system_get_config', {'args': [], 'kwargs': {}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0], '鉴权通过的请求必须有主体')
        assert seen[0] is not None
        self.assertEqual(seen[0].role, ROLE_OWNER)

    def test_forged_principal_argument_is_ignored(self):
        """验收标准：伪造 `principal` 字段的请求与不带的请求得到同一个主体。

        这条对"从请求体读主体"的实现会红 —— 而那种实现能通过所有"能拿到主体"
        的用例，因此它才是真正的判据。
        """
        forged = {'args': [], 'kwargs': {},
                  'principal': 'bob', 'principal_id': 'bob', 'role': ROLE_OWNER,
                  'name': 'Bob'}
        plain_view, spoofed_view = [], []
        with self._observe_principal(plain_view):
            plain = self._post('system_get_config', {'args': [], 'kwargs': {}})
        with self._observe_principal(spoofed_view):
            spoofed = self._post('system_get_config', forged)

        self.assertEqual(plain.status_code, 200)
        self.assertEqual(plain.get_json(), spoofed.get_json())
        self.assertEqual(plain_view, spoofed_view)
        assert plain_view[0] is not None
        self.assertNotEqual(plain_view[0].id, 'bob')
        # 请求之间不得残留主体
        self.assertIsNone(current_principal())

    def test_unknown_well_formed_token_is_rejected(self):
        """未登记凭据必须 401，而不是降级成匿名/本机主体。"""
        response = self._post('system_get_config', {'args': [], 'kwargs': {}},
                              headers={TOKEN_HEADER: 'x' * 43})
        self.assertEqual(response.status_code, 401)

    def test_removed_principal_loses_access_immediately(self):
        """撤销凭据必须立即生效：撤了还要等重启就等于没撤。"""
        store = PrincipalStore(self.config_dir)
        store.remove('owner')
        response = self._post('system_get_config', {'args': [], 'kwargs': {}})
        self.assertEqual(response.status_code, 401)

    def test_second_principal_gets_its_own_identity(self):
        """按主体颁发令牌的意义所在：两个使用者拿到两个不同的主体。"""
        store = PrincipalStore(self.config_dir)
        store.add('alice', 'alice-token', role=ROLE_MEMBER, name='Alice')
        response = self.client.post('/api/_probe_principal', json={},
                                    headers={TOKEN_HEADER: 'alice-token'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['result'], {'id': 'alice', 'role': ROLE_MEMBER})

    # ── 观测工具 ──────────────────────────────────────────────────────────

    def _observe_principal(self, sink):
        """在**主体仍然生效时**记录 `current_principal()`。

        观测钩子必须在 `create_app` 之后、**第一个请求之前**注册（Flask 3 的
        `after_request` 过了首个请求就拒绝再注册），因此这里注册一次固定钩子，
        由 `self._observed` 决定这次要不要记录。

        为什么用 `after_request` 而不是加一条测试路由：它在视图执行**之后**、
        `teardown_request` 之前触发，读到的是壳注入并正在生效的那个主体；
        另加一条测试路由测的是"另一条路由也能注入"，与验收标准不是同一件事。
        """
        self._observed = sink
        return _Clear(self)

    def _install_observer(self):
        def observer(response):
            if self._observed is not None:
                self._observed.append(current_principal())
            return response

        # 直接登记到 Flask 的内部表：需要在首个请求之前完成，而 setUp 里
        # create_app 之后就是那个时机（此处不用 app.after_request 只是为了避免
        # "同一次 setUp 里连续注册两个钩子"的重复，行为与它完全一致）。
        self.app.after_request_funcs.setdefault(None, []).append(observer)


class _Clear:
    """`with` 结束后停止记录（钩子本身常驻，避免重复注册）。"""

    def __init__(self, test_case):
        self._test_case = test_case

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._test_case._observed = None


def _write_table(config_dir: Path, records) -> None:
    """用例辅助：直接写一份凭据表（形状与 `PrincipalStore` 一致）。

    手写而非走 `PrincipalStore.add()`：用来覆盖"文件是外部改的"这类情形
    （工作区里的凭据表本来就是人手能改的）。
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    principals_file(config_dir).write_text(
        json.dumps({'principals': records}, ensure_ascii=False), encoding='utf-8')


class PluginPrincipalInjectionTest(unittest.TestCase):
    """验收标准落在**插件**这一层：`PluginBase.current_principal()` 拿到的是壳注入的
    主体，且请求体里的 `principal` 字段**影响不了它**。

    为什么必须用真实的 `PluginManager` + 真实的 `/api/<插件>__<方法>` 通路，而不是
    直接调 `current_principal()`：被验收的承诺是"插件读到的主体由壳注入"，
    而"壳到插件"这一段（`before_request` → `ContextVar` → 插件方法）才是它要保证的。
    壳自身端点上的同类断言见 `ShellInjectionTest`，两者不能互相替代。
    """

    PLUGIN_SOURCE = '''\
from shell.backend.plugin_base import PluginBase


class WhoAmIPlugin(PluginBase):
    """最小插件：把本次调用的主体回给调用方。"""

    def whoami(self):
        principal = self.current_principal()
        if principal is None:
            return {'id': None, 'role': None, 'is_admin': False}
        return {'id': principal.id, 'role': principal.role,
                'is_admin': principal.is_admin, 'source': principal.source}

    def require(self):
        """没有主体时抛异常（走壳的 500 分支），有主体则回它的 id。"""
        return {'id': self.require_principal().id}

    def in_thread(self):
        """在插件自起的线程里读主体：必须是 None（不跨线程继承）。"""
        import threading
        seen = []
        thread = threading.Thread(target=lambda: seen.append(self.current_principal()))
        thread.start()
        thread.join()
        return {'id': seen[0].id if seen[0] is not None else None}

    def register_api(self):
        return {'whoami': self.whoami, 'require': self.require, 'in_thread': self.in_thread}
'''

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / '.config'
        self.data_root = self.home / 'data'
        self.data_root.mkdir()

        plugin_dir = self.home / 'plugins' / 'whoami'
        (plugin_dir / 'backend').mkdir(parents=True)
        (plugin_dir / 'backend' / 'main.py').write_text(self.PLUGIN_SOURCE, encoding='utf-8')
        (plugin_dir / 'manifest.json').write_text(json.dumps({
            'name': 'whoami', 'version': '0.1.0', 'displayName': 'WhoAmI',
            'backend': {'entry': 'backend/main.py', 'class': 'WhoAmIPlugin'},
        }), encoding='utf-8')

        from shell.backend.plugin_manager import PluginManager
        self.manager = PluginManager(self.home / 'plugins',
                                     {'directories': {'data_root': str(self.data_root)}})
        self.manager.load_all()
        self.assertIn('whoami', self.manager._instances, '测试插件必须被真实加载')

        config = {'server': {'host': '127.0.0.1', 'port': 18081},
                  'directories': {'data_root': str(self.data_root)}}
        with mock.patch('shell.backend.file_server.get_config_dir',
                        return_value=self.config_dir):
            self.app = create_app(config, self.manager)
        self.client = self.app.test_client()
        self.token = get_or_create_token(self.config_dir)

    def _call(self, method, payload=None):
        return self.client.post(f'/api/whoami__{method}', json=payload or {'args': [], 'kwargs': {}},
                                headers={TOKEN_HEADER: self.token})

    def test_plugin_reads_the_shell_injected_principal(self):
        response = self._call('whoami')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()['result']
        self.assertEqual(result['id'], 'owner')
        self.assertEqual(result['role'], ROLE_OWNER)
        self.assertTrue(result['is_admin'])

    def test_forged_principal_field_does_not_reach_the_plugin(self):
        """验收标准：伪造 `principal` 的请求与不带的请求，插件读到**同一个**主体。"""
        plain = self._call('whoami').get_json()['result']
        forged = self._call('whoami', {
            'args': [], 'kwargs': {},
            'principal': 'bob', 'principal_id': 'bob', 'role': 'member',
        }).get_json()['result']
        self.assertEqual(plain, forged)
        self.assertEqual(forged['id'], 'owner')

    def test_forged_principal_in_kwargs_is_also_ignored(self):
        """更坏的一种伪造：把 `principal` 塞进 kwargs，希望它成为方法参数。

        壳只把 `args` / `kwargs` 展开成调用参数，而插件方法没有 `principal` 形参，
        因此壳会回一个错误 —— 关键是它**绝不能**因此让插件拿到 'bob'。
        """
        response = self._call('whoami', {'args': [], 'kwargs': {'principal': 'bob'}})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('bob', json.dumps(response.get_json(), ensure_ascii=False))

    def test_require_principal_succeeds_for_a_known_caller(self):
        response = self._call('require')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['result']['id'], 'owner')

    def test_plugin_thread_has_no_principal(self):
        """插件自起的线程读不到主体：设计文档 §12 要求的"后台任务必须显式携带"。"""
        response = self._call('in_thread')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()['result']['id'])

    def test_second_principal_is_visible_to_the_plugin(self):
        """两个使用者 → 插件看到两个不同主体（这才是"按主体颁发令牌"的意义）。"""
        PrincipalStore(self.config_dir).add('alice', 'alice-token',
                                            role=ROLE_MEMBER, name='Alice')
        response = self.client.post('/api/whoami__whoami',
                                    json={'args': [], 'kwargs': {}},
                                    headers={TOKEN_HEADER: 'alice-token'})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()['result']
        self.assertEqual((result['id'], result['role']), ('alice', ROLE_MEMBER))
        self.assertFalse(result['is_admin'])


class AdminOnlyEndpointsTest(unittest.TestCase):
    """管理员专属端点（设计文档 group-mesh §12 第 5 项）。

    原状：`system_settings_save` / `system_get_config` 只校验令牌，任何持令牌者都能
    改任意插件的设置、读走完整配置 —— 而设置里有绑定地址、下载目录这类"改了就等于
    改了别人机器行为"的项。现在按角色判定：管理员通过，普通成员 403。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.config_dir = self.home / '.config'
        self.data_root = self.home / 'data'
        self.data_root.mkdir()
        self.config = {'server': {'host': '127.0.0.1', 'port': 18082},
                       'directories': {'data_root': str(self.data_root)}}
        with mock.patch('shell.backend.file_server.get_config_dir',
                        return_value=self.config_dir):
            self.app = create_app(self.config, _StubPluginManager())
        self.client = self.app.test_client()
        self.owner_token = get_or_create_token(self.config_dir)
        self.store = PrincipalStore(self.config_dir)

    def _post(self, method, token, payload=None):
        return self.client.post(f'/api/{method}',
                                json=payload or {'args': [], 'kwargs': {}},
                                headers={TOKEN_HEADER: token})

    def test_owner_can_read_config_and_save_settings(self):
        """自举成 owner 的老令牌不受影响：本机使用者无需任何操作。"""
        self.assertEqual(self._post('system_get_config', self.owner_token).status_code, 200)
        self.assertEqual(self._post('system_settings_save', self.owner_token).status_code, 200)

    def test_member_is_forbidden_on_admin_endpoints(self):
        self.store.add('alice', 'alice-token', role=ROLE_MEMBER, name='Alice')
        for method in ('system_get_config', 'system_settings_save',
                       'system_get_plugin_status'):
            with self.subTest(method=method):
                self.assertEqual(self._post(method, 'alice-token').status_code, 403)

    def test_admin_role_passes(self):
        self.store.add('carol', 'carol-token', role=ROLE_ADMIN, name='Carol')
        self.assertEqual(self._post('system_get_config', 'carol-token').status_code, 200)

    def test_member_can_still_call_plugin_apis(self):
        """限权只覆盖壳自己的三个端点：插件 API 不受影响（由插件自己决定要不要限）。"""
        self.store.add('alice', 'alice-token', role=ROLE_MEMBER)
        self.assertEqual(self._post('system_get_plugins', 'alice-token').status_code, 200)

    def test_unknown_token_is_still_401_not_403(self):
        """未认证是 401（前端会引导重新取令牌），已认证但权限不够才是 403。"""
        self.assertEqual(self._post('system_get_config', 'x' * 43).status_code, 401)


if __name__ == '__main__':
    unittest.main()
