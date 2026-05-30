# backend/modules/novel_parser.py
import re
import os
import chardet
from typing import List, Tuple, Optional


class NovelParser:
    """
    小说文件解析器
    严格保证返回的章节内容不包含任何标题行
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
                result = chardet.detect(raw_data)
                if result['encoding'] and result['confidence'] > 0.5:
                    encoding = result['encoding'].lower()
                    if encoding in ['gb2312', 'gbk', 'gb18030']:
                        return 'gbk'
                    elif encoding in ['utf-8', 'utf8']:
                        return 'utf-8'
                    elif 'utf-16' in encoding:
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
            for enc in ['utf-8', 'gbk', 'gb2312', 'gb18030']:
                if enc != encoding:
                    try:
                        return raw_bytes.decode(enc)
                    except (UnicodeDecodeError, UnicodeError):
                        continue
            return raw_bytes.decode('utf-8', errors='ignore')
    
    @staticmethod
    def parse_txt(file_path: str, encoding: str = 'auto') -> Tuple[List[dict], List[int]]:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")
        
        content = NovelParser.read_full_content(file_path, encoding)
        lines = content.split('\n')
        
        # 预计算每行的起始字符偏移（包括换行符）
        line_offsets = [0]
        for line in lines:
            line_offsets.append(line_offsets[-1] + len(line) + 1)  # +1 for \n
        
        # 查找章节标题
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
            # 内容从标题行的下一行开始
            content_start_line = start_line
            
            # 跳过标题后的空行
            while content_start_line < len(lines) and not lines[content_start_line].strip():
                content_start_line += 1
            
            # 结束位置：下一章标题行（不包含标题行本身）
            if idx + 1 < len(chapter_starts):
                end_line = chapter_starts[idx + 1][0]  # 下一章标题所在行
            else:
                end_line = len(lines)
            
            # 使用预计算的偏移量
            start_offset = line_offsets[content_start_line]
            end_offset = line_offsets[end_line]
            
            # 提取章节内容
            chapter_text = content[start_offset:end_offset]
            word_count = len(chapter_text.replace('\n', '').replace(' ', '').replace('\r', ''))
            
            chapters.append({
                'index': idx,
                'title': title,
                'word_count': word_count
            })
            offsets.append(start_offset)
        
        return chapters, offsets
    
    @staticmethod
    def _split_by_word_count(content: str, words_per_chapter: int = 5000) -> Tuple[List[dict], List[int]]:
        chapters = []
        offsets = []
        paragraphs = content.split('\n')
        current_chapter = []
        current_word_count = 0
        chapter_index = 0
        start_offset = 0
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            current_chapter.append(para)
            current_word_count += len(para)
            if current_word_count >= words_per_chapter:
                chapter_text = '\n'.join(current_chapter)
                chapters.append({
                    'index': chapter_index,
                    'title': f'第{chapter_index + 1}章',
                    'word_count': current_word_count
                })
                offsets.append(start_offset)
                start_offset += len(chapter_text)
                current_chapter = []
                current_word_count = 0
                chapter_index += 1
        
        if current_chapter:
            chapter_text = '\n'.join(current_chapter)
            chapters.append({
                'index': chapter_index,
                'title': f'第{chapter_index + 1}章',
                'word_count': current_word_count
            })
            offsets.append(start_offset)
        
        return chapters, offsets