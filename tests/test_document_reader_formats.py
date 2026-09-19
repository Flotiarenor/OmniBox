"""document-reader 多格式读取契约（Markdown / EPUB / pdf / 外部打开）。

守三件事：

1. 富文本 HTML 由后端白名单转换器产出：EPUB/Markdown 里的 `<script>`、`on*` 属性、
   `javascript:` 链接不能进到与壳同源的插件 iframe（那里有 Bridge）；
2. zip 成员名不可信：越界成员既不落盘，也不出现在 HTML 里；
3. 进度键从"去扩展名的文件名"改成完整文件名后，旧进度仍要读得出来。

运行：
    venv\\Scripts\\python.exe -m unittest tests.test_document_reader_formats -v
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

PLUGIN_DIR = PROJECT_ROOT / 'plugins' / 'document-reader'
OPF_PATH = 'OEBPS/content.opf'
# 一个最小 PNG 头（测试只关心"字节原样搬运"，不解码）
_PNG_BYTES = b'\x89PNG\r\n\x1a\n'


def _chapter_xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{title}</title></head>'
        f'<body><h1>{title}</h1>{body}</body></html>'
    )


def _write_epub(path: Path, docs, nav=None, extra=None, cover=None) -> None:
    """写一个最小合法 EPUB：mimetype + container.xml + OPF + nav.xhtml + 正文。

    `cover` 指定封面的声明方式（三种真实存在的写法各测一遍）：
      `('properties', 'cover.png')` —— EPUB3：manifest 上的 `properties="cover-image"`；
      `('meta', 'cover.png')`       —— EPUB2：`<meta name="cover" content="封面 item 的 id">`；
      `('guide', 'cover.png')`      —— 更老的：`<guide><reference type="cover" href="...">`。
    """
    items, spine = [], []
    for index, (href, _title, _body) in enumerate(docs):
        items.append(f'<item id="doc{index}" href="{href}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="doc{index}"/>')
    nav_item, links = '', ''
    if nav is not None:
        nav_item = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        links = ''.join(f'<li><a href="{href}">{title}</a></li>' for href, title in nav)
    cover_item, cover_meta, guide = '', '', ''
    if cover:
        kind, name = cover
        properties = ' properties="cover-image"' if kind == 'properties' else ''
        cover_item = f'<item id="coverimg" href="{name}" media-type="image/png"{properties}/>'
        if kind == 'meta':
            cover_meta = '<meta name="cover" content="coverimg"/>'
        elif kind == 'guide':
            guide = f'<guide><reference type="cover" href="{name}"/></guide>'
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="bookid">urn:uuid:test</dc:identifier>'
        f'<dc:title>测试书</dc:title><dc:language>zh</dc:language>{cover_meta}</metadata>'
        f'<manifest>{nav_item}{cover_item}{"".join(items)}</manifest>'
        f'<spine>{"".join(spine)}</spine>{guide}</package>'
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


class DocumentReaderFormatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.docs = self.root / 'docs'
        self.docs.mkdir()
        self.plugin = self._new_plugin()

    def _new_plugin(self, root_dirs=None):
        from tools.check_plugins import _load_backend_class

        cls = _load_backend_class(PLUGIN_DIR, 'backend/main.py', 'DocumentReaderPlugin')
        # 直接喂"已解决设置"，省掉 SettingsStore 与一次 root_dir 切换
        raw = root_dirs if root_dirs is not None else str(self.docs)
        cls._resolved_config = {'root_dir': '\n'.join(raw) if isinstance(raw, list) else raw}
        return cls({'name': 'document-reader'}, {'directories': {'data_root': str(self.root / 'data')}})

    # ===== 列表与格式分派 =====

    def test_lists_supported_formats_and_skips_unknown(self):
        (self.docs / 'a.txt').write_text('第一章\n正文\n', encoding='utf-8')
        (self.docs / 'b.md').write_text('# 标题\n正文\n', encoding='utf-8')
        (self.docs / '2024年度-报告.docx').write_bytes(b'PK\x03\x04')
        (self.docs / 'note.log').write_text('x', encoding='utf-8')

        items = {n['id']: n for n in self.plugin.list_documents()['documents']}
        self.assertEqual(set(items), {'a.txt', 'b.md', '2024年度-报告.docx'})
        self.assertEqual(items['b.md']['kind'], 'md')
        self.assertEqual(items['2024年度-报告.docx']['kind'], 'external')
        # 非 txt 不按 '-' 拆作者：'2024年度-报告' 是标题本身
        self.assertEqual(items['2024年度-报告.docx']['title'], '2024年度-报告')
        self.assertEqual(items['a.txt']['title'], 'a')
        # 主目录下的文件没有子目录可显示
        self.assertEqual(items['a.txt']['dir'], '')

    def test_scans_subdirectories_and_multiple_roots(self):
        nested = self.docs / '文档' / '武侠'
        nested.mkdir(parents=True)
        (self.docs / '顶层.txt').write_text('第一章\n正文\n', encoding='utf-8')
        (nested / '剑来.epub').write_bytes(b'PK\x03\x04')
        # 隐藏目录（自己的 .document_state 就走这条路）不进列表
        hidden = self.docs / '.hidden'
        hidden.mkdir()
        (hidden / '藏起来.txt').write_text('第一章\n', encoding='utf-8')

        second = self.root / '第二根'
        second.mkdir()
        (second / '另一本.md').write_text('# 一\n正文\n', encoding='utf-8')
        self.plugin = self._new_plugin([str(self.docs), str(second)])

        items = {n['id']: n for n in self.plugin.list_documents()['documents']}
        self.assertEqual(set(items), {'顶层.txt', '文档/武侠/剑来.epub', '另一本.md'})
        # id 是相对各自根目录的路径，主目录第一层仍是文件名（老进度键不受影响）
        self.assertEqual(items['文档/武侠/剑来.epub']['dir'], '文档/武侠')
        self.assertEqual(items['文档/武侠/剑来.epub']['title'], '剑来')
        self.assertEqual(items['另一本.md']['dir'], '')
        # 两个根目录都要能被文件路由访问（EPUB 图片按绝对路径引用）；
        # 朗读缓存目录（<config>/plugins/<插件名>）也必须在内 —— 音频经 /file 播放。
        roots = [str(path) for path in self.plugin.get_file_roots()]
        self.assertEqual(roots[:2], [str(self.docs.resolve()), str(second.resolve())])
        self.assertIn(str(self.plugin._tts_cache_dir().parent), roots)
        # 主目录是第一根：缓存与阅读进度存在它下面
        self.assertEqual(str(self.plugin.get_data_root()), str(self.docs.resolve()))

    def test_pdf_and_external_have_no_chapters(self):
        (self.docs / 'c.pdf').write_bytes(b'%PDF-1.4\n')
        (self.docs / 'd.docx').write_bytes(b'PK\x03\x04')

        kinds = {n['id']: n['kind'] for n in self.plugin.list_documents()['documents']}
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
        self.plugin.list_documents()

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
        self.plugin.list_documents()

        html = self.plugin.get_content('notes.md', 0)['content']
        # 图片 URL 用绝对路径：多根目录下相对路径只认第一根
        self.assertIn(f'src="/file?path={quote(str((self.docs / "pic.png").resolve()))}&amp;plugin=document-reader"',
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
        self.plugin.list_documents()

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
        self.plugin.list_documents()

        # nav 为空 → 标题回落到正文里的第一个标题
        self.assertEqual([c['title'] for c in self.plugin.get_chapters('evil.epub')['chapters']],
                         ['第一章'])

        html = self.plugin.get_content('evil.epub', 0)['content']
        self.assertNotIn('<img', html)
        self.assertFalse((self.root / 'secret.png').exists())
        self.assertFalse((self.docs / 'secret.png').exists())
        extract_root = self.docs / '.document_state' / 'extract'
        written = [p for p in extract_root.rglob('*') if p.is_file()] if extract_root.exists() else []
        self.assertEqual(written, [])

    # ===== 封面 =====

    def test_cover_from_epub3_properties(self):
        """EPUB3：manifest 上的 properties="cover-image"。封面不在 spine 里，必须单独取。"""
        self._write_cover_epub('epub3.epub', ('properties', 'cover.png'))
        cover = self.plugin.document_cover('epub3.epub')['cover']
        self.assertTrue(cover, 'EPUB3 的 cover-image 没被取到')
        self.assertIn('/file?path=', cover)
        self.assertIn('plugin=document-reader', cover)
        # 封面文件真的落盘了，而且落在主目录的 .document_state 下
        written = list((self.docs / '.document_state' / 'extract').rglob('cover.png'))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0].read_bytes(), _PNG_BYTES)

    def test_cover_from_epub2_meta(self):
        self._write_cover_epub('epub2.epub', ('meta', 'cover.png'))
        self.assertTrue(self.plugin.document_cover('epub2.epub')['cover'])

    def test_cover_from_guide_reference(self):
        self._write_cover_epub('old.epub', ('guide', 'cover.png'))
        self.assertTrue(self.plugin.document_cover('old.epub')['cover'])

    def test_cover_missing_file_is_not_an_error(self):
        """声明了封面但 zip 里没有那个成员：返回空串（前端画生成式封面），不是异常。"""
        _write_epub(self.docs / 'broken.epub',
                    docs=[('ch1.xhtml', '第一章', '<p>正文</p>')],
                    cover=('properties', 'not-there.png'))
        self.plugin.list_documents()
        self.assertEqual(self.plugin.document_cover('broken.epub'), {'cover': ''})

    def test_cover_for_non_epub_and_unknown_doc(self):
        (self.docs / 'a.txt').write_text('第一章\n正文\n', encoding='utf-8')
        self.plugin.list_documents()
        # txt 没有封面概念；不存在的 id 也不该炸
        self.assertEqual(self.plugin.document_cover('a.txt'), {'cover': ''})
        self.assertEqual(self.plugin.document_cover('没有这本书.epub'), {'cover': ''})

    def _write_cover_epub(self, name: str, cover) -> None:
        _write_epub(
            self.docs / name,
            docs=[('ch1.xhtml', '第一章', '<p>正文</p>')],
            nav=[('ch1.xhtml', '第一章')],
            cover=cover,
            extra={'OEBPS/cover.png': _PNG_BYTES},
        )
        self.plugin.list_documents()

    # ===== 书签 =====

    def test_marks_add_list_remove(self):
        (self.docs / 'a.txt').write_text('第一章\n正文\n', encoding='utf-8')
        self.plugin.list_documents()

        added = self.plugin.marks_add('a.txt', 1, 128, '他忽然想起很多年前', '第三章')
        self.assertTrue(added['success'])
        self.assertEqual(len(added['marks']), 1)
        self.assertEqual(added['marks'][0]['char'], 128)

        listed = self.plugin.marks_list('a.txt')['marks']
        self.assertEqual(listed[0]['snippet'], '他忽然想起很多年前')
        # 不带 id 时列出全部，并补上 document_id（书架左侧「书签」要用）
        self.assertEqual(self.plugin.marks_list()['marks'][0]['document_id'], 'a.txt')

        removed = self.plugin.marks_remove('a.txt', 1, 128)
        self.assertEqual(removed['marks'], [])
        self.assertEqual(self.plugin.marks_list('a.txt')['marks'], [])

    def test_marks_same_position_is_idempotent(self):
        (self.docs / 'a.txt').write_text('第一章\n正文\n', encoding='utf-8')
        self.plugin.list_documents()
        self.plugin.marks_add('a.txt', 0, 10, '第一段')
        self.plugin.marks_add('a.txt', 0, 10, '第一段（重复点了一次）')
        marks = self.plugin.marks_list('a.txt')['marks']
        self.assertEqual(len(marks), 1)
        self.assertIn('重复', marks[0]['snippet'])

    def test_marks_persist_across_plugin_reload(self):
        (self.docs / 'a.txt').write_text('第一章\n正文\n', encoding='utf-8')
        self.plugin.list_documents()
        self.plugin.marks_add('a.txt', 2, 33, '摘录')

        fresh = self._new_plugin()
        fresh.list_documents()
        self.assertEqual(fresh.marks_list('a.txt')['marks'][0]['char'], 33)

    def test_marks_reject_unknown_document_and_bad_position(self):
        self.assertEqual(self.plugin.marks_add('没有这本书.txt', 0, 0)['success'], False)
        (self.docs / 'a.txt').write_text('第一章\n正文\n', encoding='utf-8')
        self.plugin.list_documents()
        # 位置参数不是数字时给错误，而不是 500
        self.assertFalse(self.plugin.marks_add('a.txt', 'x', 'y')['success'])
        # 负数与非法章节被夹回合法范围，不产生负偏移
        added = self.plugin.marks_add('a.txt', -5, -20, 'x')
        self.assertEqual(added['marks'][0]['chapter'], 0)
        self.assertEqual(added['marks'][0]['char'], 0)

    def test_marks_stay_per_document_when_ids_collide_across_roots(self):
        """两个根目录下同名文件：id 会被去重成 `a.txt` / `a.txt#2`，书签必须各归各。"""
        other = self.root / 'other'
        other.mkdir()
        (self.docs / 'a.txt').write_text('第一章\n正文\n', encoding='utf-8')
        (other / 'a.txt').write_text('第一章\n另一本正文\n', encoding='utf-8')
        plugin = self._new_plugin([str(self.docs), str(other)])
        ids = sorted(item['id'] for item in plugin.list_documents()['documents'])
        self.assertEqual(ids, ['a.txt', 'a.txt#2'])

        plugin.marks_add('a.txt', 0, 5, '第一本')
        plugin.marks_add('a.txt#2', 0, 9, '第二本')
        self.assertEqual(plugin.marks_list('a.txt')['marks'][0]['snippet'], '第一本')
        self.assertEqual(plugin.marks_list('a.txt#2')['marks'][0]['char'], 9)

    # ===== 进度键迁移 =====

    def test_legacy_progress_keyed_by_stem_still_reads(self):
        (self.docs / '旧书.txt').write_text('第一章\n正文\n', encoding='utf-8')
        state = self.docs / '.document_state'
        state.mkdir(parents=True, exist_ok=True)
        (state / '.document_progress.json').write_text(
            json.dumps({'旧书': {'last_read_chapter': 3, 'progress': 0.5, 'encoding': 'gbk'}},
                       ensure_ascii=False),
            encoding='utf-8',
        )
        self.plugin._progress_cache = None      # 构造时已读过一次（那时文件还不存在）

        item = next(n for n in self.plugin.list_documents()['documents'] if n['id'] == '旧书.txt')
        self.assertEqual(item['last_read_chapter'], 3)
        self.assertEqual(item['encoding'], 'gbk')

        # 新写入用完整文件名：旧进度被读到后自然迁移到新键
        self.plugin._chapter_cache['旧书.txt:txt:gbk'] = [{'index': i} for i in range(4)]
        self.plugin.update_progress('旧书.txt', 1, 0.0, 'gbk')
        saved = json.loads((state / '.document_progress.json').read_text(encoding='utf-8'))
        self.assertIn('旧书.txt', saved)

    # ===== 编码兜底 =====

    def test_truncated_gbk_txt_still_reads(self):
        """末尾被截断半个字的 GBK txt 必须还能读出来。

        旧实现严格解码失败后统一退回 utf-8 + errors='ignore'：对 GBK 中文就是满屏乱码，
        而且不抛异常 —— 表现就是"auto、gbk、gb2312 换哪个编码都读不出来"。
        """
        body = '第一章 起点\r\n正文一\r\n第二章 终点\r\n正文二\r\n'
        (self.docs / '残卷.txt').write_bytes(body.encode('gbk') + '尾'.encode('gbk')[:1])
        self.plugin.list_documents()

        for encoding in ('auto', 'gbk', 'gb2312'):
            chapters = self.plugin.get_chapters('残卷.txt', encoding)['chapters']
            self.assertEqual([c['title'] for c in chapters], ['第一章 起点', '第二章 终点'],
                             f'{encoding}: 截断结尾的 GBK 文件没解对')
            content = self.plugin.get_content('残卷.txt', 0, encoding)['content']
            self.assertIn('正文一', content, f'{encoding}: 正文是乱码')

    def test_truncated_utf8_txt_still_reads(self):
        """UTF-8 的文件末尾同样可能被截断：不能因为 gbk / gb18030 也"解得开"就换成乱码。"""
        (self.docs / 'utf8残卷.txt').write_bytes(
            '第一章 起点\n正文一\n'.encode('utf-8') + '尾'.encode('utf-8')[:1])
        self.plugin.list_documents()

        for encoding in ('auto', 'utf-8'):
            chapters = self.plugin.get_chapters('utf8残卷.txt', encoding)['chapters']
            self.assertEqual([c['title'] for c in chapters], ['第一章 起点'],
                             f'{encoding}: 截断结尾的 UTF-8 文件没解对')
            content = self.plugin.get_content('utf8残卷.txt', 0, encoding)['content']
            self.assertIn('正文一', content, f'{encoding}: 正文是乱码')

    def test_gbk_only_char_reads_under_gb2312_selection(self):
        """选了 gb2312 而正文里有 GBK 扩展字（"玥"不在 GB2312 里）时也要读得出来：
        gb2312 / gbk 都是 gb18030 的子集，按 gb18030 解不会有损失。"""
        (self.docs / '扩展字.txt').write_bytes('第一章 起点\n玥儿登场\n'.encode('gbk'))
        self.plugin.list_documents()

        result = self.plugin.get_content('扩展字.txt', 0, 'gb2312')
        self.assertIn('玥儿登场', result['content'])
        self.assertEqual([c['title'] for c in self.plugin.get_chapters('扩展字.txt', 'gb2312')['chapters']],
                         ['第一章 起点'])

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
        self.plugin.list_documents()

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
        self.plugin._document_cache['a.docx']['file_path'] = str(outside)
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

        ids = {n['id'] for n in self.plugin.list_documents()['documents']}
        self.assertEqual(ids, {'y.txt', 'x.md'})
        roots = [str(path) for path in self.plugin.get_file_roots()]
        self.assertEqual(roots[:2], [str(self.docs.resolve()), str(second.resolve())],
                         '配置的两个根要按顺序生效')

    # ===== 改名迁移 =====

    def test_legacy_settings_file_is_migrated_on_load(self):
        """插件改名后新名下没有 root_dir 时，从旧名（novel-reader）的设置文件迁移过来。"""
        from shell.backend.settings_store import SettingsStore
        from tools.check_plugins import _load_backend_class

        legacy_root = self.root / '旧配置目录'
        legacy_root.mkdir()
        (legacy_root / 'x.md').write_text('# 一\n正文\n', encoding='utf-8')
        store = SettingsStore(str(self.root / 'config' / 'plugins'))
        store.set('novel-reader', {'root_dir': str(legacy_root)})

        # 改名后的真实状态：新名下既没有设置文件，壳预解析的 _resolved_config 也是空的
        cls = _load_backend_class(PLUGIN_DIR, 'backend/main.py', 'DocumentReaderPlugin')
        cls._resolved_config = {}
        plugin = cls({'name': 'document-reader'},
                     {'directories': {'data_root': str(self.root / 'data')}})
        plugin._settings_store = store
        self.assertEqual(str(plugin.get_data_root()), str((self.root / 'data').resolve()))

        plugin.on_load()
        self.assertEqual(plugin.setting('root_dir'), str(legacy_root))
        self.assertEqual([str(path) for path in plugin.get_file_roots()][:1],
                         [str(legacy_root.resolve())],
                         '迁移后的第一根应当是旧配置里的目录')
        self.assertEqual({n['id'] for n in plugin.list_documents()['documents']}, {'x.md'})
        # 迁移结果写进新名下：下次启动不必再迁一次
        self.assertEqual(store.get('document-reader')['root_dir'], str(legacy_root))

    def test_legacy_state_directory_is_migrated(self):
        """旧名（.novel_state）下的状态要搬到新名下：改名不该让阅读进度归零。"""
        # 情况一：新目录还不存在 → 整个目录搬过去（真实升级路径）
        fresh = self.root / '另一个库'
        (fresh / '.novel_state').mkdir(parents=True)
        (fresh / '.novel_state' / '.novel_progress.json').write_text(
            json.dumps({'a.txt': {'last_read_chapter': 2}}, ensure_ascii=False), encoding='utf-8')
        (fresh / 'a.txt').write_text('第一章\n正文\n第二章\n正文\n', encoding='utf-8')

        plugin = self._new_plugin([str(fresh)])
        self.assertFalse((fresh / '.novel_state').exists())
        state = fresh / '.document_state'
        self.assertTrue((state / '.document_progress.json').is_file())
        item = next(n for n in plugin.list_documents()['documents'] if n['id'] == 'a.txt')
        self.assertEqual(item['last_read_chapter'], 2)

        # 情况二：新目录已经建出来（插件先加载过一次）→ 逐个文件搬
        second = self.root / '第三个库'
        (second / '.document_state').mkdir(parents=True)
        (second / '.novel_state').mkdir()
        (second / '.novel_state' / '.novel_cache.json').write_text(
            json.dumps({'parser_version': 3, 'novels': {'a.txt': {'id': 'a.txt'}}},
                       ensure_ascii=False), encoding='utf-8')
        plugin2 = self._new_plugin([str(second)])
        migrated = second / '.document_state' / '.document_cache.json'
        self.assertTrue(migrated.is_file())
        # 旧缓存文件里这个键叫 novels：读得出来，不必全量重解析
        self.assertIn('a.txt', plugin2._document_cache)

    # ===== 章节数缓存 =====

    def test_chapter_count_survives_rescan(self):
        """列表里那一行不该永远是 "?"：解析过一次的章节数要活过重新扫描。"""
        (self.docs / 'a.txt').write_text('第一章\n正文\n第二章\n正文\n', encoding='utf-8')
        self.plugin.list_documents()
        chapters = self.plugin.get_chapters('a.txt')['chapters']
        self.plugin._save_cache()          # get_chapters 内部已保存，这里只是明确语义

        # 新增一个文件迫使重新扫描
        (self.docs / 'b.txt').write_text('第一章\n正文\n', encoding='utf-8')
        items = {n['id']: n for n in self.plugin.list_documents()['documents']}
        self.assertEqual(items['a.txt']['chapter_count'], len(chapters))
        self.assertEqual(items['b.txt']['chapter_count'], 0)   # 还没解析过 → 前端显示体积


if __name__ == '__main__':   # pragma: no cover
    unittest.main()
