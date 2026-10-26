"""壳界面（``shell/frontend/src``）的静态契约与纯逻辑用例。

为什么需要：这轮迁移新增的四个壳组件（PluginChrome / PluginSettingsModal /
SchemaRenderer / NativePanel）与 App.vue 的改动没有 Vue 单测环境，只有一次性
Selenium 验收；组件里的生命周期、焦点作用域与消息边界一旦退回去，页面"看起来"
仍然正常。这里用两类不启动浏览器的用例把关键不变量锁住：

1. 纯 JS 模块（``core/plugin-url.js``）用 node 直接 import 做行为断言；
2. 组件/SFC 的结构性契约用源码断言：破坏时失败信息会指向"哪条不变量丢了"。

两条都必须能真的失败。先红后绿的具体输出与 Selenium 取证见 ``.review/shell-ui.md``。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / 'shell' / 'frontend' / 'src'
DOC = PROJECT_ROOT / 'docs' / 'plugin-page-architecture.md'
NODE = shutil.which('node')


def read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def function_body(source: str, signature: str) -> str:
    """截出顶层函数体（SFC 的 script 里函数以行首 ``}`` 结束）。"""
    start = source.index(signature)
    end = source.index('\n}', start)
    return source[start:end]


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
    """组件结构性契约（没有 Vue 单测环境时，用源码断言锁住不变量）。"""

    def test_declared_protocol_messages_are_documented(self) -> None:
        """白名单里的消息类型必须能在文档里找到出处（死协议不再被当成"已支持"）。"""
        doc = read(DOC)
        app = read(SRC / 'App.vue')
        watched = {'omnibox:ready', 'omnibox:chrome', 'omnibox:open-settings',
                   'omnibox:get-plugin-appearance', 'omnibox:set-plugin-alpha'}
        for message in watched:
            self.assertIn(f'`{message}`', doc, f'{message} 未在 {DOC.name} 的消息表里出现')
        body = function_body(app, 'function onPluginChromeMessage(')
        self.assertEqual(set(re.findall(r"data\.type !== '(omnibox:[a-z-]+)'", body)), watched)

    def test_native_panel_url_guard_is_shared(self) -> None:
        """两处 native 面板入口（壳扩展 / 宿主消息）必须共用同一份归一化校验。"""
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

    def test_unsent_close_native_message_is_not_advertised(self) -> None:
        """``omnibox:close-native`` 全仓库没有发送方，处理分支不能留成"已支持"。"""
        source = read(SRC / 'App.vue')
        self.assertIn("'omnibox:open-native'", source)
        self.assertNotIn('omnibox:close-native', source)

    def test_settings_field_types_are_in_the_shared_contract(self) -> None:
        """插件 ``settings_schema`` 用到的 type 必须都在 SchemaField 的联合里。"""
        union_source = read(SRC / 'core' / 'plugin-content.ts')
        match = re.search(r'type\?:\s*([^\n]+)', union_source)
        self.assertIsNotNone(match, 'plugin-content.ts 里找不到 SchemaField.type 联合')
        assert match is not None
        union = set(re.findall(r"'([a-z]+)'", match.group(1)))
        used: set[str] = set()
        for manifest in sorted((PROJECT_ROOT / 'plugins').glob('*/backend/main.py')):
            used.update(re.findall(r'"type":\s*"([a-z]+)"', read(manifest)))
        self.assertTrue(used, '没有从插件后端读到任何字段类型，用例会变成空转')
        self.assertEqual(sorted(used - union), [], f'插件用了契约外的字段类型: {sorted(used - union)}')

    def test_secret_mask_matches_the_backend_contract(self) -> None:
        """掩码回传 = 不改动；前后端必须用同一个字面量，否则会被当成新值写入。"""
        backend = read(PROJECT_ROOT / 'shell' / 'backend' / 'plugin_base.py')
        modal = read(SRC / 'components' / 'PluginSettingsModal.vue')
        self.assertIn("SECRET_MASK = '********'", backend)
        self.assertIn("'********'", modal)


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
