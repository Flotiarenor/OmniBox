"""宿主↔附属插件契约（缩略图四成员）的回归测试。

这四个成员（get_file_roots /
get_thumb_data / thumb_dir / ensure_thumb）此前只由 shell/backend/file_server.py
以 getattr 探针隐式定义：既不在 PluginBase 里，也没有测试固定形状。后果是

  - 宿主把 thumb_dir 改成方法 → getattr 得到 bound method（真值）→ 跳过回退分支
    → Path(bound_method) 抛 TypeError → /thumbs 对所有插件 500；
  - Companion 插件（image-cleaner）代理的正是这些未声明成员，宿主重构无回归保护。

本文件用伪宿主把四种形状钉死：默认形态、只覆写 get_thumb_data（media-player）、
thumb_dir 写成 property / 写成方法、以及 image-cleaner 的代理形态。

运行：
    python -m unittest tests.test_plugin_host_contract -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shell.backend.auth import TOKEN_HEADER, get_or_create_token
from shell.backend.file_server import _resolve_thumb_dir, create_app
from shell.backend.paths import get_config_dir, get_plugins_config_dir
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_manager import collect_protected_paths


class _StubPluginManager:
    """只提供 create_app 在请求期会用到的接口，并按名字返回伪宿主实例。"""

    _instances: ClassVar[dict] = {}

    def __init__(self, instances: dict | None = None):
        self._provided = dict(instances or {})

    def get_api_methods(self):
        return {}

    def get_frontend_manifests(self):
        return []

    def get_plugin_extensions(self):
        return {}

    def get_plugin_status(self):
        return {'loaded': [], 'failures': []}

    def get_plugin_instance(self, name):
        return self._provided.get(name)

    def get_protected_paths(self):
        """与真实 PluginManager 共用同一份聚合 + 边界校验实现（这里都是 PluginBase 子类）。"""
        return collect_protected_paths(self._provided, get_plugins_config_dir())


def _same_path(actual, expected) -> bool:
    """比较两条路径是否指向同一位置。

    不能直接比 Path 字符串：Windows 上同一目录可能以 8.3 短名
    （C:\\Users\\ADMINI~1\\...）或长名出现，而且 Path.resolve() 返回哪一种
    取决于该目录当时是否已存在 —— 逐字比较会随机假失败。优先用文件系统级
    比较（os.path.samefile），路径尚不存在时退回规范化后的字符串比较。
    """
    actual, expected = Path(actual), Path(expected)
    try:
        return os.path.samefile(actual, expected)
    except OSError:
        return os.path.normcase(str(actual.resolve())) == os.path.normcase(str(expected.resolve()))


def _make_proxy(host, keep_alive=None):
    """伪造一个具体插件类，把四个成员按需覆写（函数体内仅 exec、不写入磁盘）。"""

    class _ProxyPlugin(PluginBase):
        def register_api(self):
            return {}

        def get_data_root(self):
            return host.get_data_root()

        def get_file_roots(self):
            return host.get_file_roots()

        @property
        def thumb_dir(self):
            return host.thumb_dir

        def ensure_thumb(self, rel_path):
            return host.ensure_thumb(rel_path)

        def get_thumb_data(self, rel_path):
            return host.get_thumb_data(rel_path)

    return _ProxyPlugin({'name': 'fake-proxy'}, {'directories': {'data_root': str(host.get_data_root())}})


class _DefaultPlugin(PluginBase):
    """什么都不覆写的插件：四个成员必须全部由基类给出可用的默认实现。"""

    def register_api(self):
        return {}


class PluginBaseThumbContractTests(unittest.TestCase):
    """基类默认实现与形状（不经过 HTTP）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_root = Path(self._tmp.name) / 'data'
        self.data_root.mkdir()
        self.config = {'directories': {'data_root': str(self.data_root)}}

    def _plugin(self, cls=_DefaultPlugin, manifest=None):
        return cls(manifest or {'name': 'contract-probe'}, self.config)

    def test_default_thumb_dir_is_data_root_cache_thumbs(self):
        self.assertTrue(
            _same_path(self._plugin().thumb_dir, self.data_root / '.cache' / 'thumbs'),
            f'默认 thumb_dir 应为 数据根/.cache/thumbs，实际 {self._plugin().thumb_dir}',
        )

    def test_default_get_thumb_data_is_none(self):
        self.assertIsNone(self._plugin().get_thumb_data('a/b.png'))

    def test_default_ensure_thumb_is_a_noop(self):
        self.assertIsNone(self._plugin().ensure_thumb('a/b.png'))

    def test_default_get_file_roots_is_data_root(self):
        roots = self._plugin().get_file_roots()
        self.assertEqual(len(roots), 1)
        self.assertTrue(_same_path(roots[0], self.data_root))

    def test_thumb_dir_assignment_does_not_fork_a_second_directory(self):
        """image-viewer 在 __init__ 与换根目录时都会 self.thumb_dir = ... 赋值。

        赋值必须只更新内部派生缓存，读取始终走 property —— 否则"基类默认目录"
        与"实例属性目录"会分叉，宿主与插件看到两个不同的路径。
        """
        plugin = self._plugin()
        other = Path(self._tmp.name) / 'moved'
        plugin.thumb_dir = other / 'thumbs'
        self.assertTrue(_same_path(plugin.thumb_dir, other / 'thumbs'))

    def test_thumb_dir_follows_get_data_root_change(self):
        """插件换数据根目录（image-viewer 的 on_settings_changed）后必须自动跟随。"""
        plugin = self._plugin()
        moved = Path(self._tmp.name) / 'moved-root'
        moved.mkdir()
        plugin.get_data_root = lambda: moved
        self.assertTrue(_same_path(plugin.thumb_dir, moved / '.cache' / 'thumbs'))

    def test_media_player_shape_keeps_thumb_dir_default(self):
        """media-player 只覆写 get_thumb_data、不定义 thumb_dir：默认值必须仍可用。"""

        class _MediaShape(PluginBase):
            def register_api(self):
                return {}

            def get_thumb_data(self, filepath):
                return (b'COVER', 'image/jpeg')

        plugin = _MediaShape({'name': 'media-shape'}, self.config)
        self.assertEqual(plugin.get_thumb_data('track-1'), (b'COVER', 'image/jpeg'))
        self.assertTrue(_same_path(plugin.thumb_dir, self.data_root / '.cache' / 'thumbs'))


class ResolveThumbDirTests(unittest.TestCase):
    """_resolve_thumb_dir 的形状归一化：任何形状都不得让 /thumbs 500。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_root = Path(self._tmp.name) / 'data'
        self.data_root.mkdir()
        self.config = {'directories': {'data_root': str(self.data_root)}}

    def test_property_shape_is_used(self):
        plugin = _DefaultPlugin({'name': 'p'}, self.config)
        self.assertTrue(_same_path(_resolve_thumb_dir(plugin), self.data_root / '.cache' / 'thumbs'))

    def test_method_shape_is_normalized_not_rejected(self):
        """旧插件把 thumb_dir 写成方法：归一化调用，而不是 Path(bound_method) 抛 TypeError。"""

        class _MethodShape(PluginBase):
            def register_api(self):
                return {}

            def thumb_dir(self):  # 故意占用契约成员名做形状回归
                return self.get_data_root() / 'legacy-thumbs'

        resolved = _resolve_thumb_dir(_MethodShape({'name': 'p'}, self.config))
        self.assertTrue(_same_path(resolved, self.data_root / 'legacy-thumbs'))

    def test_broken_shapes_fall_back_to_none(self):
        class _BadShape(PluginBase):
            def register_api(self):
                return {}

            def thumb_dir(self):
                return 42

        for plugin in (_BadShape({'name': 'p'}, self.config), None):
            with self.subTest(plugin=type(plugin).__name__):
                self.assertIsNone(_resolve_thumb_dir(plugin))

    def test_raising_property_falls_back_to_none(self):
        class _Raising(PluginBase):
            def register_api(self):
                return {}

            @property
            def thumb_dir(self):
                raise RuntimeError('boom')

        self.assertIsNone(_resolve_thumb_dir(_Raising({'name': 'p'}, self.config)))


class ThumbRouteContractTests(unittest.TestCase):
    """四种插件形态经过真实 /thumbs 路由的端到端结果（含 image-cleaner 代理形态）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_root = Path(self._tmp.name) / 'data'
        self.data_root.mkdir()
        self.host_thumbs = self.data_root / '.cache' / 'thumbs'
        self.host_thumbs.mkdir(parents=True)
        self.config = {
            'server': {'host': '127.0.0.1', 'port': 18080},
            'directories': {'data_root': str(self.data_root)},
        }
        self.headers = {TOKEN_HEADER: get_or_create_token(get_config_dir())}

    def _client(self, instance, plugin_name='probe'):
        app = create_app(self.config, _StubPluginManager({plugin_name: instance}))
        return app.test_client()

    def _get(self, client, url):
        """统一发请求并登记关闭：文件响应带真实文件句柄，不关会刷 ResourceWarning。"""
        resp = client.get(url, headers=self.headers)
        self.addCleanup(resp.close)
        return resp

    def _host_stub(self):
        """宿主形态：get_thumb_data 返回 None，散文件放在 .cache/thumbs 下。"""
        data_root = self.data_root

        class _Host(PluginBase):
            def register_api(self):
                return {}

            def get_data_root(self):
                return data_root

            def get_thumb_data(self, rel_path):
                return None

        return _Host({'name': 'fake-host'}, self.config)

    def test_default_plugin_serves_thumb_from_default_dir(self):
        """未覆写任何成员：/thumbs 落到 数据根/.cache/thumbs。"""
        (self.host_thumbs / 'ok.jpg').write_bytes(b'DEFAULT-THUMB')
        client = self._client(_DefaultPlugin({'name': 'p'}, self.config))
        resp = self._get(client, '/thumbs/ok.jpg?plugin=probe')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'DEFAULT-THUMB')

    def test_get_thumb_data_takes_priority_over_thumb_dir(self):
        """优先级：get_thumb_data 命中优先，未命中才用 thumb_dir（media-player 形态）。"""
        (self.host_thumbs / 'ok.jpg').write_bytes(b'FROM-THUMB-DIR')

        class _BytesShape(PluginBase):
            def register_api(self):
                return {}

            def get_thumb_data(self, rel_path):
                return (b'FROM-DB', 'image/jpeg')

        client = self._client(_BytesShape({'name': 'p'}, self.config))
        resp = self._get(client, '/thumbs/ok.jpg?plugin=probe')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'FROM-DB')
        self.assertEqual(resp.mimetype, 'image/jpeg')

    def test_method_form_thumb_dir_does_not_500(self):
        """回归：thumb_dir 写成方法时 /thumbs 曾是 500（Path(bound_method)）。

        断言不止"不是 500"：文件只放在方法返回的目录里，必须真的被归一化读到，
        否则"回退到全局目录"也能让状态码不是 500，却把插件缩略图全部弄丢。
        """
        legacy = self.data_root / 'legacy-thumbs'
        legacy.mkdir(parents=True)
        (legacy / 'ok.jpg').write_bytes(b'FROM-LEGACY-THUMB-DIR')

        class _MethodShape(PluginBase):
            def register_api(self):
                return {}

            def get_thumb_data(self, rel_path):
                return None

            def thumb_dir(self):  # 故意占用契约成员名做形状回归
                return legacy

        client = self._client(_MethodShape({'name': 'p'}, self.config))
        resp = self._get(client, '/thumbs/ok.jpg?plugin=probe')
        self.assertNotEqual(resp.status_code, 500, '方法形态的 thumb_dir 不得让整条路由 500')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'FROM-LEGACY-THUMB-DIR')

    def test_unresolvable_thumb_dir_does_not_500(self):
        class _BadShape(PluginBase):
            def register_api(self):
                return {}

            def get_thumb_data(self, rel_path):
                return None

            @property
            def thumb_dir(self):
                return object()

        client = self._client(_BadShape({'name': 'p'}, self.config))
        resp = self._get(client, '/thumbs/ok.jpg?plugin=probe')
        self.assertNotEqual(resp.status_code, 500)
        self.assertEqual(resp.status_code, 404)

    def test_companion_proxy_form_serves_host_bytes(self):
        """image-cleaner 形态：代理宿主三成员，经 /thumbs 端到端返回宿主字节。"""
        (self.host_thumbs / 'photo.jpg').write_bytes(b'PROXIED-THUMB')
        proxy = _make_proxy(self._host_stub())
        resp = self._get(self._client(proxy, plugin_name='image-cleaner'),
                         '/thumbs/photo.jpg?plugin=image-cleaner')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'PROXIED-THUMB')

    def test_unknown_thumb_is_404_not_500(self):
        client = self._client(_DefaultPlugin({'name': 'p'}, self.config))
        resp = self._get(client, '/thumbs/missing.jpg?plugin=probe')
        self.assertEqual(resp.status_code, 404)

    def test_thumb_data_wrong_shape_is_404_not_500(self):
        """插件返回不可解包的值（单值 / 三元组）时必须是 404，不能 500。"""

        class _WrongShape(PluginBase):
            def register_api(self):
                return {}

            def get_thumb_data(self, rel_path):
                return b'ONLY-DATA'

        client = self._client(_WrongShape({'name': 'p'}, self.config))
        resp = self._get(client, '/thumbs/ok.jpg?plugin=probe')
        self.assertEqual(resp.status_code, 404)

    def test_thumb_traversal_is_403_before_plugin_bytes_branch(self):
        """审计项 P3-1：`get_thumb_data` 返回字节的分支原先不做包含判定。

        同一个 `../` 在散文件分支是 403、在字节分支是 200 —— 而"插件自己记得判越界"
        不是 Shell 可以依赖的前提（`get_thumb_data` 是插件实现）。现在两条分支共用
        `_checked_thumb_path`：越界在**调用插件之前**就拒绝，插件根本不该看到它。
        """
        seen = []

        class _BytesShape(PluginBase):
            def register_api(self):
                return {}

            def get_thumb_data(self, rel_path):
                seen.append(rel_path)
                return (b'FROM-DB', 'image/jpeg')

        client = self._client(_BytesShape({'name': 'p'}, self.config))
        payload = '..\\..\\secret.txt' if os.name == 'nt' else '../../secret.txt'
        resp = self._get(client, f'/thumbs/{payload}?plugin=probe')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(seen, [], '越界路径被交给了插件的 get_thumb_data')

        # 根内路径仍然走插件的字节分支（修的是判定位置，不是把功能关掉）
        ok = self._get(client, '/thumbs/ok.jpg?plugin=probe')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_data(), b'FROM-DB')
        self.assertEqual(seen, ['ok.jpg'])

    def test_thumb_traversal_still_403(self):
        client = self._client(_DefaultPlugin({'name': 'p'}, self.config))
        payload = '..\\..\\secret.txt' if os.name == 'nt' else '../../secret.txt'
        resp = self._get(client, f'/thumbs/{payload}?plugin=probe')
        self.assertEqual(resp.status_code, 403)


class FileRouteResolveContractTests(unittest.TestCase):
    """`/file` 的相对路径：插件可以用 `resolve_file_path` 解释**虚拟路径**。

    缺陷背景（image-viewer 多根目录）：`__额外图库/作者B/图.jpg` 这类虚拟路径在
    第一根下并不存在，而 `/file` 原先只按 `get_file_roots()[0]` 拼，于是"网格与
    缩略图都正常，点开任何一张原图都是破图"。`/thumbs` 一直是由插件解释路径的
    （`get_thumb_data` / `ensure_thumb`），本契约把 `/file` 对齐到同一套。

    这一组同时钉住三条安全语义：解析结果必须落在某一根之内、受保护文件仍 403、
    插件实现抛错或返回错形状时**回退默认解析**而不是 500。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.data_root = base / 'data'
        self.data_root.mkdir()
        self.extra_root = base / 'extra'
        self.extra_root.mkdir()
        (self.extra_root / 'pic.jpg').write_bytes(b'EXTRA-ORIGINAL')
        (self.data_root / 'secret.txt').write_bytes(b'SECRET')
        self.outside = base / 'outside'
        self.outside.mkdir()
        (self.outside / 'leak.jpg').write_bytes(b'LEAK')
        self.config = {
            'server': {'host': '127.0.0.1', 'port': 18080},
            'directories': {'data_root': str(self.data_root)},
        }
        self.headers = {TOKEN_HEADER: get_or_create_token(get_config_dir())}

    def _plugin(self, resolver):
        """伪插件：两个根（数据根 + 额外根）、一个受保护文件、可注入的解析器。"""
        data_root, extra_root = self.data_root, self.extra_root

        class _Virtual(PluginBase):
            def register_api(self):
                return {}

            def get_data_root(self):
                return data_root

            def get_file_roots(self):
                return [data_root, extra_root]

            def get_protected_paths(self):
                return [*super().get_protected_paths(), data_root / 'secret.txt']

            def resolve_file_path(self, rel_path):
                return resolver(rel_path)

        return _Virtual({'name': 'virtual'}, self.config)

    def _get(self, instance, url):
        app = create_app(self.config, _StubPluginManager({'probe': instance}))
        resp = app.test_client().get(url, headers=self.headers)
        self.addCleanup(resp.close)
        return resp

    def test_default_resolver_is_none(self):
        """不覆写时保持原语义：Shell 按第一根拼路径。"""
        plugin = _DefaultPlugin({'name': 'p'}, self.config)
        self.assertIsNone(plugin.resolve_file_path('a.jpg'))

    def test_plugin_can_resolve_a_virtual_path_into_an_extra_root(self):
        """核心回归：`__extra/pic.jpg` 必须打到额外根下的真实文件。"""
        def resolver(rel_path):
            if rel_path.startswith('__extra/'):
                return self.extra_root / rel_path[len('__extra/'):]
            return None

        resp = self._get(self._plugin(resolver), '/file?path=__extra/pic.jpg&plugin=probe')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'EXTRA-ORIGINAL')

    def test_resolved_path_outside_every_root_is_403(self):
        """解析结果不在任何根内 → 403（与越界同一语义），插件说了不算。"""
        resp = self._get(self._plugin(lambda rel: self.outside / 'leak.jpg'),
                         '/file?path=whatever.jpg&plugin=probe')
        self.assertEqual(resp.status_code, 403)

    def test_resolved_protected_file_is_403(self):
        """即使插件把路径解析到自己的受保护文件，也照样 403。"""
        resp = self._get(self._plugin(lambda rel: self.data_root / 'secret.txt'),
                         '/file?path=secret.txt&plugin=probe')
        self.assertEqual(resp.status_code, 403)

    def test_missing_resolved_file_is_404_not_500(self):
        resp = self._get(self._plugin(lambda rel: self.extra_root / 'nope.jpg'),
                         '/file?path=nope.jpg&plugin=probe')
        self.assertEqual(resp.status_code, 404)

    def test_raising_file_roots_falls_back_instead_of_500(self):
        """审计项 P3-3：`get_file_roots()` 抛异常时 /file 曾是 500。

        同一路由对 `resolve_file_path` / `is_content_placeholder` / `ensure_file` 都有
        兜底，`/thumbs` 对 `thumb_dir` 也有（`_resolve_thumb_dir`），只有取根这两个调用
        裸露在外 —— 一个插件的实现缺陷会让它全部 /file 请求变成 500（Flask 默认错误页）。
        回退顺序：插件的根 → 插件数据根 → 全局数据根。
        """
        (self.data_root / 'plain.jpg').write_bytes(b'PLAIN')

        class _BrokenRoots(PluginBase):
            def register_api(self):
                return {}

            def get_file_roots(self):
                raise RuntimeError('插件内部错误')

            def get_data_root(self):
                raise RuntimeError('插件内部错误2')

        # 两个插件方法都抛错 → 回退到 config['directories']['data_root']，仍然服务该根内的文件
        resp = self._get(_BrokenRoots({'name': 'broken'}, self.config),
                         '/file?path=plain.jpg&plugin=probe')
        self.assertEqual(resp.status_code, 200, '取根失败被当成了 500')
        self.assertEqual(resp.get_data(), b'PLAIN')
        # 根外路径照旧 403（回退不等于放开边界）
        resp = self._get(_BrokenRoots({'name': 'broken'}, self.config),
                         f'/file?path={self.outside}/leak.jpg&plugin=probe')
        self.assertEqual(resp.status_code, 403)

    def test_broken_data_root_falls_back_when_roots_are_empty(self):
        """只坏一半：`get_file_roots()` 返回空、`get_data_root()` 抛错 → 仍走全局数据根。"""
        (self.data_root / 'plain.jpg').write_bytes(b'PLAIN')

        class _EmptyRoots(PluginBase):
            def register_api(self):
                return {}

            def get_file_roots(self):
                return []

            def get_data_root(self):
                raise RuntimeError('插件内部错误')

        resp = self._get(_EmptyRoots({'name': 'empty'}, self.config),
                         '/file?path=plain.jpg&plugin=probe')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'PLAIN')

    def test_raising_resolver_falls_back_to_the_first_root(self):
        """插件实现抛错：回退默认解析，老行为不受影响，且不得 500。"""
        (self.data_root / 'plain.jpg').write_bytes(b'PLAIN')

        def resolver(rel_path):
            raise RuntimeError('boom')

        resp = self._get(self._plugin(resolver), '/file?path=plain.jpg&plugin=probe')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), b'PLAIN')
        resp = self._get(self._plugin(resolver), '/file?path=nope.jpg&plugin=probe')
        self.assertEqual(resp.status_code, 404)

    def test_non_path_return_is_ignored(self):
        """返回字符串这类错形状一律忽略（插件返回值不可信），不能当成路径用。"""
        resp = self._get(self._plugin(lambda rel: str(self.extra_root / 'pic.jpg')),
                         '/file?path=__extra/pic.jpg&plugin=probe')
        self.assertEqual(resp.status_code, 404)

    def test_relative_traversal_is_still_403(self):
        """解析器答不上来时，越界路径仍按老规矩 403。"""
        payload = '..\\..\\secret.txt' if os.name == 'nt' else '../../secret.txt'
        resp = self._get(self._plugin(lambda rel: None), f'/file?path={payload}&plugin=probe')
        self.assertEqual(resp.status_code, 403)


if __name__ == '__main__':
    unittest.main()
