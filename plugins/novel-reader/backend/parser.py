"""小说 TXT 解析器。"""

import os
import re
from typing import List, Tuple

try:
    import chardet
except ImportError:
    chardet = None


class NovelParser:
    """章节识别与内容切分。

    返回的 offset 为 [content_start, content_end] 闭开区间，
    确保章节内容既不包含本章标题，也不包含下一章标题。
    """

    CHAPTER_PATTERNS = [
        re.compile(r'^第\s*[零一二三四五六七八九十百千万\d]+\s*章'),
        re.compile(r'^第\s*[零一二三四五六七八九十百千万\d]+\s*节'),
        re.compile(r'^第\s*\d+\s*章'),
        re.compile(r'^第\s*\d+\s*节'),
        re.compile(r'^[卷部]\s*[零一二三四五六七八九十百千万\d]+'),
    ]

    @staticmethod
    def detect_encoding(file_path: str) -> str:
        try:
            with open(file_path, 'rb') as f:
                raw_data = f.read(100000)
                if chardet:
                    result = chardet.detect(raw_data)
                    if result.get('encoding') and result.get('confidence', 0) > 0.5:
                        encoding = result['encoding'].lower()
                        if encoding in ('gb2312', 'gbk', 'gb18030'):
                            return 'gbk'
                        if encoding in ('utf-8', 'utf8'):
                            return 'utf-8'
                        if 'utf-16' in encoding:
                            return 'utf-16'
                        return encoding
        except Exception:
            pass
        return 'utf-8'

    @staticmethod
    def read_full_content(file_path: str, encoding: str = 'auto') -> str:
        with open(file_path, 'rb') as f:
            raw_bytes = f.read()
        if encoding == 'auto':
            encoding = NovelParser.detect_encoding(file_path)
        try:
            return raw_bytes.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            for enc in ('utf-8', 'gbk', 'gb2312', 'gb18030'):
                if enc != encoding:
                    try:
                        return raw_bytes.decode(enc)
                    except (UnicodeDecodeError, UnicodeError):
                        continue
            return raw_bytes.decode('utf-8', errors='ignore')

    @staticmethod
    def parse_txt(file_path: str, encoding: str = 'auto') -> Tuple[List[dict], List[Tuple[int, int]]]:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        content = NovelParser.read_full_content(file_path, encoding)
        lines = content.splitlines(keepends=True)

        line_offsets = [0]
        for line in lines:
            line_offsets.append(line_offsets[-1] + len(line))

        chapter_starts = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            for pattern in NovelParser.CHAPTER_PATTERNS:
                if pattern.match(stripped):
                    chapter_starts.append((i, stripped))
                    break

        if not chapter_starts:
            return NovelParser._split_by_word_count(content)

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
