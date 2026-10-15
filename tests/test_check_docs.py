"""`tools/check_docs.py`（文档引用门禁）的判定逻辑。

为什么需要用例
--------------
这条门禁有两侧风险，都必须钉住：

1. **漏报**：引用真的腐烂了却不报 —— 它读的是 Markdown，没有任何东西会替代它跑；
2. **误报**：报出大量"其实没问题"的引用。第一次接上本仓库时报了 418 条，其中
   415 条来自插件界面契约文档（CSS 类名、DOM 属性、运行时数据文件），没有一条是
   真腐烂。一条会误报的门禁活不过一周 —— 会被绕过、被注释掉、被删掉。

因此用例既构造"必须报"的反例（名字/路径/链接/围栏命令），也构造"必须不报"的
正例（生成物路径、产物后缀、占位符、另一份 .md 里的同名文字、插件界面文档里的
类名）。用例全部在临时仓库里构造，不碰真实文件。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.check_docs import check_docs, scan


class CheckDocsGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def _errors(self) -> list:
        return check_docs(self.root)

    # ---------- 仓库现状必须过 ----------

    def test_real_repo_docs_pass(self):
        """仓库自己的 AGENTS.md 必须过门禁（防止门禁与现状漂移）。"""
        self.assertEqual(check_docs(PROJECT_ROOT), [])

    def test_scan_counts_docs_and_refs(self):
        self._write('AGENTS.md', '# 根\n\n`thing.py` 是入口。\n')
        self._write('thing.py', 'x = 1\n')

        result = scan(self.root)

        self.assertEqual(result.docs, 1)
        self.assertGreater(result.refs, 0)
        self.assertEqual(result.errors, [])

    # ---------- 名字 ----------

    def test_missing_name_is_reported(self):
        self._write('mod/AGENTS.md', '# mod\n\n关键符号 `real_name` 与 `ghost_name`。\n')
        self._write('mod/code.py', 'def real_name():\n    pass\n')

        errors = self._errors()

        self.assertEqual(len(errors), 1, errors)
        self.assertIn('mod/AGENTS.md', errors[0])
        self.assertIn('ghost_name', errors[0])

    def test_name_only_in_another_markdown_does_not_count(self):
        """另一份 .md 里的同名文字不能替被删掉的符号背书（所以只搜代码文件）。"""
        self._write('AGENTS.md', '# 根\n\n关键符号 `ghost_name`。\n')
        self._write('docs/notes.md', '这里提到过 ghost_name 这个词。\n')

        errors = self._errors()

        self.assertEqual(len(errors), 1, errors)
        self.assertIn('ghost_name', errors[0])

    def test_dotted_module_path_resolves(self):
        self._write('AGENTS.md', '# 根\n\n`tests.test_alpha` 是入口用例。\n')
        self._write('tests/test_alpha.py', 'import unittest\n')

        self.assertEqual(self._errors(), [])

    def test_placeholder_and_artifact_names_are_not_reported(self):
        self._write('AGENTS.md', '# 根\n\n版本号写成 `x.y.z`；产物是 `OmniBox.zip`。\n')

        self.assertEqual(self._errors(), [])

    def test_external_name_is_allowed(self):
        self._write('AGENTS.md', '# 根\n\n由 `git_commit` 在写库前校验。\n')

        self.assertEqual(self._errors(), [])

    # ---------- 路径 ----------

    def test_missing_path_is_reported(self):
        self._write('mod/AGENTS.md', '# mod\n\n见 `mod/gone.py`。\n')
        self._write('mod/here.py', 'x = 1\n')

        errors = self._errors()

        self.assertEqual(len(errors), 1, errors)
        self.assertIn('mod/gone.py', errors[0])

    def test_path_resolves_from_doc_dir_and_repo_root(self):
        self._write('mod/AGENTS.md', '# mod\n\n`here.py` 与 `res/icons/icons.svg`。\n')
        self._write('mod/here.py', 'x = 1\n')
        self._write('res/icons/icons.svg', '<svg/>\n')

        self.assertEqual(self._errors(), [])

    def test_bare_filename_resolves_anywhere_in_repo(self):
        self._write('AGENTS.md', '# 根\n\n每个插件都有 `manifest.json`。\n')
        self._write('plugins/alpha/manifest.json', '{}\n')

        self.assertEqual(self._errors(), [])

    def test_generated_paths_are_not_reported(self):
        """`dist/` 在 .gitignore 里，文档引用它是说明产物落在哪。"""
        self._write('AGENTS.md', '# 根\n\n首页由 `dist/index.html` 提供。\n')

        self.assertEqual(self._errors(), [])

    def test_runtime_url_is_not_a_repo_path(self):
        """前导斜杠是运行时 URL（`/shell/base.js`），不是仓库路径。"""
        self._write('AGENTS.md', '# 根\n\n插件页面引用 `/shell/base.js`。\n')

        self.assertEqual(self._errors(), [])

    def test_slug_must_exist_as_directory(self):
        self._write('AGENTS.md', '# 根\n\n脚手架模板 `hello-world` 与 `ghost-plugin`。\n')
        self._write('tools/examples/hello-world/manifest.json', '{}\n')

        errors = self._errors()

        self.assertEqual(len(errors), 1, errors)
        self.assertIn('ghost-plugin', errors[0])

    # ---------- 围栏代码块 ----------

    def test_fenced_path_is_checked(self):
        self._write('AGENTS.md', '# 根\n\n```bash\npython tools/gone.py\n```\n')

        errors = self._errors()

        self.assertEqual(len(errors), 1, errors)
        self.assertIn('围栏命令里的路径', errors[0])
        self.assertIn('tools/gone.py', errors[0])

    def test_existing_fenced_path_passes(self):
        self._write('AGENTS.md', '# 根\n\n```bash\npython tools/real.py --check\n```\n')
        self._write('tools/real.py', 'x = 1\n')

        self.assertEqual(self._errors(), [])

    def test_fenced_names_are_not_checked(self):
        """围栏块是照抄去执行的命令：参数与占位符不做名字判定。"""
        self._write('AGENTS.md', '# 根\n\n```bash\npython -m unittest tests.not_a_symbol\n```\n')

        self.assertEqual(self._errors(), [])

    # ---------- Markdown 链接 ----------

    def test_missing_markdown_link_is_reported(self):
        self._write('AGENTS.md', '# 根\n\n见 [模块文档](mod/AGENTS.md)。\n')

        errors = self._errors()

        self.assertEqual(len(errors), 1, errors)
        self.assertIn('Markdown 链接', errors[0])

    def test_existing_link_and_external_link_pass(self):
        self._write('AGENTS.md', '# 根\n\n见 [模块文档](mod/AGENTS.md) 与 [在线](https://example.com/a)。\n')
        self._write('mod/AGENTS.md', '# mod\n')

        self.assertEqual(self._errors(), [])

    # ---------- 插件界面文档 ----------

    def test_plugin_interface_doc_skips_names_but_checks_links(self):
        self._write('plugins/alpha/AGENTS.md',
                    '# alpha\n\n界面类名 `nr-card`，脚本 `js/app.js`。\n\n见 [缺失](missing.md)。\n')

        errors = self._errors()

        self.assertEqual(len(errors), 1, errors)
        self.assertIn('Markdown 链接', errors[0])

    def test_same_tokens_in_module_doc_are_reported(self):
        """边界固化：同一批 token 出现在模块文档里就必须报（判据按文档类型分开）。"""
        self._write('mod/AGENTS.md', '# mod\n\n界面类名 `nr-card`，脚本 `js/app.js`。\n')

        errors = self._errors()

        self.assertEqual(len(errors), 2, errors)

    # ---------- 作用域本身 ----------

    def test_declared_extra_scope_must_exist(self):
        self._write('shell/backend/AGENTS.md', '# backend\n')

        errors = self._errors()

        self.assertEqual(len(errors), 1, errors)
        self.assertIn('docs/Releases', errors[0])

    def test_skipped_dirs_are_not_scanned(self):
        """venv / node_modules / .build 里的同名文档不参与（那是依赖与产物）。"""
        self._write('venv/lib/AGENTS.md', '# 依赖里的文档\n\n`ghost_name`\n')

        result = scan(self.root)

        self.assertEqual(result.docs, 0)
        self.assertEqual(result.errors, [])


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
