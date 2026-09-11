"""运行时禁止 print 的门禁（docs/code-review.md §4.2）。

发行版打包 console=False，`sys.stdout` 是 None，`print` 会被静默丢弃 —— 所以
"运行时代码里每多一处 print，就多一处发布后看不见的诊断信息"。这条门禁把
"用 logging"从纪律变成机器检查。

允许保留 print 的只有两类（各有明确理由，不是遗漏）：
  1. 面向终端的工具/脚本：stdout 就是它们的产物（CI 报告、交互提示）；
  2. shell/backend/app_logging.py 自身：日志还没建立时的兜底输出。

运行：
    python -m unittest tests.test_no_print_in_runtime -v
"""

import ast
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 运行时入口与内核
RUNTIME_ROOTS = ('main.py', 'shell/backend')
# 插件后端（libs/ 是 vendored 第三方，不属于本仓库维护范围）
PLUGIN_GLOB = 'plugins/*/backend/**/*.py'

ALLOWED = {
    # 日志初始化失败时的兜底：此时 logging 还用不了
    'shell/backend/app_logging.py',
    # pixiv-sync 的两个伴侣 CLI 工具：交互提示就是产品本身
    'plugins/pixiv-sync/backend/pixiv_sync/pixiv_purge_non_original.py',
    'plugins/pixiv-sync/backend/pixiv_sync/pixiv_unmark_recent.py',
}


def _runtime_files():
    files = []
    for rel in RUNTIME_ROOTS:
        path = PROJECT_ROOT / rel
        if path.is_file():
            files.append(path)
        else:
            files.extend(path.rglob('*.py'))
    files.extend(PROJECT_ROOT.glob(PLUGIN_GLOB))
    result = []
    for path in files:
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if rel in ALLOWED or '/libs/' in rel or '__pycache__' in rel:
            continue
        result.append((rel, path))
    return sorted(result)


def _print_calls(path: Path):
    """返回真正的 print(...) 调用行号（用 AST，不会误判字符串里的 'print('）。"""
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (SyntaxError, UnicodeDecodeError) as e:  # pragma: no cover - 语法错误另有门禁
        raise AssertionError(f'{path} 解析失败: {e}') from e
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'print':
            lines.append(node.lineno)
    return sorted(lines)


class NoPrintInRuntimeTests(unittest.TestCase):
    def test_runtime_code_uses_logging_not_print(self):
        offenders = []
        for rel, path in _runtime_files():
            lines = _print_calls(path)
            if lines:
                offenders.append(f'{rel}: 第 {", ".join(str(n) for n in lines)} 行')
        self.assertEqual(
            offenders, [],
            '运行时代码必须用 logging（发行版没有控制台，print 会静默丢失）:\n  '
            + '\n  '.join(offenders),
        )

    def test_scan_actually_covered_runtime_files(self):
        """防止门禁本身失效：至少要扫到内核与全部插件后端。"""
        scanned = {rel for rel, _ in _runtime_files()}
        self.assertIn('main.py', scanned)
        self.assertIn('shell/backend/plugin_manager.py', scanned)
        self.assertIn('shell/backend/file_server.py', scanned)
        for expected in ('media-player', 'image-viewer', 'image-cleaner',
                         'manga-library', 'novel-reader', 'pixiv-sync', 'netease-music'):
            self.assertIn(f'plugins/{expected}/backend/main.py', scanned, f'漏扫 {expected}')
        self.assertGreater(len(scanned), 15)

    def test_logging_supports_plugin_logger_names(self):
        """插件用 logging.getLogger(__name__)（__name__ 是内核注入的自定义模块名）。"""
        text = (PROJECT_ROOT / 'shell/backend/plugin_manager.py').read_text(encoding='utf-8')
        self.assertIn('log = logging.getLogger(__name__)', text)


if __name__ == '__main__':
    unittest.main()
