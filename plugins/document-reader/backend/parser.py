"""TXT 解析器（章节识别与偏移切分）。"""

import os
import re
from typing import ClassVar, List, Optional, Pattern, Tuple

try:
    import chardet
except ImportError:
    chardet = None


class TxtParser:
    """章节识别与内容切分。

    返回的 offset 为 [content_start, content_end] 闭开区间，
    确保章节内容既不包含本章标题，也不包含下一章标题。
    """

    CHAPTER_PATTERNS: ClassVar[List[Pattern[str]]] = [
        re.compile(r'^第\s*[零一二三四五六七八九十百千万\d]+\s*章'),
        re.compile(r'^第\s*[零一二三四五六七八九十百千万\d]+\s*节'),
        re.compile(r'^第\s*\d+\s*章'),
        re.compile(r'^第\s*\d+\s*节'),
        re.compile(r'^[卷部]\s*[零一二三四五六七八九十百千万\d]+'),
    ]

    @staticmethod
    def detect_encoding(file_path: str) -> str:
        """猜编码。

        顺序：BOM → UTF-8 自证 → chardet → gb18030。

        为什么不先信 chardet：它对中文 txt 的置信度经常低得离谱（实测一本 5 MB 的
        GBK 小说被猜成 koi8-u、置信度 0.036），旧代码低于 0.5 就直接当 UTF-8，
        于是整个文件只能靠"解码失败后的兜底"来救，而那个兜底恰恰是错的。
        UTF-8 能自证，所以先试它；反过来 chardet 只用来在"不是 UTF-8"之后分辨是
        哪种中文编码（big5 / shift_jis / 无 BOM 的 utf-16 …）。
        """
        try:
            with open(file_path, 'rb') as f:
                raw_data = f.read(100000)
        except OSError:
            return 'utf-8'

        if raw_data[:2] in (b'\xff\xfe', b'\xfe\xff'):
            return 'utf-16'
        # 采样是按固定长度切的，末尾可能正好切在一个汉字的中间：只容忍末尾一个替换符
        if '\ufffd' not in raw_data.decode('utf-8', errors='replace')[:-1]:
            return 'utf-8'
        if chardet:
            result = chardet.detect(raw_data) or {}
            guess = (result.get('encoding') or '').lower()
            if guess in ('gb2312', 'gbk', 'gb18030'):
                return 'gb18030'        # gb2312 / gbk 都是 gb18030 的子集
            if guess and (result.get('confidence') or 0) > 0.5:
                return guess
        return 'gb18030'                # 中文 txt 的大多数

    @staticmethod
    def _decode_strict(raw_bytes: bytes, encoding: str) -> Optional[str]:
        """按 encoding 严格解码；只有"末尾被截断"才容忍，丢掉最后那个不完整的字。

        返回 None 表示这个编码解不开（选错了编码，或者文件本身就是坏的）。
        """
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError as e:
            if e.start >= len(raw_bytes) - 4:
                return raw_bytes[:e.start].decode(encoding, errors='replace')
            return None
        except LookupError:
            return None

    @staticmethod
    def read_full_content(file_path: str, encoding: str = 'auto') -> str:
        """按编码读出全文。

        界面上没有编码开关（`auto` 是唯一会被用到的值）；显式编码保留给 API 调用方，
        行为与 auto 一致 —— 解不开就沿解码链往下走，不会因为一个错的编码把整本书作废。
        """
        with open(file_path, 'rb') as f:
            raw_bytes = f.read()
        if not encoding or encoding == 'auto':
            encoding = TxtParser.detect_encoding(file_path)
        # GBK 家族统一按 gb18030 解：gb2312 / gbk 都是它的子集，传进来的编码是哪个
        # 都不该因为正文里一个 GBK 扩展字就整篇解不开。
        if encoding.lower().startswith('gb'):
            encoding = 'gb18030'
        # 选中的编码解不开就按最可能的顺序再试（chardet 猜错、文件被截断、传进来的编码
        # 本身就不对）。绝不能退回 utf-8 + errors='ignore'：那对中文 GBK 只会吐出满屏
        # 乱码，而且不抛异常 —— 用户看到的就是"换哪个编码都读不出来"，还找不到原因。
        for candidate in (encoding, 'gb18030', 'utf-8'):
            text = TxtParser._decode_strict(raw_bytes, candidate)
            if text is not None:
                return text
        # 谁都不行：就地替换着解，至少把能看的部分给出来
        return raw_bytes.decode('gb18030', errors='replace')

    @staticmethod
    def parse_txt(file_path: str, encoding: str = 'auto') -> Tuple[List[dict], List[Tuple[int, int]]]:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        content = TxtParser.read_full_content(file_path, encoding)
        lines = content.splitlines(keepends=True)

        line_offsets = [0]
        for line in lines:
            line_offsets.append(line_offsets[-1] + len(line))

        chapter_starts = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            for pattern in TxtParser.CHAPTER_PATTERNS:
                if pattern.match(stripped):
                    chapter_starts.append((i, stripped))
                    break

        if not chapter_starts:
            return TxtParser._split_by_word_count(content)

        chapters = []
        offsets = []

        for idx, (start_line, title) in enumerate(chapter_starts):
            # 内容从标题行的下一行开始，并跳过紧随其后的空行。
            content_start_line = start_line + 1
            while content_start_line < len(lines) and not lines[content_start_line].strip():
                content_start_line += 1

            if idx + 1 < len(chapter_starts):
                # 结束位置是下一章标题行起点，正好排除下一章标题。
                end_offset = line_offsets[chapter_starts[idx + 1][0]]
            else:
                end_offset = line_offsets[-1]

            start_offset = line_offsets[content_start_line]
            if start_offset > end_offset:
                start_offset = end_offset

            chapter_text = content[start_offset:end_offset]
            word_count = len(chapter_text.replace('\n', '').replace(' ', '').replace('\r', ''))

            chapters.append({
                'index': idx,
                'title': title,
                'word_count': word_count
            })
            offsets.append((start_offset, end_offset))

        return chapters, offsets

    @staticmethod
    def _split_by_word_count(content: str, words_per_chapter: int = 5000) -> Tuple[List[dict], List[Tuple[int, int]]]:
        """无章节标题时按字数切分，并保留原始字符偏移。"""
        paragraphs = []
        position = 0
        for line in content.splitlines(keepends=True):
            stripped = line.strip()
            if stripped:
                paragraphs.append((position, stripped))
            position += len(line)

        chapters = []
        offsets = []
        current_chapter = []
        current_word_count = 0
        chapter_index = 0
        start_offset = 0

        for offset, para in paragraphs:
            if not current_chapter:
                start_offset = offset
            current_chapter.append(para)
            current_word_count += len(para)
            if current_word_count >= words_per_chapter:
                chapters.append({
                    'index': chapter_index,
                    'title': f'第{chapter_index + 1}章',
                    'word_count': current_word_count
                })
                offsets.append((start_offset, offset + len(para)))
                current_chapter = []
                current_word_count = 0
                chapter_index += 1

        if current_chapter:
            chapters.append({
                'index': chapter_index,
                'title': f'第{chapter_index + 1}章',
                'word_count': current_word_count
            })
            offsets.append((start_offset, len(content)))

        return chapters, offsets
