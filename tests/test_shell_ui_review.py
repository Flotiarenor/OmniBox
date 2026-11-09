"""壳界面（``shell/frontend/src``）的静态契约与纯逻辑用例。

为什么需要：这轮迁移新增的四个壳组件（PluginChrome / PluginSettingsModal /
SchemaRenderer / NativePanel）与 App.vue 的改动没有 Vue 单测环境，只有一次性
Selenium 验收；组件里的生命周期、焦点作用域与消息边界一旦退回去，页面"看起来"
仍然正常。这里用两类不启动浏览器的用例把关键不变量锁住：

1. 可执行的判定逻辑抽到 ``src/core/*`` 的纯模块，由 ``tests/js/*.mjs`` 用 node 直接
   import 真实实现做行为断言（``plugin-url.js`` 的用例在本文件里，其余由各自的
   ``test_*_js.py`` 驱动）；
2. 剩下的结构性契约用源码断言 —— 只保留"Node 侧无法驱动"的那部分：它们要表达的是
   **接线事实**（某个 SFC 用了哪份共享实现、某个监听器注册在哪个生命周期里），
   而不是可以抽成函数的判定规则。凡是能抽成函数的判定都已经搬进 ``src/core``。

两条都必须能真的失败。先红后绿的具体输出与 Selenium 取证见 ``.review/shell-ui.md``。
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / 'shell' / 'frontend' / 'src'
NODE = shutil.which('node')
PLUGINS = PROJECT_ROOT / 'plugins'
SCHEMA_VAR = 'SETTINGS_SCHEMA'


def read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def _schema_type_literals(path: Path) -> set[str]:
    """读出某个后端模块里 ``SETTINGS_SCHEMA = [...]`` 声明的字段 ``type`` 字面量。

    为什么按 AST 而不是全文正则：组件化之后 schema 定义在各插件的 ``const.py``，
    入口只剩 ``settings_schema = const.SETTINGS_SCHEMA`` 一行；而同一个后端目录里还有
    别的 ``"type"`` 字面量（API 返回里的 ``"type": "image"`` / ``"type": "album"`` 等）。
    全文扫会把它们当成设置字段类型，误报"契约外的字段类型"。按"模块级
    ``SETTINGS_SCHEMA`` 赋值语句"取常量，既不依赖文件名，也不受同文件其它字面量影响。
    """
    try:
        tree = ast.parse(read(path), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):   # pragma: no cover - 后端语法错误由别的门禁拦
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(target, ast.Name) and target.id == SCHEMA_VAR for target in targets):
            continue
        if node.value is None:   # pragma: no cover - 只有 `SETTINGS_SCHEMA: list` 的空注解
            continue
        for sub in ast.walk(node.value):
            if not isinstance(sub, ast.Dict):
                continue
            # dict 字面量的 keys / values 由 AST 保证一一对应，严格 zip 不会误伤
            for key, value in zip(sub.keys, sub.values, strict=True):
                if (isinstance(key, ast.Constant) and key.value == 'type'
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    found.add(value.value)
    return found


def schema_types_by_plugin() -> dict[str, set[str]]:
    """插件名 → 它 ``settings_schema`` 里声明过的字段类型。"""
    by_plugin: dict[str, set[str]] = {}
    for path in sorted(PLUGINS.glob('*/backend/**/*.py')):
        types = _schema_type_literals(path)
        if not types:
            continue
        plugin = path.relative_to(PLUGINS).parts[0]
        by_plugin.setdefault(plugin, set()).update(types)
    return by_plugin


def plugins_declaring_settings() -> set[str]:
    """入口里绑定了 ``settings_schema`` 的插件，用来防"扫不到类型却静默通过"。"""
    declared: set[str] = set()
    for entry in sorted(PLUGINS.glob('*/backend/main.py')):
        if re.search(r'(?m)^\s*settings_schema\b', read(entry)):
            declared.add(entry.parent.parent.name)
    return declared


@unittest.skipUnless(NODE, '未检测到 node，跳过 plugin-url 前端用例')
class PluginUrlGuardTests(unittest.TestCase):
    """native 面板 URL 的信任边界：只能指向 `/plugins/<plugin>/` 目录内。

    旧实现是 ``url.startsWith('/plugins/' + plugin + '/')``：``/plugins/x/../settings``
    与 ``%2e%2e`` 变体都能通过 while 浏览器会把路径归一化到插件目录之外。
    """

    CASES = (
        ('group-mesh', '/plugins/group-mesh/native.html', True),
        ('group-mesh', '/plugins/group-mesh/content.html?content=1', True),
        ('image-cleaner', '/plugins/image-cleaner/frontend/native.html?native=1', True),
        ('group-mesh', '/plugins/group-mesh/a/b/c.html#x', True),
        ('group-mesh', '/plugins/group-mesh/', True),
        ('group-mesh', '/plugins/group-mesh/../settings', False),
        ('group-mesh', '/plugins/group-mesh/%2e%2e/settings', False),
        ('group-mesh', '/plugins/group-mesh/..%2fsettings', False),
        ('group-mesh', '/plugins/group-mesh/%2e%2e%2fsettings', False),
        ('x', '/plugins/x/..%5c..%5csettings', False),
        ('x', '/plugins/x/..%5csettings', False),
        ('group-mesh', '/plugins/other/x', False),
        ('group-mesh', '//evil.example/x', False),
        ('group-mesh', 'https://evil.example/plugins/group-mesh/x', False),
        ('group-mesh', 'javascript:alert(1)', False),
        ('Group-Mesh', '/plugins/Group-Mesh/x', False),
        ('..', '/plugins/../x', False),
        ('group-mesh', '', False),
        ('group-mesh', '/plugins/group-mesh', False),
    )
    NAMES = (
        ('group-mesh', True),
        ('image-cleaner', True),
        ('a1-b2', True),
        ('group_mesh', False),
        ('-x', False),
        ('..', False),
        ('Group-Mesh', False),
        ('', False),
    )

    def test_guard_rejects_urls_that_escape_the_plugin_directory(self) -> None:
        module = (SRC / 'core' / 'plugin-url.js').as_uri()
        script = '\n'.join([
            f'const m = await import({json.dumps(module)});',
            f'const names = {json.dumps(self.NAMES)};',
            f'const cases = {json.dumps(self.CASES)};',
            'const out = {',
            '  names: names.map(([value]) => [value, m.isPluginName(value)]),',
            '  urls: cases.map(([plugin, url, want]) => [plugin, url, want, m.isPluginPageUrl(plugin, url)]),',
            '};',
            'console.log(JSON.stringify(out));',
        ])
        proc = subprocess.run(
            [NODE, '--input-type=module', '-e', script],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120, check=False,
        )
        if proc.returncode != 0:
            self.fail(f'plugin-url.js 用例执行失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')
        payload = json.loads(proc.stdout.strip().splitlines()[-1])

        wrong_names = {value: got for value, got in payload['names'] if dict(self.NAMES)[value] is not got}
        self.assertEqual(wrong_names, {}, f'插件名判定与预期不一致: {wrong_names}')

        mismatches = [case for case in payload['urls'] if case[2] != case[3]]
        detail = [f'{plugin} {url}: want {want}, got {got}'
                  for plugin, url, want, got in mismatches]
        self.assertEqual(mismatches, [], 'URL 判定与预期不一致: ' + json.dumps(detail, ensure_ascii=False))


class ShellSourceContractTests(unittest.TestCase):
    """剩下的结构性契约：只保留"Node 侧无法驱动"的接线事实。

    消息白名单与 chrome 状态合并已抽成 ``core/plugin-chrome-protocol.js``，其行为由
    ``tests/js/plugin_chrome_protocol.mjs``（驱动见 ``tests/test_plugin_chrome_protocol_js.py``）
    断言：白名单内容、文档出处、未登记类型被拒、合并语义。这里因此不再反推源码里的
    类型字面量 —— 那种正则断言会随写法漂移，改成"行为回归"才拦得住。
    """

    def test_native_panel_url_guard_is_shared(self) -> None:
        """两处 native 面板入口（壳扩展 / 宿主消息）必须共用同一份归一化校验。

        URL 判定本身由 ``PluginUrlGuardTests`` 跑真实模块断言；这里守的是**接线**：
        两个入口都调到同一份实现，没有哪一处退回前缀比较。调用的"份数"只能在源码里数，
        因此保留为静态断言。
        """
        source = read(SRC / 'App.vue')
        self.assertIn("from './core/plugin-url'", source)
        self.assertEqual(source.count('isPluginPageUrl('), 2,
                         'onChromeNative 与 onNativeMessage 都应调用 isPluginPageUrl')
        self.assertNotIn("url.startsWith('/plugins/' + plugin + '/')", source,
                         '前缀比较会被点段绕过，不能退回原实现')

    def test_plugin_chrome_exposes_shell_active_view_for_audit(self) -> None:
        """壳侧当前视图要有可查询标记，审计才能不依赖插件内容页的 [data-panel]。"""
        source = read(SRC / 'components' / 'PluginChrome.vue')
        self.assertIn('data-active-view="activeView"', source)
        self.assertIn('data-view-id="item.id"', source)

    def test_freshness_remounts_when_native_panel_recreates_mount_point(self) -> None:
        """native 面板开关会销毁/重建顶栏，``Freshness.mount`` 不能只做一次。

        复现（Selenium，见 .review/shell-ui.md）：图片相册打开「相册清理」面板再关掉，
        ``.plugin-freshness`` 容器还在但里面是空的 —— 同步/校验控件永久消失。
        """
        source = read(SRC / 'components' / 'PluginChrome.vue')
        self.assertIn('ref="freshnessRef"', source)
        self.assertRegex(source, r'watch\(\s*freshnessRef\s*,')
        self.assertRegex(source, r'watch\(\s*freshnessRef[\s\S]{0,200}mountFreshness\(')

    def test_focus_trap_scopes_to_topmost_nested_dialog(self) -> None:
        """设置弹窗里再开目录选择器时，Tab 循环必须以最上层那层为界。

        复现（Selenium，见 .review/shell-ui.md）：FolderPicker 打开后第一次 Tab
        就落到背景的关闭按钮，Enter 会把整个设置弹窗关掉、未保存的改动丢失。
        """
        source = read(SRC / 'core' / 'use-dialog-focus.ts')
        self.assertIn("'.iv-picker-modal'", source)
        self.assertIn('focusScope()', source)
        self.assertIn('scope.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)', source,
                      '可聚焦元素必须从 focusScope() 的根查，而不是整个设置弹窗')

    def test_chrome_protocol_is_used_from_the_shared_module(self) -> None:
        """chrome 消息的受理与合并必须走共享模块，不能在 SFC 里再写一份白名单。

        白名单内容与合并语义由 ``tests/js/plugin_chrome_protocol.mjs`` 断言（真实 import）；
        这里守接线：App.vue 导入并调用了这两个入口。
        """
        source = read(SRC / 'App.vue')
        self.assertIn("from './core/plugin-chrome-protocol'", source)
        self.assertIn('isPluginChromeMessage(data)', source)
        self.assertIn('mergeChromeState(', source)

    def test_unsent_close_native_message_is_not_advertised(self) -> None:
        """``cubby:close-native`` 全仓库没有发送方，处理分支不能留成"已支持"。

        这条守的是 native 面板通道（``onNativeMessage`` 只认 ``cubby:open-native``）。
        chrome 白名单那一半已经变成行为断言：``isPluginChromeMessage`` 拒绝未登记类型，
        由 ``tests/js/plugin_chrome_protocol.mjs`` 覆盖；但"App.vue 里不得出现这个字面量"
        只能看源码 —— 它描述的是源码里没有这条分支，不是一个可执行的判定。
        """
        source = read(SRC / 'App.vue')
        self.assertIn("'cubby:open-native'", source)
        self.assertNotIn('cubby:close-native', source)

    def test_settings_field_types_are_in_the_shared_contract(self) -> None:
        """插件 ``settings_schema`` 用到的 type 必须都在 SchemaField 的联合里。

        类型取自每个插件**真正声明 schema 的模块**（按 ``SETTINGS_SCHEMA`` 赋值语句用
        AST 取），不是入口 ``main.py``：组件化之后入口只剩 ``settings_schema = const.
        SETTINGS_SCHEMA`` 一行引用。两个方向都锁住 —— 声明了 schema 的插件必须真被
        读到（否则用例空转），读到的类型不得越出契约。
        """
        union_source = read(SRC / 'core' / 'plugin-content.ts')
        match = re.search(r'type\?:\s*([^\n]+)', union_source)
        self.assertIsNotNone(match, 'plugin-content.ts 里找不到 SchemaField.type 联合')
        assert match is not None
        union = set(re.findall(r"'([a-z]+)'", match.group(1)))

        used = schema_types_by_plugin()
        declared = plugins_declaring_settings()
        self.assertTrue(declared, '没有找到任何声明 settings_schema 的插件，用例会变成空转')
        unscanned = declared - set(used)
        self.assertEqual(unscanned, set(),
                         '这些插件声明了 settings_schema，但没从它的定义里读到字段类型：'
                         f'{sorted(unscanned)}（schema 换了定义方式？）')
        unknown = {plugin: sorted(types - union)
                   for plugin, types in sorted(used.items()) if types - union}
        self.assertEqual(unknown, {}, f'插件用了契约外的字段类型: {unknown}')

    def test_secret_mask_matches_the_backend_contract(self) -> None:
        """掩码回传 = 不改动；前后端必须用同一个字面量，否则会被当成新值写入。"""
        backend = read(PROJECT_ROOT / 'shell' / 'backend' / 'plugin_base.py')
        modal = read(SRC / 'components' / 'PluginSettingsModal.vue')
        self.assertIn("SECRET_MASK = '********'", backend)
        self.assertIn("'********'", modal)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
