"""本地隐私门禁（`tools/check_private_paths.py`）的用例。

为什么需要用例：这个门禁读的是**开发机配置**（CI 上没有，会 SKIP），因此它在 CI 上
永远是绿的 —— 一旦提取逻辑或归一化写错，本机也会静默放过真泄漏。这里用临时目录造一份
"本机配置 + 被跟踪文件"，把两类错误都钉住：

1. 应当命中的：真机路径（不同转义写法、只抄上层目录）、配置里的密钥值、路径上下文里的
   用户名；
2. 不应当命中的：回环地址 / 主机名这类通用值、占位符形状的"密钥"、没有本地配置时。

配置文件一律用 `json.dumps` 生成：手写转义太容易把 `\\\\` 与 `\\` 写混，用例本身就成了
"另一种形式的错误"。
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_SPEC = importlib.util.spec_from_file_location(
    'check_private_paths', str(PROJECT_ROOT / 'tools' / 'check_private_paths.py'))
cpp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cpp)


class PrivatePathGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / '.config' / 'plugins').mkdir(parents=True)

    def _config(self, name: str, data: dict) -> None:
        (self.root / '.config' / 'plugins' / name).write_text(
            json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def _tracked(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def _scan(self, *files) -> list:
        return cpp.scan(self.root, list(files), cpp.collect_tokens(self.root))

    # ---------- 应当命中 ----------

    def test_real_root_path_is_caught_in_every_escaping(self):
        self._config('manga-library.json', {'root_dir': r'D:\图库\本子'})
        cases = {
            'a.md': r'参考目录 D:\图库 的形态',          # 单反斜杠（Markdown / 注释）
            'b.py': '"""`D:\\\\图库` 那种目录走一遍很慢"""',   # 源码里两个反斜杠
            'c.js': "const p = 'D:/图库';",                  # 正斜杠
            'd.py': r'# D:\图库\本子',                       # 完整路径
        }
        hits = self._scan(*[self._tracked(rel, text) for rel, text in cases.items()])

        self.assertEqual(sorted({rel for rel, *_ in hits}), sorted(cases),
                         f'每种写法都要命中，实际: {hits}')

    def test_text_split_path_is_out_of_scope(self):
        """边界固化：路径被拆成多段字符串拼接后不可检测（文本门禁的固有边界）。

        记下来免得日后误以为"门禁能拦一切"：拼接形态也不是"顺手复制粘贴"的形态。
        """
        self._config('manga-library.json', {'root_dir': r'D:\图库\本子'})
        split = self._tracked('e.py', 'x = "D:" + "\\\\" + "图库"')

        self.assertEqual(self._scan(split), [])

    def test_secret_value_from_config_is_caught(self):
        self._config('pixiv-sync.json', {'refresh_token': 'r4kQ9vLmZ7pXe2F5Q'})
        target = self._tracked('docs/x.md', '示例 token: r4kQ9vLmZ7pXe2F5Q')

        hits = self._scan(target)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][2], '密钥')

    def test_username_only_in_path_context(self):
        name = Path(os.environ.get('USERPROFILE') or os.environ.get('HOME') or '').name
        if len(name) < 4:
            self.skipTest('本机用户名太短，跳过')
        in_path = self._tracked('a.py', f'root = r"C:\\Users\\{name}\\media"')
        bare = self._tracked('b.py', f'# 这行只是恰好包含 {name} 这个词')

        hits = self._scan(in_path, bare)

        self.assertEqual([rel for rel, *_ in hits], ['a.py'], '只在路径上下文里算命中')

    # ---------- 不应当命中 ----------

    def test_generic_hosts_and_placeholders_are_ignored(self):
        (self.root / '.config' / 'app.yaml').write_text(
            'server:\n  host: 127.0.0.1\n  base: http://127.0.0.1:18080\n', encoding='utf-8')
        self._config('alpha.json', {'refresh_token': 'SECRET-TOKEN'})
        targets = [
            self._tracked('a.py', "host = '127.0.0.1'"),
            self._tracked('b.md', '默认 http://127.0.0.1:18080'),
            self._tracked('c.py', "self.assertTrue(update('refresh_token', 'SECRET-TOKEN'))"),
        ]

        self.assertEqual(self._scan(*targets), [], '通用值与占位符不该报')

    def test_gate_skips_without_local_config(self):
        """CI 场景：没有本地配置时 SKIP 并以 0 退出（用户名不算"有配置"）。"""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cpp.main(['--root', str(self.root)])

        self.assertEqual(code, 0)
        self.assertIn('SKIP', buffer.getvalue())


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
