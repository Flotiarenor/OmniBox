# backend/modules/novel_module.py
import os
import json
import time
from typing import Dict, List, Any, Optional
from .novel_parser import NovelParser


class NovelModule:
    """
    小说管理模块
    简化版：直接加载全部内容，不做分步加载
    """
    
    CACHE_FILE = '.novel_cache.json'
    PROGRESS_FILE = '.novel_progress.json'
    
    def __init__(self, novel_dir: str):
        self.novel_dir = novel_dir
        self._cache_dir = os.path.join(novel_dir, '.novel_state')
        os.makedirs(self._cache_dir, exist_ok=True)
        
        self._novel_cache: Dict[str, dict] = {}
        self._chapter_cache: Dict[str, List[dict]] = {}
        self._offset_cache: Dict[str, List[int]] = {}
        self._full_content_cache: Dict[str, str] = {}  # 缓存完整内容
        
        self._load_cache()
    
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
        """获取小说列表"""
        current_files = set()
        if os.path.exists(self.novel_dir):
            for entry in os.scandir(self.novel_dir):
                if entry.is_file() and entry.name.endswith('.txt'):
                    current_files.add(entry.name)
        
        cached_files = set(self._novel_cache.keys())
        
        if current_files == cached_files and self._novel_cache:
            return {'novels': list(self._novel_cache.values())}
        
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
        try:
            name_without_ext = os.path.splitext(entry.name)[0]
            parts = name_without_ext.split('-')
            title = parts[0].strip()
            author = parts[1].strip() if len(parts) > 1 else '未知作者'
            
            progress = self._load_progress()
            novel_progress = progress.get(name_without_ext, {})
            
            return {
                'id': name_without_ext,
                'title': title,
                'author': author,
                'file_path': entry.path,
                'file_size': entry.stat().st_size,
                'chapter_count': 0,
                'last_read_time': novel_progress.get('last_read_time', ''),
                'last_read_chapter': novel_progress.get('last_read_chapter', 0),
                'progress': novel_progress.get('progress', 0.0),
                'encoding': novel_progress.get('encoding', 'auto')  # 用户选择的编码
            }
        except Exception as e:
            print(f"扫描小说失败 {entry.name}: {e}")
            return None
    
    def get_chapters(self, novel_id: str, encoding: str = 'auto') -> Dict[str, Any]:
        """
        获取章节列表
        支持指定编码
        """
        if novel_id in self._chapter_cache:
            return {'chapters': self._chapter_cache[novel_id]}
        
        novel_info = self._novel_cache.get(novel_id)
        if not novel_info:
            return {'error': '小说不存在', 'chapters': []}
        
        try:
            # 使用指定编码或自动检测
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
        """
        获取小说完整内容
        直接加载全部，不做分步
        """
        # 检查缓存
        if novel_id in self._full_content_cache:
            return {'content': self._full_content_cache[novel_id]}
        
        novel_info = self._novel_cache.get(novel_id)
        if not novel_info:
            return {'error': '小说不存在', 'content': ''}
        
        try:
            # 直接读取全部内容
            content = NovelParser.read_full_content(novel_info['file_path'], encoding)
            
            # 缓存
            self._full_content_cache[novel_id] = content
            
            return {'content': content}
        except Exception as e:
            return {'error': f'读取小说失败: {e}', 'content': ''}
    
    def get_content(self, novel_id: str, chapter_index: int, 
                    encoding: str = 'auto') -> Dict[str, Any]:
        """
        获取指定章节内容
        先加载完整内容，然后按章节分割
        """
        # 先获取完整内容
        content_result = self.get_full_content(novel_id, encoding)
        if content_result.get('error'):
            return content_result
        
        full_content = content_result['content']
        
        # 确保章节信息已加载
        if novel_id not in self._offset_cache:
            chapters_result = self.get_chapters(novel_id, encoding)
            if chapters_result.get('error'):
                return {'error': chapters_result['error'], 'content': ''}
        
        offsets = self._offset_cache.get(novel_id)
        if not offsets or chapter_index >= len(offsets):
            return {'error': '章节索引无效', 'content': ''}
        
        # 按偏移分割内容
        start_offset = offsets[chapter_index]
        if chapter_index + 1 < len(offsets):
            end_offset = offsets[chapter_index + 1]
            # 多读取一些，避免截断突兀
            if chapter_index + 2 < len(offsets):
                end_offset = min(end_offset + 200, offsets[chapter_index + 2])
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
            'encoding': encoding  # 保存用户选择的编码
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