"""EPUB 解析：zip 容器 + OPF 索引 + spine，逐章转成白名单 HTML。

为什么自己写容器解析而不是引库：
  - EPUB 就是 zip + XML 索引（META-INF/container.xml → OPF → spine），zipfile 与
    xml.etree 足够；EbookLib 会连带 lxml（编译型依赖），为这点事不值得。
  - `open_document` 的格式分派在 `documents.py`，本模块只负责 EPUB 一侧。

安全边界（两条都不可省）：

1. **所有 HTML 都由白名单转换器产出**。EPUB 的 XHTML 里的 `<script>`、`on*` 属性、
   `javascript:` 链接一旦进了插件 iframe，就拿得到与壳同源的 Bridge，所以这些内容在
   转换阶段就被丢掉。转换器实例由入口注入（`tools`），本模块不 import 兄弟模块。
2. **zip 成员名不可信**：成员名不参与落盘路径（解包出来的图片一律用成员名哈希命名），
   只接受数据根之内的相对路径，并且限制单个成员与单本书的解包体积。
"""

import hashlib
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple
from urllib.parse import unquote

# 单个 zip 成员（图片）与单本书解包总量的上限：zip 炸弹与超大图不能把磁盘写满。
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_EXTRACT_BYTES = 512 * 1024 * 1024

_EXT_RE = re.compile(r'^\.[a-z0-9]{1,5}$')
# 封面只认真正的图片扩展名：正文抽图可以把未知扩展名存成 `.img` 交给浏览器试，
# 但封面是列表里每本书都要加载的东西，落一个浏览器解不开的文件就是一张破图。
_COVER_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg'}
_SPACE_RE = re.compile(r'\s+')


class HtmlTools(Protocol):
    """`documents.HtmlTools` 的结构契约：解码、白名单转换、构造文档异常。"""

    def decode(self, raw: bytes, encoding: str = 'auto') -> str: ...

    def clean(self, fragment: str,
              image_src: Optional[Callable[[str], Optional[str]]] = None) -> str: ...

    def error(self, message: str) -> Exception: ...

    def text_probe(self) -> Any: ...


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


def _read_member(zf: zipfile.ZipFile, name: str, tools: HtmlTools) -> bytes:
    if not _is_safe_member(name):
        raise tools.error(f'EPUB 内部路径不合法: {name}')
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise tools.error(f'EPUB 里没有这个文件: {name}') from None
    if info.is_dir():
        raise tools.error(f'EPUB 内部路径是目录: {name}')
    if info.file_size > MAX_MEMBER_BYTES:
        raise tools.error(f'EPUB 成员过大: {name}（{info.file_size} 字节）')
    return zf.read(name)


def _find_opf(zf: zipfile.ZipFile, tools: HtmlTools) -> str:
    root = ET.fromstring(_read_member(zf, 'META-INF/container.xml', tools))
    for node in root.iter():
        if _local(node.tag) == 'rootfile':
            full = node.get('full-path')
            if full:
                return full.replace('\\', '/')
    raise tools.error('EPUB 的 container.xml 里没有 rootfile')


def _parse_opf(zf: zipfile.ZipFile, opf_path: str,
               tools: HtmlTools) -> Tuple[Dict[str, dict], List[str], str]:
    """返回 (manifest, spine 的 zip 成员路径列表, 封面成员路径)。

    封面的三条来路都要认，实测三种书各占一部分：

      1. EPUB3：manifest 里 `<item properties="cover-image">`；
      2. EPUB2：`<metadata><meta name="cover" content="封面 item 的 id">`；
      3. 更老的：`<guide><reference type="cover" href="...">`（部分书只有这条）。

    封面文件**通常不在 spine 里**（它不是正文），所以必须单独取出来 —— 靠读 spine
    顺手带出封面是行不通的。三条都没有就返回空串，交给前端画生成式封面。
    """
    root = ET.fromstring(_read_member(zf, opf_path, tools))
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

    cover = ''
    for item in manifest.values():
        if 'cover-image' in item['properties'] and 'image' in item['type']:
            cover = item['href']
            break
    if not cover:
        cover_id = ''
        for node in root.iter():
            if _local(node.tag) == 'meta' and (node.get('name') or '').lower() == 'cover':
                cover_id = (node.get('content') or '').strip()
                break
        item = manifest.get(cover_id)
        if item and item['href']:
            cover = item['href']
    if not cover:
        for node in root.iter():
            if _local(node.tag) != 'reference':
                continue
            if (node.get('type') or '').lower() != 'cover':
                continue
            href = (node.get('href') or '').strip()
            if href:
                cover = _zip_join(opf_dir, href)
                break
    if cover and not _is_safe_member(cover):
        cover = ''

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
    return manifest, spine, cover


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


def _toc_titles(zf: zipfile.ZipFile, manifest: Dict[str, dict],
                tools: HtmlTools) -> Dict[str, str]:
    """成员路径 → 标题。EPUB3 的 nav 优先，EPUB2 的 ncx 补空缺。"""
    titles: Dict[str, str] = {}

    nav_path = next((item['href'] for item in manifest.values() if 'nav' in item['properties']), '')
    if nav_path:
        try:
            base = posixpath.dirname(nav_path)
            collector = _AnchorCollector()
            collector.feed(tools.decode(_read_member(zf, nav_path, tools)))
            collector.close()
            for href, title in collector.items:
                key = _zip_join(base, href)
                if key and title:
                    titles.setdefault(key, title)
        except Exception:
            pass

    ncx_path = next((item['href'] for item in manifest.values()
                     if item['type'] == 'application/x-dtbncx+xml'), '')
    if ncx_path:
        try:
            base = posixpath.dirname(ncx_path)
            root = ET.fromstring(_read_member(zf, ncx_path, tools))
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
        except Exception:
            pass

    return titles


class EpubDocument:
    """spine 即目录：一章一个 XHTML 文档，转换时抽图并按需落盘。

    ponytail: 只取正文与图片，原书的 CSS / 字体 / 分栏一律丢弃（排版交给阅读器
    自己的主题）。要还原原书版式就得引 Epub.js 那类整包渲染器，不值得 —— 现有
    主题、字号、行距、双模式全部会失效。
    """

    kind = 'epub'

    def __init__(self, path: str, tools: HtmlTools, extract_dir: Optional[str] = None,
                 image_url: Optional[Callable[[str], str]] = None) -> None:
        self._path = str(path)
        self._tools = tools
        self._extract_dir = Path(extract_dir) if extract_dir else None
        self._image_url = image_url
        self._spine: List[Dict[str, str]] = []
        self._meta: Dict[int, Tuple[str, int]] = {}
        self._extracted_bytes = 0
        self._cover_href = ''
        self._load()

    # ---- 解析 ----

    def _load(self) -> None:
        if not zipfile.is_zipfile(self._path):
            raise self._tools.error('不是有效的 EPUB（zip）文件')
        try:
            with zipfile.ZipFile(self._path) as zf:
                opf_path = _find_opf(zf, self._tools)
                manifest, spine, cover = _parse_opf(zf, opf_path, self._tools)
                titles = _toc_titles(zf, manifest, self._tools)
                self._cover_href = cover
        except (zipfile.BadZipFile, OSError, ET.ParseError) as e:
            raise self._tools.error(f'EPUB 结构无法解析: {e}') from None
        if not spine:
            raise self._tools.error('EPUB 的 spine 为空，没有可读的正文')
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
            raise self._tools.error('章节索引无效')
        href = self._spine[index]['href']
        with zipfile.ZipFile(self._path) as zf:
            raw = _read_member(zf, href, self._tools)
            return self._tools.clean(self._tools.decode(raw), image_src=self._image_resolver(zf, href))

    def cover(self) -> Optional[str]:
        """封面图的 URL；没有封面返回 None（前端画生成式封面）。"""
        href = self._cover_href
        if not href or self._extract_dir is None or self._image_url is None:
            return None
        try:
            with zipfile.ZipFile(self._path) as zf:
                try:
                    info = zf.getinfo(href)
                except KeyError:
                    return None
                if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                    return None
                ext = posixpath.splitext(href)[1].lower()
                # 与正文抽图同一套成员名校验，避免把 zip 里的垃圾成员当图片落盘
                if not _is_safe_member(href) or ext not in _COVER_EXTS:
                    return None
                target = self._extract_dir / ('cover' + ext)
                if not target.is_file():
                    self._extract_dir.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(href))
                return self._image_url(str(target))
        except (zipfile.BadZipFile, OSError):
            return None

    # ---- 章节元信息 ----

    def _describe(self, zf: zipfile.ZipFile, index: int, item: Dict[str, str]) -> Tuple[str, int]:
        toc_title = item.get('title') or ''
        fallback = toc_title or f'第{index + 1}章'
        try:
            probe = self._tools.text_probe()
            probe.feed(self._tools.decode(_read_member(zf, item.get('href') or '', self._tools)))
            probe.close()
        except Exception:
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
