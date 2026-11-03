"""共享运行时拆分后的结构契约：分段清单、公共全局名、单文件体积。

为什么需要：`shell/frontend/public/shell/base.js` 原先是一个两千多行的脚本，注入每个插件页；
改动任何一处都会影响全部插件，而它内部靠 `window.Utils` / `window.Bridge` 共享全局。
按区段拆成八段之后（`kernel` 那一层仍叫 `base.js`，与文档和既有门禁的名字保持一致），
**回归风险不是"某行写错"，而是结构性的退化**：

1. 某一段没被注入（插件页少一整类组件，页面看起来正常）；
2. 拆完之后公共全局名丢了或被改名（`Utils.escapeHtml` / `Bridge.call` / `createTree` …）；
3. 单文件又长回上帝脚本。

这三条都能在纯文本层面判定，不需要起 Flask 或浏览器；"每段可按 URL 取到"由
`tests/test_file_server_host.py` 用真实 app 断言。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHELL_PUBLIC = PROJECT_ROOT / 'shell' / 'frontend' / 'public' / 'shell'
FILE_SERVER = PROJECT_ROOT / 'shell' / 'backend' / 'file_server.py'

# 拆分后的共享运行时（注入顺序）：base 承载 Bridge / Utils / 生命周期钩子，其余按区段分文件。
# chrome-client.js 是内容页 ↔ 壳的握手（四个内容页共用），排在运行时这一层末尾。
RUNTIME_SCRIPTS = (
    'base.js',
    'dialogs.js',
    'settings.js',
    'host-channel.js',
    'tree.js',
    'lightbox.js',
    'pagination.js',
    'context-menu.js',
    'chrome-client.js',
)

# 单文件行数棘轮：拆分前是 2,149 行；当前最大的一段约 460 行，留出改写余量。
MAX_LINES_PER_SCRIPT = 600

# 必须继续存在的公共全局名（形状不变是拆分的前提：插件与共享组件都按名字取用）。
REQUIRED_GLOBALS = {
    'base.js': ('window.PluginLifecycle', 'window.Bridge', 'window.Utils'),
    'host-channel.js': ('window.HostChannel',),
    'dialogs.js': ('window.Shell.dialog',),
    # 内容页 ↔ 壳的握手：四个内容页按这个名字取用（tests/js/chrome_client.mjs 断言行为）
    'chrome-client.js': ('window.ChromeClient',),
}

# 七个按需调用的裸全局工厂：插件直接以顶层函数名调用。
REQUIRED_FACTORIES = {
    'dialogs.js': ('confirmDialog',),
    'settings.js': ('openSettingsModal', 'openModal'),
    'tree.js': ('createTree',),
    'lightbox.js': ('createLightbox',),
    'pagination.js': ('createPagination',),
    'context-menu.js': ('createContextMenu',),
}


def read(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def bootstrap_scripts() -> list[str]:
    """引导片段里按注入顺序出现的 /shell/*.js（去重后保持顺序）。"""
    text = read(FILE_SERVER)
    seen: list[str] = []
    for name in re.findall(r'src="/shell/([\w.-]+\.js)"', text):
        if name not in seen:
            seen.append(name)
    return seen


def runtime_layer() -> list[str]:
    """共享运行时这一层：toast.js 之后、folder-picker.js 之前。"""
    order = bootstrap_scripts()
    try:
        start = order.index('toast.js')
        end = order.index('folder-picker.js')
    except ValueError as exc:  # pragma: no cover - 引导片段结构被改坏时会走到
        raise AssertionError(f'引导片段里缺少 toast.js / folder-picker.js 定位点: {order}') from exc
    return order[start + 1:end]


class SharedRuntimeSplitTests(unittest.TestCase):
    def test_runtime_layer_is_exactly_the_split_files(self) -> None:
        """注入清单里的运行时层必须正好是这八段（不多、不少、顺序一致）。"""
        self.assertEqual(runtime_layer(), list(RUNTIME_SCRIPTS))

    def test_every_segment_exists_and_stays_small(self) -> None:
        """分段文件都在，且没有哪一段又长回上帝脚本。"""
        for name in RUNTIME_SCRIPTS:
            with self.subTest(script=name):
                path = SHELL_PUBLIC / name
                self.assertTrue(path.is_file(), f'缺少分段文件 {path}')
                lines = len(read(path).splitlines())
                self.assertLessEqual(
                    lines, MAX_LINES_PER_SCRIPT,
                    f'{name} 有 {lines} 行，超过 {MAX_LINES_PER_SCRIPT} 行的上限：'
                    f'共享运行时按区段拆分的目的是别让单文件再长回上帝脚本',
                )

    def test_public_globals_are_preserved(self) -> None:
        """拆开的是文件，不是接口：插件按名字取用的全局名一个都不能少。"""
        for name, globals_ in REQUIRED_GLOBALS.items():
            source = read(SHELL_PUBLIC / name)
            for symbol in globals_:
                with self.subTest(script=name, symbol=symbol):
                    self.assertIn(f'{symbol} =', source, f'{name} 里不再定义 {symbol}')

    def test_bare_component_factories_are_preserved(self) -> None:
        """七个裸全局工厂仍然是顶格函数声明（插件以顶层函数名直接调用）。"""
        for name, factories in REQUIRED_FACTORIES.items():
            source = read(SHELL_PUBLIC / name)
            for factory in factories:
                with self.subTest(script=name, factory=factory):
                    self.assertRegex(
                        source, rf'(?m)^function {factory}\(',
                        f'{name} 里不再有顶格的 function {factory}(',
                    )

    def test_utils_still_lives_in_base_js(self) -> None:
        """`window.Utils` 必须仍在 base.js：两份门禁按符号名从该文件截取这段实现。"""
        source = read(SHELL_PUBLIC / 'base.js')
        start = source.find('window.Utils =')
        self.assertGreaterEqual(start, 0, 'base.js 里找不到 window.Utils 定义')
        block = source[start:start + 2000]
        for method in ('escapeHtml', 'iconHtml', 'jsString', 'formatFileSize', 'debounce'):
            with self.subTest(method=method):
                self.assertIn(f'{method}(', block, f'window.Utils 少了 {method}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
