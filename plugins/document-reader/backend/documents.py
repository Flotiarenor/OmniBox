"""阅读器的富文本入口：白名单 HTML 转换、文本解码与格式分派。

各格式的章节模型分成三个模块：`markdown.py`（Markdown）、`epub.py`（EPUB）、
`parser.py`（TXT 章节偏移）。本模块只放三样东西：

1. **所有 HTML 都由本模块的白名单转换器产出**。EPUB 的 XHTML 与 Markdown 里的
   `<script>`、`on*` 属性、`javascript:` 链接一旦进了插件 iframe，就拿得到与壳
   同源的 Bridge（可读任意文件、可调任意插件 API），所以这些内容在转换阶段就被
   丢掉，而不是"先注入再 innerHTML，再想办法消毒"。
2. 文本解码：zip 成员没有独立文件路径，编码只能就地判（EPUB 侧用它）。
3. `open_document`：按 kind 分派到注入的章节模型类，以及"用系统程序打开"。

组件形态：本模块是模块级函数 + 无状态类，由入口把 `TxtParser.read_full_content`、
Markdown 与 EPUB 的工厂函数注入 `open_document`，因此它不 import 任何兄弟模块。
zip 成员名不可信这条边界在 `epub.py`（成员名不参与落盘路径，只接受数据根之内的
相对路径，并限制单个成员与单本书的解包体积）。
"""

import html
import os
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import chardet
except ImportError:  # pragma: no cover - 缺包时按 UTF-8/gb18030 兜底
    chardet = None


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

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self._start(tag, dict(attrs))

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
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

    def _start(self, tag: str, attrs: Dict[str, Optional[str]]) -> None:
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

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
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


class HtmlTools:
    """两种格式共用的三件事：解码、白名单转换、构造本模块的异常。

    `epub.py` 不 import 本模块（后端模块之间不互相 import），由入口把本对象的
    实例注入 `EpubDocument`；这样"所有富文本都过同一个白名单转换器"这条边界
    仍然只有一份实现。
    """

    @staticmethod
    def decode(raw: bytes, encoding: str = 'auto') -> str:
        return _decode(raw, encoding)

    @staticmethod
    def clean(fragment: str,
              image_src: Optional[Callable[[str], Optional[str]]] = None) -> str:
        return _clean(fragment, image_src)

    @staticmethod
    def error(message: str) -> DocError:
        return DocError(message)

    @staticmethod
    def text_probe() -> HTMLParser:
        """章节正文的纯文本探测器（EPUB 侧用它取标题兜底与字数）。"""
        return _TextProbe()


HTML_TOOLS = HtmlTools()

# ============================================================
# 对外接口
# ============================================================

def open_document(path: str, kind: str, *, read_text: Callable[[str, str], str],
                  epub_cls: Callable[..., Any], markdown_cls: Callable[..., Any],
                  encoding: str = 'auto', root_dir: Optional[str] = None,
                  extract_dir: Optional[str] = None,
                  image_url: Optional[Callable[[str], str]] = None) -> Any:
    """按格式返回统一形状的文档对象：`.chapters()` / `.chapter_html(index)` / `.cover()`。

    `.cover()` 返回封面图 URL 或 None（None 表示"这本书没有封面图，前端自己画一张"）。
    三个格式相关依赖由入口注入：`read_text` 是 TXT 的全文读取
    （`TxtParser.read_full_content`），`epub_cls` 与 `markdown_cls` 是各自的章节模型类。
    """
    if kind == 'epub':
        return epub_cls(path, tools=HTML_TOOLS, extract_dir=extract_dir, image_url=image_url)
    if kind == 'md':
        return markdown_cls(path, tools=HTML_TOOLS, read_text=read_text, encoding=encoding,
                            root_dir=root_dir, image_url=image_url)
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
