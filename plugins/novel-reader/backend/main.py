import os
import re
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from shell.backend.plugin_base import PluginBase

try:
    import chardet
except ImportError:
    chardet = None


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
                if chardet:
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


class NovelReaderPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "小说根目录", "type": "text",
         "placeholder": "默认: ./data", "help": "存放 .txt 小说文件的根目录"},
    ]

    CACHE_FILE = '.novel_cache.json'
    PROGRESS_FILE = '.novel_progress.json'

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self.settings_file = Path(__file__).parent.parent / 'settings.json'
        self._settings = {}
        root = self._resolved_config.get('root_dir') or str(super().get_data_root())
        self.novel_dir = str(Path(root).resolve())

        self._cache_dir = os.path.join(self.novel_dir, '.novel_state')
        os.makedirs(self._cache_dir, exist_ok=True)

        self._novel_cache: Dict[str, dict] = {}
        self._chapter_cache: Dict[str, List[dict]] = {}
        self._offset_cache: Dict[str, List[int]] = {}
        self._full_content_cache: Dict[str, str] = {}

        self._load_cache()

    # ===== 文件服务根目录 =====

    def get_data_root(self) -> Path:
        return Path(self.novel_dir)

    # ===== 设置持久化 =====

    def _load_settings(self) -> dict:
        if self.settings_file.exists():
            try:
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_settings_to_file(self):
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self._settings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[NovelReader] 保存设置失败: {e}")

    def on_settings_changed(self, changed_keys):
        if 'root_dir' in changed_keys:
            new_dir = self.setting('root_dir')
            if new_dir and Path(new_dir).is_dir():
                self._apply_root_dir(str(Path(new_dir).resolve()))

    def _apply_root_dir(self, new_dir: str):
        self.novel_dir = new_dir
        self._cache_dir = os.path.join(self.novel_dir, '.novel_state')
        os.makedirs(self._cache_dir, exist_ok=True)
        self._novel_cache = {}
        self._chapter_cache = {}
        self._offset_cache = {}
        self._full_content_cache = {}
        self._load_cache()

    # ===== API 注册 =====

    def register_api(self) -> dict:
        return {
            'novel_list': self.list_novels,
            'novel_get_chapters': self.get_chapters,
            'novel_get_content': self.get_content,
            'novel_update_progress': self.update_progress,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    # ===== 核心业务（由旧版 NovelModule 迁移） =====

    def _cache_path(self, name: str) -> str:
        return os.path.join(self._cache_dir, name)

    def _load_cache(self):
        cache_file = self._cache_path(self.CACHE_FILE)
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._novel_cache = data.get('novels', {})
                    self._chapter_cache = data.get('chapters', {})
                    self._offset_cache = data.get('offsets', {})
            except Exception:
                pass

    def _save_cache(self):
        cache_file = self._cache_path(self.CACHE_FILE)
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'novels': self._novel_cache,
                    'chapters': self._chapter_cache,
                    'offsets': self._offset_cache
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_progress(self) -> dict:
        progress_file = self._cache_path(self.PROGRESS_FILE)
        if os.path.exists(progress_file):
            try:
                with open(progress_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_progress(self, progress: dict):
        progress_file = self._cache_path(self.PROGRESS_FILE)
        try:
            with open(progress_file, 'w', encoding='utf-8') as f:
                json.dump(progress, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def list_novels(self) -> Dict[str, Any]:
        """获取小说列表，检查文件修改时间"""
        current_files = {}
        if os.path.exists(self.novel_dir):
            for entry in os.scandir(self.novel_dir):
                if entry.is_file() and entry.name.endswith('.txt'):
                    stat = entry.stat()
                    current_files[entry.name] = {
                        'mtime': stat.st_mtime,
                        'size': stat.st_size
                    }

        # 检查哪些文件需要更新
        needs_update = False
        for name, info in current_files.items():
            novel_id = os.path.splitext(name)[0]
            cached = self._novel_cache.get(novel_id)

            # 如果文件是新添加的，或者修改时间/大小变化了，需要重新解析
            if not cached or \
               cached.get('mtime') != info['mtime'] or \
               cached.get('size') != info['size']:
                needs_update = True
                # 清除该小说的章节和内容缓存
                self._chapter_cache.pop(novel_id, None)
                self._offset_cache.pop(novel_id, None)
                self._full_content_cache.pop(novel_id, None)

        # 检查是否有文件被删除
        cached_ids = set(self._novel_cache.keys())
        current_ids = {os.path.splitext(name)[0] for name in current_files.keys()}
        if cached_ids != current_ids:
            needs_update = True

        # 如果没有变化，直接返回缓存
        if not needs_update and self._novel_cache:
            return {'novels': list(self._novel_cache.values())}

        # 重新扫描
        novels = []
        for entry in os.scandir(self.novel_dir):
            if entry.is_file() and entry.name.endswith('.txt'):
                novel_info = self._scan_novel_meta(entry)
                if novel_info:
                    novels.append(novel_info)

        self._novel_cache = {n['id']: n for n in novels}
        self._save_cache()

        return {'novels': novels}

    def _scan_novel_meta(self, entry: os.DirEntry) -> Optional[dict]:
        """扫描小说元信息，记录修改时间和大小"""
        try:
            name_without_ext = os.path.splitext(entry.name)[0]
            parts = name_without_ext.split('-')
            title = parts[0].strip()
            author = parts[1].strip() if len(parts) > 1 else '未知作者'

            stat = entry.stat()

            progress = self._load_progress()
            novel_progress = progress.get(name_without_ext, {})

            return {
                'id': name_without_ext,
                'title': title,
                'author': author,
                'file_path': entry.path,
                'file_size': stat.st_size,
                'mtime': stat.st_mtime,  # 记录修改时间
                'chapter_count': 0,
                'last_read_time': novel_progress.get('last_read_time', ''),
                'last_read_chapter': novel_progress.get('last_read_chapter', 0),
                'progress': novel_progress.get('progress', 0.0),
                'encoding': novel_progress.get('encoding', 'auto')
            }
        except Exception as e:
            print(f"扫描小说失败 {entry.name}: {e}")
            return None

    def get_chapters(self, novel_id: str, encoding: str = 'auto') -> Dict[str, Any]:
        """获取章节列表"""
        if novel_id in self._chapter_cache:
            return {'chapters': self._chapter_cache[novel_id]}

        novel_info = self._novel_cache.get(novel_id)
        if not novel_info:
            return {'error': '小说不存在', 'chapters': []}

        try:
            chapters, offsets = NovelParser.parse_txt(novel_info['file_path'], encoding)

            self._chapter_cache[novel_id] = chapters
            self._offset_cache[novel_id] = offsets
            self._save_cache()

            if novel_id in self._novel_cache:
                self._novel_cache[novel_id]['chapter_count'] = len(chapters)

            return {'chapters': chapters}
        except Exception as e:
            return {'error': f'解析章节失败: {e}', 'chapters': []}

    def get_full_content(self, novel_id: str, encoding: str = 'auto') -> Dict[str, Any]:
        """获取小说完整内容"""
        if novel_id in self._full_content_cache:
            return {'content': self._full_content_cache[novel_id]}

        novel_info = self._novel_cache.get(novel_id)
        if not novel_info:
            return {'error': '小说不存在', 'content': ''}

        try:
            content = NovelParser.read_full_content(novel_info['file_path'], encoding)
            self._full_content_cache[novel_id] = content
            return {'content': content}
        except Exception as e:
            return {'error': f'读取小说失败: {e}', 'content': ''}

    def get_content(self, novel_id: str, chapter_index: int,
                    encoding: str = 'auto') -> Dict[str, Any]:
        """获取指定章节内容"""
        content_result = self.get_full_content(novel_id, encoding)
        if content_result.get('error'):
            return content_result

        full_content = content_result['content']

        if novel_id not in self._offset_cache:
            chapters_result = self.get_chapters(novel_id, encoding)
            if chapters_result.get('error'):
                return {'error': chapters_result['error'], 'content': ''}

        offsets = self._offset_cache.get(novel_id)
        if not offsets or chapter_index >= len(offsets):
            return {'error': '章节索引无效', 'content': ''}

        start_offset = offsets[chapter_index]
        if chapter_index + 1 < len(offsets):
            end_offset = offsets[chapter_index + 1]
            chapter_content = full_content[start_offset:end_offset]
        else:
            chapter_content = full_content[start_offset:]

        return {'content': chapter_content}

    def update_progress(self, novel_id: str, chapter_index: int,
                        scroll_position: float = 0.0,
                        encoding: str = 'auto') -> Dict[str, Any]:
        """更新阅读进度"""
        progress = self._load_progress()

        chapters = self._chapter_cache.get(novel_id, [])
        total_chapters = len(chapters)
        overall_progress = (chapter_index + scroll_position) / total_chapters if total_chapters > 0 else 0.0

        progress[novel_id] = {
            'last_read_time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'last_read_chapter': chapter_index,
            'scroll_position': scroll_position,
            'progress': round(overall_progress, 4),
            'encoding': encoding
        }

        self._save_progress(progress)

        if novel_id in self._novel_cache:
            self._novel_cache[novel_id].update({
                'last_read_time': progress[novel_id]['last_read_time'],
                'last_read_chapter': chapter_index,
                'progress': overall_progress,
                'encoding': encoding
            })

        return {'success': True}
