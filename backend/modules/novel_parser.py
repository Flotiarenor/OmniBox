# backend/modules/novel_parser.py
import re
import os
import chardet
from typing import List, Tuple, Optional


class NovelParser:
    """
    小说文件解析器
    简化版：支持手动选择编码
    """
    
    CHAPTER_PATTERNS = [
        re.compile(r'第\s*[零一二三四五六七八九十百千万\d]+\s*章'),
        re.compile(r'第\s*[零一二三四五六七八九十百千万\d]+\s*节'),
        re.compile(r'第\s*\d+\s*章'),
        re.compile(r'第\s*\d+\s*节'),
        re.compile(r'\d+[\.、]'),
        re.compile(r'^[卷部]\s*[零一二三四五六七八九十百千万\d]+'),
    ]
    
    @staticmethod
    def detect_encoding(file_path: str) -> str:
        """自动检测编码"""
        try:
            with open(file_path, 'rb') as f:
                raw_data = f.read(100000)
                result = chardet.detect(raw_data)
                
                if result['encoding'] and result['confidence'] > 0.5:
                    encoding = result['encoding'].lower()
                    # 统一编码名称
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
        """
        读取文件完整内容
        支持手动指定编码或自动检测
        """
        with open(file_path, 'rb') as f:
            raw_bytes = f.read()
        
        if encoding == 'auto':
            encoding = NovelParser.detect_encoding(file_path)
        
        # 尝试解码
        try:
            return raw_bytes.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            # 如果指定编码失败，尝试其他编码
            for enc in ['utf-8', 'gbk', 'gb2312', 'gb18030']:
                if enc != encoding:
                    try:
                        return raw_bytes.decode(enc)
                    except (UnicodeDecodeError, UnicodeError):
                        continue
            # 最后手段
            return raw_bytes.decode('utf-8', errors='ignore')
    
    @staticmethod
    def parse_txt(file_path: str, encoding: str = 'auto') -> Tuple[List[dict], List[int]]:
        """
        解析txt文件，返回章节信息和字符偏移
        使用字符偏移而不是字节偏移，更准确
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")
        
        # 读取完整内容
        content = NovelParser.read_full_content(file_path, encoding)
        
        # 按行分割
        lines = content.split('\n')
        chapters = []
        offsets = []
        
        # 查找章节标题
        chapter_starts = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            for pattern in NovelParser.CHAPTER_PATTERNS:
                if pattern.search(stripped):
                    chapter_starts.append((i, stripped))
                    break
        
        # 如果没有找到章节标题，按固定字数分章
        if not chapter_starts:
            return NovelParser._split_by_word_count(content)
        
        # 计算字符偏移
        for idx, (start_line, title) in enumerate(chapter_starts):
            # 计算起始字符位置
            start_offset = len('\n'.join(lines[:start_line]))
            if start_line > 0:
                start_offset += 1  # 换行符
            
            # 计算结束字符位置
            if idx + 1 < len(chapter_starts):
                end_line = chapter_starts[idx + 1][0]
                end_offset = len('\n'.join(lines[:end_line]))
            else:
                end_offset = len(content)
            
            # 计算字数
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
        """按字数分章"""
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