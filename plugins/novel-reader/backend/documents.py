"""把 Markdown / EPUB 读成阅读器能渲染的 HTML 片段。

为什么自己写容器解析而不是引库：
  - EPUB 就是 zip + XML 索引（META-INF/container.xml → OPF → spine），zipfile 与
    xml.etree 足够；EbookLib 会连带 lxml（编译型依赖），为这点事不值得。
  - Markdown 用已装的 markdown-it-py（requirements.txt 已声明，只多一行 HIDDEN_IMPORTS）。

安全边界（两条都不可省）：

1. **所有 HTML 都由本模块的白名单转换器产出**。EPUB 的 XHTML 与 Markdown 里的
   `<script>`、`on*` 属性、`javascript:` 链接一旦进了插件 iframe，就拿得到与壳
   同源的 Bridge（可读任意文件、可调任意插件 API），所以这些内容在转换阶段就被
   丢掉，而不是"先注入再 innerHTML，再想办法消毒"。
2. **zip 成员名不可信**：成员名不参与落盘路径（解包出来的图片一律用成员名哈希命名），
   只接受数据根之内的相对路径，并且限制单个成员与单本书的解包体积。
"""

import hashlib
import html
import os
import posixpath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote

from shell.backend.plugin_utils import load_sibling

try:
    import chardet
except ImportError:  # pragma: no cover - 缺包时按 UTF-8/gb18030 兜底
    chardet = None

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover - requirements.txt 已声明，缺了只有 md 不可用
    MarkdownIt = None

_parser_mod = load_sibling(__file__, 'parser', 'novel_reader')
NovelParser = _parser_mod.NovelParser

# 单个 zip 成员（图片）与单本书解包总量的上限：zip 炸弹与超大图不能把磁盘写满。
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_EXTRACT_BYTES = 512 * 1024 * 1024

_MD = None
if MarkdownIt is not None:
    # commonmark 预设 + 表格/删除线（这两个规则是 markdown-it 自带的，不引新包；
    # gfm-like 预设会打开 linkify，那需要额外的 linkify-it-py，不引）。
    _MD = MarkdownIt('commonmark', {'html': False, 'breaks': False}).enable(['table', 'strikethrough'])

_HEADING_RE = re.compile(r'^(#{1,3})[ \t]+(.+?)[ \t]*#*[ \t]*$')
_EXT_RE = re.compile(r'^\.[a-z0-9]{1,5}$')
_SPACE_RE = re.compile(r'\s+')


class DocError(Exception):
    """文档结构无法解析（不是有效的 EPUB、markdown-it 不可用等）。"""


# ============================================================
# 文本解码
# ============================================================

def _decode(raw: bytes, encoding: str = 'auto') -> str:
    """zip 成员没有独立文件路径，编码只能就地判：显式编码 → BOM → chardet → 兜底。"""
    if encoding and encoding != 'auto':
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try:
            return raw.decode('utf-16')
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        pass
    if chardet is not None:
        guess = chardet.detect(raw[:65536]).get('encoding')
        if guess:
            try:
                return raw.decode(guess)
            except (UnicodeDecodeError, LookupError):
                pass
    return raw.decode('gb18030', errors='replace')


# ============================================================
# HTML 白名单转换器
# ============================================================

_ALLOWED_TAGS = {
    'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'em', 'strong', 'i', 'b', 'u',
    's', 'strike', 'del', 'ins', 'mark', 'small', 'sub', 'sup', 'span', 'br', 'hr',
    'blockquote', 'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'table', 'thead', 'tbody',
    'tfoot', 'tr', 'td', 'th', 'caption', 'img', 'a', 'pre', 'code', 'figure',
    'figcaption',
}
# 内容一并丢弃的标签（需要配对结束标签）
_SKIP_TAGS = {
    'script', 'style', 'head', 'title', 'iframe', 'object', 'embed', 'noscript',
    'template', 'canvas', 'audio', 'video',
}
# HTML 的空元素：出现结束标签时不得弹栈
_VOID_TAGS = {
    'br', 'hr', 'img', 'link', 'meta', 'base', 'input', 'col', 'source', 'track',
    'wbr', 'area', 'param',
}
# 只保留这些属性，其余（class/style/id/on*/data-*）一律丢弃
_ATTRS = {
    'a': ('href',),
    'img': ('src', 'alt'),
    'td': ('colspan', 'rowspan'),
    'th': ('colspan', 'rowspan'),
    'ol': ('start',),
}
# 这些元素在文档里经常不闭合：遇到同名开始标签先收口，避免越嵌越深
_AUTO_CLOSE = {'p', 'li', 'td', 'th', 'tr', 'dt', 'dd'}


def _safe_href(raw: str) -> Optional[str]:
    """链接白名单：只放行 http(s)/mailto/相对路径，挡掉 javascript: 等可执行 scheme。"""
    value = (raw or '').strip()
    if not value:
        return None
    # 去掉空白与控制字符后再判 scheme：`java\tscript:` 这类混淆必须在规范化后比对
    flat = ''.join(ch for ch in value if ord(ch) > 32).lower()
    if flat.startswith(('javascript:', 'vbscript:', 'data:', 'file:', 'blob:')):
        return None
    return value


class _HtmlCleaner(HTMLParser):
    """把任意 HTML 片段收敛成白名单子集。

    `image_src` 回调负责把图片地址换成本地可加载的 URL（返回 None 表示丢弃这张图）。
    """

    def __init__(self, image_src: Optional[Callable[[str], Optional[str]]] = None) -> None:
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self._image_src = image_src
        self._open: List[str] = []
        self._skip = 0

    # ---- HTMLParser 回调 ----

    def handle_starttag(self, tag: str, attrs) -> None:
        self._start(tag, dict(attrs))

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._start(tag, dict(attrs))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip or tag in _VOID_TAGS or tag not in _ALLOWED_TAGS:
            return
        if tag not in self._open:
            return
        while self._open:
            name = self._open.pop()
            self.out.append(f'</{name}>')
            if name == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        if data.strip() or (self._open and self._open[-1] == 'pre'):
            self.out.append(html.escape(data, quote=False))

    # ---- 内部 ----

    def _start(self, tag: str, attrs: Dict[str, str]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        # EPUB 封面常写成 <svg><image xlink:href="..."/></svg>：当 img 处理，别整张丢掉
        if tag == 'image':
            self._emit_img(attrs.get('xlink:href') or attrs.get('href') or '', attrs.get('alt'))
            return
        if tag not in _ALLOWED_TAGS:
            return
        if tag == 'img':
            self._emit_img(attrs.get('src') or '', attrs.get('alt'))
            return
        if tag in _AUTO_CLOSE and tag in self._open:
            self.handle_endtag(tag)

        parts = [tag]
        for name in _ATTRS.get(tag, ()):
            value = attrs.get(name)
            if value is None:
                continue
            if name == 'href':
                value = _safe_href(value)
                if not value:
                    continue
            parts.append(f'{name}="{html.escape(value, quote=True)}"')
        self.out.append('<' + ' '.join(parts) + '>')
        self._open.append(tag)

    def _emit_img(self, src: str, alt: Optional[str]) -> None:
        url = self._image_src(src) if self._image_src else None
        if not url:
            return
        chunk = f'<img src="{html.escape(url, quote=True)}"'
        if alt:
            chunk += f' alt="{html.escape(alt, quote=True)}"'
        self.out.append(chunk + '>')


class _TextProbe(HTMLParser):
    """只取章节正文的纯文本与第一个标题，用于目录标题兜底与字数统计。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.heading = ''
        self.text: List[str] = []
        self._skip = 0
        self._in_heading = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._in_heading += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip:
                self._skip -= 1
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._in_heading = max(0, self._in_heading - 1)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.text.append(data)
        if self._in_heading and not self.heading:
            self.heading = data.strip()


def _clean(fragment: str, image_src: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """白名单转换的唯一入口（所有进入阅读器的富文本都从这里过）。"""
    cleaner = _HtmlCleaner(image_src=image_src)
    cleaner.feed(fragment)
    cleaner.close()
    return ''.join(cleaner.out)


# ============================================================
# Markdown
# ============================================================

class MarkdownDocument:
    """按 1~3 级标题切章，逐章渲染成 HTML。

    ponytail: 只认 ATX（`#`）标题。Setext 下划线式标题（标题 + `===`）不切章，
    整篇会变成一章 —— 真遇到这种文档再补一条正则。
    """

    kind = 'md'

    def __init__(self, path: str, encoding: str = 'auto', root_dir: Optional[str] = None,
                 image_url: Optional[Callable[[str], str]] = None) -> None:
        self._path = str(path)
        self._encoding = encoding
        self._image_url = image_url
        self._root_dir = Path(root_dir).resolve() if root_dir else None
        self._chapters: Optional[List[Tuple[Optional[str], str]]] = None

    def _load(self) -> List[Tuple[Optional[str], str]]:
        if self._chapters is not None:
            return self._chapters
        text = NovelParser.read_full_content(self._path, self._encoding)
        segments: List[Tuple[Optional[str], str]] = []
        title: Optional[str] = None
        buf: List[str] = []
        for line in text.splitlines():
            match = _HEADING_RE.match(line)
            if match:
                # 第一个标题之前的内容算"前言"；没有标题时末尾那次 flush 仍是 None，
                # 由 chapters() 回落到文件名（整篇没有标题的文档不该叫"前言"）
                _flush_markdown(segments, title if title is not None else '前言', buf)
                title, buf = match.group(2).strip(), [line]
                continue
            buf.append(line)
        _flush_markdown(segments, title, buf)
        if not segments:
            segments = [(None, text)]
        self._chapters = segments
        return segments

    def chapters(self) -> List[dict]:
        return [
            {
                'index': index,
                'title': title or self._fallback_title(),
                'word_count': len(_SPACE_RE.sub('', source)),
            }
            for index, (title, source) in enumerate(self._load())
        ]

    def chapter_html(self, index: int) -> str:
        segments = self._load()
        if not isinstance(index, int) or index < 0 or index >= len(segments):
            raise DocError('章节索引无效')
        if _MD is None:
            raise DocError('markdown-it-py 不可用，无法渲染 Markdown')
        return _clean(_MD.render(segments[index][1]), image_src=self._resolve_image)

    def _fallback_title(self) -> str:
        return posixpath.splitext(posixpath.basename(self._path.replace('\\', '/')))[0]

    def _resolve_image(self, src: str) -> Optional[str]:
        """把 md 里引用的同目录图片映射到 Shell 文件路由，其余（外链/data:）一律丢弃。"""
        value = (src or '').strip()
        if not value or '://' in value:
            return None
        flat = ''.join(ch for ch in value if ord(ch) > 32).lower()
        if flat.startswith('data:'):
            return None
        if self._image_url is None or self._root_dir is None:
            return None
        base = posixpath.dirname(self._path.replace('\\', '/'))
        target = Path(posixpath.normpath(posixpath.join(base, unquote(value))))
        try:
            target = target.resolve()
            # 相对引用不许跑出这个文档所在的根目录（../ 到磁盘别处就不给 URL）
            if not target.is_relative_to(self._root_dir) or not target.is_file():
                return None
        except (OSError, ValueError):
            return None
        return self._image_url(str(target))


def _flush_markdown(segments: List[Tuple[Optional[str], str]], title: Optional[str], buf: List[str]) -> None:
    source = '\n'.join(buf)
    if source.strip():
        segments.append((title, source))


# ============================================================
# EPUB
# ============================================================

def _local(tag: str) -> str:
    """去掉 XML 命名空间，只留本地标签名（小写）。"""
    return tag.rpartition('}')[2].lower()


def _is_safe_member(name: str) -> bool:
    """zip 成员名 / 相对路径是否可信：不接受绝对路径、`..` 段与盘符。"""
    return (bool(name) and not name.startswith('/')
            and '..' not in name.split('/') and ':' not in name)


def _zip_join(base_dir: str, href: str) -> str:
    """把（可能带 #fragment 或百分号编码的）href 解析成 zip 内的成员路径。"""
    value = unquote((href or '').strip()).replace('\\', '/').split('#')[0]
    if not value:
        return ''
    if value.startswith('/'):
        return posixpath.normpath(value.lstrip('/'))
    return posixpath.normpath(posixpath.join(base_dir, value) if base_dir else value)


def _read_member(zf: zipfile.ZipFile, name: str) -> bytes:
    if not _is_safe_member(name):
        raise DocError(f'EPUB 内部路径不合法: {name}')
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise DocError(f'EPUB 里没有这个文件: {name}') from None
    if info.is_dir():
        raise DocError(f'EPUB 内部路径是目录: {name}')
    if info.file_size > MAX_MEMBER_BYTES:
        raise DocError(f'EPUB 成员过大: {name}（{info.file_size} 字节）')
    return zf.read(name)


def _find_opf(zf: zipfile.ZipFile) -> str:
    root = ET.fromstring(_read_member(zf, 'META-INF/container.xml'))
    for node in root.iter():
        if _local(node.tag) == 'rootfile':
            full = node.get('full-path')
            if full:
                return full.replace('\\', '/')
    raise DocError('EPUB 的 container.xml 里没有 rootfile')


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> Tuple[Dict[str, dict], List[str]]:
    """返回 (manifest, spine 的 zip 成员路径列表)。"""
    root = ET.fromstring(_read_member(zf, opf_path))
    opf_dir = posixpath.dirname(opf_path)
    manifest: Dict[str, dict] = {}
    for node in root.iter():
        if _local(node.tag) != 'item':
            continue
        item_id, href = node.get('id'), node.get('href')
        if not item_id or not href:
            continue
        manifest[item_id] = {
            'href': _zip_join(opf_dir, href),
            'type': (node.get('media-type') or '').lower(),
            'properties': (node.get('properties') or '').split(),
        }

    spine: List[str] = []
    for node in root.iter():
        if _local(node.tag) != 'spine':
            continue
        for child in node:
            if _local(child.tag) != 'itemref':
                continue
            item = manifest.get(child.get('idref') or '')
            if item and item['href']:
                spine.append(item['href'])
    if not spine:
        # 没有可用 spine 的坏书：按 manifest 里的 XHTML 顺序兜底，总比打不开好
        spine = [item['href'] for item in manifest.values()
                 if 'html' in item['type'] and item['href']]
    return manifest, spine


class _AnchorCollector(HTMLParser):
    """收集 `<a href>文本</a>`，用于读 EPUB3 的 nav.xhtml 目录。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: List[Tuple[str, str]] = []
        self._href: Optional[str] = None
        self._buf: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == 'a':
            self._href = dict(attrs).get('href')
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == 'a' and self._href is not None:
            self.items.append((self._href, ''.join(self._buf).strip()))
            self._href = None


def _toc_titles(zf: zipfile.ZipFile, manifest: Dict[str, dict]) -> Dict[str, str]:
    """成员路径 → 标题。EPUB3 的 nav 优先，EPUB2 的 ncx 补空缺。"""
    titles: Dict[str, str] = {}

    nav_path = next((item['href'] for item in manifest.values() if 'nav' in item['properties']), '')
    if nav_path:
        try:
            base = posixpath.dirname(nav_path)
            collector = _AnchorCollector()
            collector.feed(_decode(_read_member(zf, nav_path)))
            collector.close()
            for href, title in collector.items:
                key = _zip_join(base, href)
                if key and title:
                    titles.setdefault(key, title)
        except (DocError, ET.ParseError, OSError):
            pass

    ncx_path = next((item['href'] for item in manifest.values()
                     if item['type'] == 'application/x-dtbncx+xml'), '')
    if ncx_path:
        try:
            base = posixpath.dirname(ncx_path)
            root = ET.fromstring(_read_member(zf, ncx_path))
            for point in root.iter():
                if _local(point.tag) != 'navpoint':
                    continue
                src, label = '', ''
                for child in point.iter():
                    name = _local(child.tag)
                    if name == 'content' and child.get('src') and not src:
                        src = child.get('src') or ''
                    elif name == 'text' and child.text and not label:
                        label = child.text.strip()
                key = _zip_join(base, src)
                if key:
                    titles.setdefault(key, label)
        except (DocError, ET.ParseError, OSError):
            pass

    return titles


class EpubDocument:
    """spine 即目录：一章一个 XHTML 文档，转换时抽图并按需落盘。

    ponytail: 只取正文与图片，原书的 CSS / 字体 / 分栏一律丢弃（排版交给阅读器
    自己的主题）。要还原原书版式就得引 Epub.js 那类整包渲染器，不值得 —— 现有
    主题、字号、行距、双模式全部会失效。
    """

    kind = 'epub'

    def __init__(self, path: str, extract_dir: Optional[str] = None,
                 image_url: Optional[Callable[[str], str]] = None) -> None:
        self._path = str(path)
        self._extract_dir = Path(extract_dir) if extract_dir else None
        self._image_url = image_url
        self._spine: List[Dict[str, str]] = []
        self._meta: Dict[int, Tuple[str, int]] = {}
        self._extracted_bytes = 0
        self._load()

    # ---- 解析 ----

    def _load(self) -> None:
        if not zipfile.is_zipfile(self._path):
            raise DocError('不是有效的 EPUB（zip）文件')
        try:
            with zipfile.ZipFile(self._path) as zf:
                opf_path = _find_opf(zf)
                manifest, spine = _parse_opf(zf, opf_path)
                titles = _toc_titles(zf, manifest)
        except (zipfile.BadZipFile, OSError, ET.ParseError) as e:
            raise DocError(f'EPUB 结构无法解析: {e}') from None
        if not spine:
            raise DocError('EPUB 的 spine 为空，没有可读的正文')
        self._spine = [{'href': href, 'title': titles.get(href) or ''} for href in spine]

    def chapters(self) -> List[dict]:
        out = []
        with zipfile.ZipFile(self._path) as zf:
            for index, item in enumerate(self._spine):
                if index not in self._meta:
                    self._meta[index] = self._describe(zf, index, item)
                title, words = self._meta[index]
                out.append({'index': index, 'title': title, 'word_count': words})
        return out

    def chapter_html(self, index: int) -> str:
        if not isinstance(index, int) or index < 0 or index >= len(self._spine):
            raise DocError('章节索引无效')
        href = self._spine[index]['href']
        with zipfile.ZipFile(self._path) as zf:
            raw = _read_member(zf, href)
            return _clean(_decode(raw), image_src=self._image_resolver(zf, href))

    # ---- 章节元信息 ----

    def _describe(self, zf: zipfile.ZipFile, index: int, item: Dict[str, str]) -> Tuple[str, int]:
        toc_title = item.get('title') or ''
        fallback = toc_title or f'第{index + 1}章'
        try:
            probe = _TextProbe()
            probe.feed(_decode(_read_member(zf, item.get('href') or '')))
            probe.close()
        except (DocError, OSError):
            return fallback, 0
        title = toc_title or probe.heading.strip() or fallback
        return title, len(_SPACE_RE.sub('', ''.join(probe.text)))

    # ---- 图片 ----

    def _image_resolver(self, zf: zipfile.ZipFile, chapter_href: str):
        base = posixpath.dirname(chapter_href)

        def resolve(src: str) -> Optional[str]:
            value = (src or '').strip()
            # 远链图片不取：阅读器没必要为了排版去访问外部站点
            if not value or '://' in value or value.lower().startswith('data:'):
                return None
            name = _zip_join(base, value)
            if not _is_safe_member(name):
                return None
            try:
                info = zf.getinfo(name)
            except KeyError:
                return None
            if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                return None
            if self._extracted_bytes + info.file_size > MAX_EXTRACT_BYTES:
                return None
            if self._extract_dir is None or self._image_url is None:
                return None
            ext = posixpath.splitext(name)[1].lower()
            if not _EXT_RE.match(ext):
                ext = '.img'
            target = self._extract_dir / (hashlib.md5(name.encode('utf-8')).hexdigest() + ext)
            try:
                if not target.is_file():
                    self._extract_dir.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(name))
                    self._extracted_bytes += info.file_size
            except OSError:
                return None
            # 交给文件路由的必须是绝对路径：多根目录下相对路径只认第一根，就会 404
            return self._image_url(str(target))

        return resolve


# ============================================================
# 对外接口
# ============================================================

def open_document(path: str, kind: str, encoding: str = 'auto', root_dir: Optional[str] = None,
                  extract_dir: Optional[str] = None,
                  image_url: Optional[Callable[[str], str]] = None) -> object:
    """按格式返回统一形状的文档对象：`.chapters()` / `.chapter_html(index)`。"""
    if kind == 'epub':
        return EpubDocument(path, extract_dir=extract_dir, image_url=image_url)
    if kind == 'md':
        return MarkdownDocument(path, encoding=encoding, root_dir=root_dir, image_url=image_url)
    raise DocError(f'不支持的格式: {kind}')


def open_with_system(path: Path) -> None:
    """用系统默认程序打开文件（三个平台的惯例各一行，不阻塞）。"""
    target = str(path)
    if sys.platform.startswith('win'):
        os.startfile(target)  # Windows 上等价于双击
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(['xdg-open', target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
