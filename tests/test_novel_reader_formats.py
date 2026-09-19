"""novel-reader 多格式读取契约（Markdown / EPUB / pdf / 外部打开）。

守三件事：

1. 富文本 HTML 由后端白名单转换器产出：EPUB/Markdown 里的 `<script>`、`on*` 属性、
   `javascript:` 链接不能进到与壳同源的插件 iframe（那里有 Bridge）；
2. zip 成员名不可信：越界成员既不落盘，也不出现在 HTML 里；
3. 进度键从"去扩展名的文件名"改成完整文件名后，旧进度仍要读得出来。

运行：
    venv\\Scripts\\python.exe -m unittest tests.test_novel_reader_formats -v
"""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'novel-reader'
OPF_PATH = 'OEBPS/content.opf'


def _chapter_xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head>'
        f'<body><h1>{title}</h1>{body}</body></html>'
    )


def _write_epub(path: Path, docs, nav=None, extra=None) -> None:
    """写一个最小合法 EPUB：mimetype + container.xml + OPF + nav.xhtml + 正文。"""
    items, spine = [], []
    for index, (href, _title, _body) in enumerate(docs):
        items.append(f'<item id="doc{index}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="doc{index}"/>')
    nav_item, links = '', ''
    if nav is not None:
        nav_item = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        links = ''.join(f'<li><a href="{href}">{title}</a></li>' for href, title in nav)
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="bookid">urn:uuid:test</dc:identifier>'
        '<dc:title>测试书</dc:title><dc:language>zh</dc:language></metadata>'
        f'<manifest>{nav_item}{"".join(items)}</manifest>'
        f'<spine>{"".join(spine)}</spine></package>'
    )
    nav_doc = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        f'<head><title>目录</title></head><body><nav epub:type="toc"><ol>{links}</ol></nav></body></html>'
    )
    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        f'<rootfiles><rootfile full-path="{OPF_PATH}" media-type="application/oebps-package+xml"/>'
        '</rootfiles></container>'
    )

    with zipfile.ZipFile(str(path), 'w') as zf:
        zf.writestr(zipfile.ZipInfo('mimetype'), 'application/epub+zip',
                    compress_type=zipfile.ZIP_STORED)
        zf.writestr('META-INF/container.xml', container)
        zf.writestr(OPF_PATH, opf)
        if nav is not None:
            zf.writestr('OEBPS/nav.xhtml', nav_doc)
        for href, title, body in docs:
            zf.writestr(f'OEBPS/{href}', _chapter_xhtml(title, body))
        for name, data in (extra or {}).items():
            zf.writestr(name, data)


class NovelReaderFormatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.docs = self.root / 'docs'
        self.docs.mkdir()
        self.plugin = self._new_plugin()

    def _new_plugin(self, root_dirs=None):
        from tools.check_plugins import _load_backend_class

        cls = _load_backend_class(PLUGIN_DIR, 'backend/main.py', 'NovelReaderPlugin')
        # 直接喂"已解决设置"，省掉 SettingsStore 与一次 root_dir 切换
        raw = root_dirs if root_dirs is not None else str(self.docs)
        cls._resolved_config = {'root_dir': '\n'.join(raw) if isinstance(raw, list) else raw}
        return cls({'name': 'novel-reader'}, {'directories': {'data_root': str(self.root / 'data')}})

    # ===== 列表与格式分派 =====

    def test_lists_supported_formats_and_skips_unknown(self):
        (self.docs / 'a.txt').write_text('第一章\n正文\n', encoding='utf-8')
        (self.docs / 'b.md').write_text('# 标题\n正文\n', encoding='utf-8')
        (self.docs / '2024年度-报告.docx').write_bytes(b'PK\x03\x04')
        (self.docs / 'note.log').write_text('x', encoding='utf-8')

        items = {n['id']: n for n in self.plugin.list_novels()['novels']}
        self.assertEqual(set(items), {'a.txt', 'b.md', '2024年度-报告.docx'})
        self.assertEqual(items['b.md']['kind'], 'md')
        self.assertEqual(items['2024年度-报告.docx']['kind'], 'external')
        # 非 txt 不按 '-' 拆作者：'2024年度-报告' 是标题本身
        self.assertEqual(items['2024年度-报告.docx']['title'], '2024年度-报告')
        self.assertEqual(items['a.txt']['title'], 'a')
        # 主目录下的文件没有子目录可显示
        self.assertEqual(items['a.txt']['dir'], '')

    def test_scans_subdirectories_and_multiple_roots(self):
        nested = self.docs / '小说' / '武侠'
        nested.mkdir(parents=True)
        (self.docs / '顶层.txt').write_text('第一章\n正文\n', encoding='utf-8')
        (nested / '剑来.epub').write_bytes(b'PK\x03\x04')
        # 隐藏目录（自己的 .novel_state 就走这条路）不进列表
        hidden = self.docs / '.hidden'
        hidden.mkdir()
        (hidden / '藏起来.txt').write_text('第一章\n', encoding='utf-8')

        second = self.root / '第二根'
        second.mkdir()
        (second / '另一本.md').write_text('# 一\n正文\n', encoding='utf-8')
        self.plugin = self._new_plugin([str(self.docs), str(second)])

        items = {n['id']: n for n in self.plugin.list_novels()['novels']}
        self.assertEqual(set(items), {'顶层.txt', '小说/武侠/剑来.epub', '另一本.md'})
        # id 是相对各自根目录的路径，主目录第一层仍是文件名（老进度键不受影响）
        self.assertEqual(items['小说/武侠/剑来.epub']['dir'], '小说/武侠')
        self.assertEqual(items['小说/武侠/剑来.epub']['title'], '剑来')
        self.assertEqual(items['另一本.md']['dir'], '')
        # 两个根目录都要能被文件路由访问（EPUB 图片按绝对路径引用）
        self.assertEqual([str(path) for path in self.plugin.get_file_roots()],
                         [str(self.docs.resolve()), str(second.resolve())])
        # 主目录是第一根：缓存与阅读进度存在它下面
        self.assertEqual(str(self.plugin.get_data_root()), str(self.docs.resolve()))

    def test_pdf_and_external_have_no_chapters(self):
        (self.docs / 'c.pdf').write_bytes(b'%PDF-1.4\n')
        (self.docs / 'd.docx').write_bytes(b'PK\x03\x04')

        kinds = {n['id']: n['kind'] for n in self.plugin.list_novels()['novels']}
        self.assertEqual(kinds, {'c.pdf': 'pdf', 'd.docx': 'external'})
        self.assertEqual(self.plugin.get_chapters('c.pdf')['chapters'], [])
        self.assertEqual(self.plugin.get_chapters('d.docx')['chapters'], [])
        self.assertEqual(self.plugin.get_content('c.pdf', 0)['error'], '该格式不支持在阅读器内打开')

    # ===== Markdown =====

    def test_markdown_chapters_and_escaped_raw_tags(self):
        (self.docs / 'book.md').write_text(
            '前言正文\n\n'
            '# 第一章 起点\n\n'
            '普通段落与 <script>alert(1)</script>\n\n'
            '| A | B |\n| - | - |\n| 1 | 2 |\n\n'
            '## 小节\n\n'
            '- 项目一\n',
            encoding='utf-8',
        )
        self.plugin.list_novels()

        chapters = self.plugin.get_chapters('book.md')['chapters']
        self.assertEqual([c['title'] for c in chapters], ['前言', '第一章 起点', '小节'])

        result = self.plugin.get_content('book.md', 1)
        self.assertEqual(result['format'], 'html')
        self.assertIn('<h1>', result['content'])
        self.assertIn('<table>', result['content'])
        # markdown-it 的 html=False：原文标签只能是转义后的文本
        self.assertNotIn('<script', result['content'])
        self.assertIn('&lt;script&gt;', result['content'])

    def test_markdown_local_image_goes_through_file_route(self):
        (self.docs / 'pic.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        (self.docs / 'notes.md').write_text('# 一\n\n![图](pic.png)\n\n![外链](https://x/y.png)\n',
                                            encoding='utf-8')
        self.plugin.list_novels()

        html = self.plugin.get_content('notes.md', 0)['content']
        # 图片 URL 用绝对路径：多根目录下相对路径只认第一根
        self.assertIn(f'src="/file?path={quote(str((self.docs / "pic.png").resolve()))}&amp;plugin=novel-reader"',
                      html)
        self.assertNotIn('x/y.png', html)

    # ===== EPUB =====

    def test_epub_chapters_from_nav_and_html_is_sanitized(self):
        _write_epub(
            self.docs / 'book.epub',
            docs=[
                ('ch1.xhtml', '第一章',
                 '<p>正文一&nbsp;结尾</p><img src="Images/pic.png" onerror="alert(1)"/>'
                 '<a href="javascript:alert(2)">链接</a><script>alert(3)</script>'),
                ('ch2.xhtml', '第二章', '<p>正文二</p>'),
            ],
            nav=[('ch1.xhtml', '目录一'), ('ch2.xhtml', '目录二')],
            extra={'OEBPS/Images/pic.png': b'\x89PNG\r\n\x1a\n'},
        )
        self.plugin.list_novels()

        chapters = self.plugin.get_chapters('book.epub')['chapters']
        self.assertEqual([c['title'] for c in chapters], ['目录一', '目录二'])
        self.assertGreater(chapters[0]['word_count'], 0)

        result = self.plugin.get_content('book.epub', 0)
        self.assertEqual(result['format'], 'html')
        html = result['content']
        self.assertIn('<h1>第一章</h1>', html)
        # &nbsp; 是 XML 里未定义的实体：能被还原说明走的是容错解析，而不是 ElementTree
        self.assertIn('\xa0', html)
        self.assertNotIn('<script', html)
        self.assertNotIn('onerror', html)
        self.assertNotIn('javascript:', html)

        # 图片按需解包到数据根之内，src 用绝对路径指向 Shell 文件路由
        self.assertIn('<img src="/file?path=', html)
        rel = unquote(html.split('<img src="/file?path=')[1].split('&amp;plugin=')[0])
        extracted = Path(rel)
        self.assertTrue(extracted.is_file(), f'解包图片不存在: {extracted}')
        self.assertTrue(extracted.resolve().is_relative_to(self.docs.resolve()))
        self.assertEqual(extracted.read_bytes(), b'\x89PNG\r\n\x1a\n')

    def test_epub_escaped_member_paths_are_not_extracted(self):
        _write_epub(
            self.docs / 'evil.epub',
            docs=[('ch1.xhtml', '第一章',
                   '<img src="../../secret.png"/><img src="/abs.png"/>'
                   '<img src="https://example.com/remote.png"/>')],
            nav=[],
            extra={'secret.png': b'outside', 'OEBPS/abs.png': b'abs'},
        )
        self.plugin.list_novels()

        # nav 为空 → 标题回落到正文里的第一个标题
        self.assertEqual([c['title'] for c in self.plugin.get_chapters('evil.epub')['chapters']],
                         ['第一章'])

        html = self.plugin.get_content('evil.epub', 0)['content']
        self.assertNotIn('<img', html)
        self.assertFalse((self.root / 'secret.png').exists())
        self.assertFalse((self.docs / 'secret.png').exists())
        extract_root = self.docs / '.novel_state' / 'extract'
        written = [p for p in extract_root.rglob('*') if p.is_file()] if extract_root.exists() else []
        self.assertEqual(written, [])

    # ===== 进度键迁移 =====

    def test_legacy_progress_keyed_by_stem_still_reads(self):
        (self.docs / '旧书.txt').write_text('第一章\n正文\n', encoding='utf-8')
        state = self.docs / '.novel_state'
        state.mkdir(parents=True, exist_ok=True)
        (state / '.novel_progress.json').write_text(
            json.dumps({'旧书': {'last_read_chapter': 3, 'progress': 0.5, 'encoding': 'gbk'}},
                       ensure_ascii=False),
            encoding='utf-8',
        )
        self.plugin._progress_cache = None      # 构造时已读过一次（那时文件还不存在）

        item = next(n for n in self.plugin.list_novels()['novels'] if n['id'] == '旧书.txt')
        self.assertEqual(item['last_read_chapter'], 3)
        self.assertEqual(item['encoding'], 'gbk')

        # 新写入用完整文件名：旧进度被读到后自然迁移到新键
        self.plugin._chapter_cache['旧书.txt:txt:gbk'] = [{'index': i} for i in range(4)]
        self.plugin.update_progress('旧书.txt', 1, 0.0, 'gbk')
        saved = json.loads((state / '.novel_progress.json').read_text(encoding='utf-8'))
        self.assertIn('旧书.txt', saved)

    # ===== 外部打开 =====

    def test_open_external_rejects_path_outside_root(self):
        inside = self.docs / 'a.docx'
        inside.write_bytes(b'PK\x03\x04')
        outside = self.root / 'outside.docx'
        outside.write_bytes(b'PK\x03\x04')
        second = self.root / '第二根'
        second.mkdir()
        far_inside = second / 'b.docx'
        far_inside.write_bytes(b'PK\x03\x04')
        self.plugin = self._new_plugin([str(self.docs), str(second)])
        self.plugin.list_novels()

        opened = []
        docs_mod = type(self.plugin).open_external.__globals__['_docs_mod']
        original = docs_mod.open_with_system
        self.addCleanup(setattr, docs_mod, 'open_with_system', original)
        docs_mod.open_with_system = lambda path: opened.append(Path(path))

        self.assertEqual(self.plugin.open_external('a.docx'), {'success': True})
        # 第二个根目录里的文件同样允许（越界判定要逐根比对，不能只看主目录）
        self.assertEqual(self.plugin.open_external('b.docx'), {'success': True})
        self.assertEqual(opened, [inside.resolve(), far_inside.resolve()])

        # 越界：把缓存里的路径改成配置目录之外，必须拒绝且不调用外部程序
        self.plugin._novel_cache['a.docx']['file_path'] = str(outside)
        self.assertIn('越界', self.plugin.open_external('a.docx').get('error', ''))
        self.assertEqual(len(opened), 2)

        self.assertEqual(self.plugin.open_external('missing.docx'), {'error': '文档不存在'})

    # ===== 多目录设置 =====

    def test_settings_declare_multi_directory_and_apply_immediately(self):
        """多目录靠 shell 的 `directory` + `multi` 字段：保存后要立刻按新根重建列表。"""
        from shell.backend.settings_store import SettingsStore

        schema = {item['key']: item for item in self.plugin.settings_schema}
        self.assertEqual(schema['root_dir']['type'], 'directory')
        self.assertTrue(schema['root_dir'].get('multi'), '目录字段必须声明 multi 才能存多行')

        second = self.root / '第二根'
        second.mkdir()
        (second / 'x.md').write_text('# 一\n正文\n', encoding='utf-8')
        (self.docs / 'y.txt').write_text('第一章\n正文\n', encoding='utf-8')

        self.plugin._settings_store = SettingsStore(str(self.root / 'config' / 'plugins'))
        self.assertEqual(self.plugin.save_settings({'root_dir': f'{self.docs}\n{second}'}),
                         {'success': True})

        ids = {n['id'] for n in self.plugin.list_novels()['novels']}
        self.assertEqual(ids, {'y.txt', 'x.md'})
        self.assertEqual([str(path) for path in self.plugin.get_file_roots()],
                         [str(self.docs.resolve()), str(second.resolve())])

    # ===== 章节数缓存 =====

    def test_chapter_count_survives_rescan(self):
        """列表里那一行不该永远是 "?"：解析过一次的章节数要活过重新扫描。"""
        (self.docs / 'a.txt').write_text('第一章\n正文\n第二章\n正文\n', encoding='utf-8')
        self.plugin.list_novels()
        chapters = self.plugin.get_chapters('a.txt')['chapters']
        self.plugin._save_cache()          # get_chapters 内部已保存，这里只是明确语义

        # 新增一个文件迫使重新扫描
        (self.docs / 'b.txt').write_text('第一章\n正文\n', encoding='utf-8')
        items = {n['id']: n for n in self.plugin.list_novels()['novels']}
        self.assertEqual(items['a.txt']['chapter_count'], len(chapters))
        self.assertEqual(items['b.txt']['chapter_count'], 0)   # 还没解析过 → 前端显示体积


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
