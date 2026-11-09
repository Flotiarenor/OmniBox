"""壳的插件 chrome 协议（`omnibox:*`）行为用例（tests/js/plugin_chrome_protocol.mjs）。

原先 tests/test_shell_ui_review.py 用 `function_body` + 正则从 App.vue 的
`onPluginChromeMessage` 里反推白名单（`data.type !== 'omnibox:x'` 连写）。那种写法
有两个方向的失效：把处理器改个写法就红（假红），加一条没有发送方的分支、
或删掉一条仍在用的分支时，正则与实现一起漂移而仍然是绿的（假绿）。

现在白名单与状态合并规则是 `shell/frontend/src/core/plugin-chrome-protocol.js`，
本用例用 Node 直接 import 真实实现：登记的类型、文档出处、合并语义（badges 清除、
actions / nav / search 按键合并、status 回退、不就地修改）都成了可执行断言。

实现用 `.js` + 同名 `.d.ts`（同 `plugin-url.js`）而不是 `.ts`：Node 直接跑 `.ts`
需要 22.6+ 的 `--experimental-strip-types`，CI 的 Node 版本低于该门槛，会在 CI 上
静默跳过。

运行：
    python -m unittest tests.test_plugin_chrome_protocol_js -v
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'plugin_chrome_protocol.mjs'
MODULE = PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'core' / 'plugin-chrome-protocol.js'
DECLARATION = MODULE.with_suffix('.d.ts')
DOC = PROJECT_ROOT / 'docs' / 'plugin-page-architecture.md'
# 实现里的导出：`export const X` / `export function X`
EXPORT_RE = re.compile(r'^export\s+(?:declare\s+)?(?:const|function)\s+(\w+)', re.MULTILINE)


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过插件 chrome 协议用例')
class PluginChromeProtocolTest(unittest.TestCase):
    def test_harness_reads_real_sources(self):
        """用例引用的实现、类型声明与文档必须真的存在（改名后"全过"不能是空转）。"""
        for file in (HARNESS, MODULE, DECLARATION, DOC):
            self.assertTrue(file.is_file(), f'用例引用的文件不存在: {file}')

    def test_declaration_covers_runtime_exports(self):
        """`.d.ts` 必须声明实现的每个导出。

        实现写成纯 JS 是为了让 Node 直接 import；代价是类型声明要人工跟上，而漏一个的
        表现是 TS 侧报「没有导出成员」（`vue-tsc` 直接失败）。这条把"跟上了"变成断言，
        新增导出时不会忘。
        """
        names = EXPORT_RE.findall(MODULE.read_text(encoding='utf-8'))
        self.assertTrue(names, '没有从实现里解析到任何导出')
        declaration = DECLARATION.read_text(encoding='utf-8')
        missing = [name for name in names
                   if not re.search(rf'\b{re.escape(name)}\b', declaration)]
        self.assertEqual(missing, [], f'.d.ts 未声明的导出: {missing}')

    def test_protocol_whitelist_and_state_merge(self):
        proc = subprocess.run(
            ['node', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'插件 chrome 协议用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
