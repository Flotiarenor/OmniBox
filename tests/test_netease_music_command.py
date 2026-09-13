"""ncm-cli 调用链的命令注入回归测试（docs/code-review.md §3.2）。

netease-music 插件**只支持 Windows**（ncm-cli 通过 npm 的 .cmd shim 落地，
假 mpv 方案也依赖 .cmd 与 mpv.exe），因此伪造 npm 全局布局的这一组断言只在
Windows 上运行，其它平台整体 skip —— 不为了让测试在 Linux 变绿而给插件加上
POSIX 分支。与平台无关的纯函数（_reject_shell_meta）单独成类，各平台都跑。

Windows 上不依赖真实 ncm-cli 与网络：在临时目录里伪造一个 npm 全局安装布局
（ncm-cli.cmd shim + node_modules/@music163/ncm-cli/dist/index.js），
把该目录放到 PATH 最前，然后断言两件事：

1. 解析出的是 node 入口，不是 .cmd shim（cmd.exe 不再参与参数解析）；
2. 搜索关键词里的 `" & echo INJECTED & "` 只会作为**一个参数**原样传给
   ncm-cli，不会被当成命令分隔符执行。

运行：
    python -m unittest tests.test_netease_music_command -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_MODULE_PATH = PROJECT_ROOT / 'plugins' / 'netease-music' / 'backend' / 'netease_music_api.py'

_spec = importlib.util.spec_from_file_location('netease_music_api_under_test', _MODULE_PATH)
ncm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ncm)

_FAKE_ENTRY_JS = """\
const args = process.argv.slice(2);
console.log(JSON.stringify(args));
"""

_FAKE_SHIM = "@ECHO off\r\nrem 伪造的 npm shim：真实调用应当绕过它\r\n"


class ShellMetaRejectionTests(unittest.TestCase):
    """退化到 .cmd shim 时的元字符拦截：纯函数，与运行平台无关。"""

    def test_shell_meta_rejected_only_on_fallback_path(self):
        """退化到 .cmd shim 时，元字符必须被拒绝而不是被 cmd.exe 解释。"""
        self.assertEqual(ncm._reject_shell_meta('plain'), 'plain')
        for bad in ['a"b', 'a & b', 'a|b', 'a>b', 'a%PATH%', 'a!b', 'a\nb']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ncm._reject_shell_meta(bad)


@unittest.skipUnless(
    os.name == 'nt',
    'netease-music 插件当前只支持 Windows（npm .cmd shim + 假 mpv .cmd）',
)
class NcmCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        npm_dir = Path(self._tmp.name)

        # 伪造 npm 全局布局
        (npm_dir / 'ncm-cli.cmd').write_text(_FAKE_SHIM, encoding='utf-8')
        entry_dir = npm_dir / 'node_modules' / '@music163' / 'ncm-cli' / 'dist'
        entry_dir.mkdir(parents=True)
        (entry_dir / 'index.js').write_text(_FAKE_ENTRY_JS, encoding='utf-8')

        self._old_path = os.environ.get('PATH', '')
        os.environ['PATH'] = f"{npm_dir}{os.pathsep}{self._old_path}"
        self.addCleanup(self._restore_path)

    def _restore_path(self):
        os.environ['PATH'] = self._old_path

    def test_prefix_avoids_cmd_shim(self):
        """解析结果必须是 node + index.js，而不是 .cmd shim。"""
        prefix = ncm._ncm_argv_prefix()
        self.assertFalse(
            prefix[0].lower().endswith(('.cmd', '.bat', '.ps1')),
            f'仍然解析到 shell shim：{prefix[0]}',
        )
        self.assertEqual(Path(prefix[1]).name, 'index.js')
        self.assertEqual(prefix[2:], ['--output', 'json'])

    def test_keyword_is_passed_as_single_argument(self):
        api = ncm.NeteaseMusicAPI(check_install=False)
        payload = 'a" & echo INJECTED & "b'
        result = api._run_command(['search', 'song', '--keyword', payload])

        argv = json.loads(result['stdout'])
        self.assertIn(payload, argv, '关键词没有原样传给 ncm-cli')
        # 注入成功的标志是 "INJECTED" 变成独立参数或被单独执行
        self.assertNotIn('INJECTED', argv)
        # 前两个参数是固定的 --output json 前缀
        self.assertEqual(argv[2:4], ['search', 'song'])
        self.assertEqual(argv[-2:], ['--keyword', payload])

    def test_keyword_with_quotes_and_ampersands_survives(self):
        """带引号/与号的歌名本来搜索就会失败，现在应当能正常传参。"""
        api = ncm.NeteaseMusicAPI(check_install=False)
        for keyword in ['LOVE"', 'A & B', 'x|y', '%CD%']:
            with self.subTest(keyword=keyword):
                result = api._run_command(['search', 'song', '--keyword', keyword])
                argv = json.loads(result['stdout'])
                self.assertIn(keyword, argv)

    def test_constant_command_string_still_supported(self):
        api = ncm.NeteaseMusicAPI(check_install=False)
        result = api._run_command('playlist list')
        self.assertEqual(json.loads(result['stdout'])[2:], ['playlist', 'list'])


if __name__ == '__main__':
    unittest.main()
