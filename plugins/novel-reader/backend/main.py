import os
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling



_parser_mod = load_sibling(__file__, 'parser', 'novel_reader')
NovelParser = _parser_mod.NovelParser

class NovelReaderPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "小说根目录", "type": "text",
         "placeholder": "默认: ./data", "help": "存放 .txt 小说文件的根目录"},
    ]

    CACHE_FILE = '.novel_cache.json'
    PROGRESS_FILE = '.novel_progress.json'
    CACHE_VERSION = 2  # 偏移格式改为 [start, end] 区间后升版

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir') or str(super().get_data_root())
        self.novel_dir = str(Path(root).resolve())

        self._cache_dir = os.path.join(self.novel_dir, '.novel_state')
        os.makedirs(self._cache_dir, exist_ok=True)

        self._novel_cache: Dict[str, dict] = {}
        self._chapter_cache: Dict[str, List[dict]] = {}
        self._offset_cache: Dict[str, List] = {}
        self._full_content_cache: Dict[str, str] = {}

        self._load_cache()

    # ===== 文件服务根目录 =====

    def get_data_root(self) -> Path:
        return Path(self.novel_dir)

    # ===== 设置持久化 =====



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
                    if data.get('parser_version') == self.CACHE_VERSION:
                        self._chapter_cache = data.get('chapters', {})
                        self._offset_cache = data.get('offsets', {})
                    else:
                        # 旧版偏移格式不可靠，丢弃章节缓存并在下次访问时重解析
                        self._chapter_cache = {}
                        self._offset_cache = {}
            except Exception:
                pass

    def _save_cache(self):
        cache_file = self._cache_path(self.CACHE_FILE)
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'parser_version': self.CACHE_VERSION,
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
        if not offsets or not isinstance(chapter_index, int) or chapter_index < 0 or chapter_index >= len(offsets):
            return {'error': '章节索引无效', 'content': ''}

        entry = offsets[chapter_index]
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            start_offset, end_offset = entry
        else:
            # 兼容旧版单偏移缓存
            start_offset = entry
            end_offset = offsets[chapter_index + 1] if chapter_index + 1 < len(offsets) else len(full_content)

        chapter_content = full_content[int(start_offset):int(end_offset)]
        return {'content': chapter_content}

    def update_progress(self, novel_id: str, chapter_index: int,
                        scroll_position: float = 0.0,
                        encoding: str = 'auto') -> Dict[str, Any]:
        """更新阅读进度"""
        progress = self._load_progress()

        chapters = self._chapter_cache.get(novel_id, [])
        total_chapters = len(chapters)
        if not isinstance(chapter_index, int) or chapter_index < 0:
            chapter_index = 0
        if total_chapters > 0:
            chapter_index = min(chapter_index, total_chapters - 1)
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
