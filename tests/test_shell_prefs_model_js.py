"""壳偏好纯数据层的契约用例（tests/js/shell_prefs_model.mjs）。

为什么需要一个 Node 侧的用例：壳偏好的归一化规则在前端（`prefs-model.ts`）与后端
（`shell_prefs.py`）各有一份 —— 两边不一致时的现象是"设置页改了没反应"（前端认为
合法、后端整条拒收并回退默认），而从任何一侧的单测都看不出来。这个用例同时跑前端
真实源码与后端源码里的常量，逐条比对。

`node --experimental-strip-types`（Node 22.6+）只做类型擦除，因此 `prefs-model.ts`
刻意不 import 任何模块；旧版本 Node 没有这个开关时跳过，而不是把环境问题报成失败。

运行：
    python -m unittest tests.test_shell_prefs_model_js -v
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HARNESS = PROJECT_ROOT / 'tests' / 'js' / 'shell_prefs_model.mjs'


def _node_supports_type_stripping() -> bool:
    """本机 node 能不能直接跑 .ts（Node 22.6 起有 --experimental-strip-types）。"""
    node = shutil.which('node')
    if not node:
        return False
    try:
        proc = subprocess.run([node, '--version'], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    raw = (proc.stdout or '').strip().lstrip('v')
    try:
        major, minor = (int(part) for part in raw.split('.')[:2])
    except ValueError:
        return False
    return (major, minor) >= (22, 6)


@unittest.skipUnless(shutil.which('node'), '未检测到 node，跳过壳偏好契约用例')
@unittest.skipUnless(_node_supports_type_stripping(),
                     'node 版本过低（需要 22.6+ 的 --experimental-strip-types）')
class ShellPrefsModelContractTest(unittest.TestCase):
    def test_frontend_rules_match_backend(self):
        proc = subprocess.run(
            ['node', '--experimental-strip-types', str(HARNESS)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=120,
        )
        if proc.returncode != 0:
            self.fail(f'壳偏好契约用例失败（exit={proc.returncode}）\n'
                      f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')

    def test_harness_reads_real_sources(self):
        """门禁用例自己也要防漂移：它比对的两个文件必须真的存在。

        文件改名后如果不报错，用例会"全部通过"而什么也没比对 —— 这类静默失效比
        没有用例更危险。
        """
        from shell.backend.shell_prefs import _VAR_NAME_RE, _VAR_VALUE_MAX

        model = PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'core' / 'prefs-model.ts'
        rules = PROJECT_ROOT / 'shell' / 'frontend' / 'src' / 'core' / 'var-rules.ts'
        for file in (model, rules, HARNESS):
            self.assertTrue(file.is_file(), f'契约用例引用的文件不存在: {file}')
        self.assertEqual(_VAR_NAME_RE.pattern, r'^--[a-z0-9-]{1,40}$')
        self.assertEqual(_VAR_VALUE_MAX, 64)
        # 后端默认偏好与前端默认偏好是同一组键（用例里逐条比对的是常量，这里补一条
        # "键集"的断言，避免只对正则不对结构）
        from shell.backend.shell_prefs import default_prefs

        keys = set(json.loads(json.dumps(default_prefs())))
        self.assertEqual(keys, {'theme', 'colors', 'background', 'startup', 'last_route'})


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
