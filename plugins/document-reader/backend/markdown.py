"""Markdown 的章节模型：按 1~3 级标题切章，逐章渲染成白名单 HTML。

为什么单独一个模块：`documents.py` 是"所有格式共用的白名单转换 + 分派"，Markdown 与
EPUB 各有自己的章节模型；两者放在同一个文件里会把 712 行的旧文件原样搬过来（门禁 R2）。
HTML 仍然只从 `documents.HTML_TOOLS.clean` 产出 —— 转换器实例由入口注入，本模块不
import 兄弟模块。
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Tuple
from urllib.parse import unquote

try:
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover - requirements.txt 已声明，缺了只有 md 不可用
    MarkdownIt = None

_HEADING_RE = re.compile(r'^(#{1,3})[ \t]+(.+?)[ \t]*#*[ \t]*$')
_SPACE_RE = re.compile(r'\s+')

_MD = None
if MarkdownIt is not None:
    # commonmark 预设 + 表格/删除线（这两个规则是 markdown-it 自带的，不引新包；
    # gfm-like 预设会打开 linkify，那需要额外的 linkify-it-py，不引）。
    _MD = MarkdownIt('commonmark', {'html': False, 'breaks': False}).enable(['table', 'strikethrough'])


class HtmlTools(Protocol):
    """`documents.HtmlTools` 的结构契约：白名单转换与构造文档异常。"""

    def clean(self, fragment: str,
              image_src: Optional[Callable[[str], Optional[str]]] = None) -> str: ...

    def error(self, message: str) -> Exception: ...


class MarkdownDocument:
    """按 1~3 级标题切章，逐章渲染成 HTML。

    ponytail: 只认 ATX（`#`）标题。Setext 下划线式标题（标题 + `===`）不切章，
    整篇会变成一章 —— 真遇到这种文档再补一条正则。
    """

    kind = 'md'

    def __init__(self, path: str, tools: HtmlTools, read_text: Callable[[str, str], str],
                 encoding: str = 'auto', root_dir: Optional[str] = None,
                 image_url: Optional[Callable[[str], str]] = None) -> None:
        self._path = str(path)
        self._tools = tools
        self._read_text = read_text
        self._encoding = encoding
        self._image_url = image_url
        self._root_dir = Path(root_dir).resolve() if root_dir else None
        self._chapters: Optional[List[Tuple[Optional[str], str]]] = None

    def cover(self) -> Optional[str]:
        """Markdown 没有封面这个概念 —— 交给前端画生成式封面。"""
        return None

    def _load(self) -> List[Tuple[Optional[str], str]]:
        if self._chapters is not None:
            return self._chapters
        text = self._read_text(self._path, self._encoding)
        segments: List[Tuple[Optional[str], str]] = []
        title: Optional[str] = None
        buf: List[str] = []
        for line in text.splitlines():
            match = _HEADING_RE.match(line)
            if match:
                # 第一个标题之前的内容算"前言"；没有标题时末尾那次 flush 仍是 None，
                # 由 chapters() 回落到文件名（整篇没有标题的文档不该叫"前言"）
                _flush(segments, title if title is not None else '前言', buf)
                title, buf = match.group(2).strip(), [line]
                continue
            buf.append(line)
        _flush(segments, title, buf)
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
            raise self._tools.error('章节索引无效')
        if _MD is None:
            raise self._tools.error('markdown-it-py 不可用，无法渲染 Markdown')
        return self._tools.clean(_MD.render(segments[index][1]), image_src=self._resolve_image)

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


def _flush(segments: List[Tuple[Optional[str], str]], title: Optional[str],
           buf: List[str]) -> None:
    source = '\n'.join(buf)
    if source.strip():
        segments.append((title, source))
