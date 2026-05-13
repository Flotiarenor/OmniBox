# backend/api.py
from backend.modules.file_module import FileModule
from backend.modules.image_module import ImageModule
from backend.modules.manga_module import MangaModule
from backend.modules.settings_module import SettingsModule
from backend.modules.download_module import DownloadModule
from backend.modules.novel_module import NovelModule


class AppAPI:
    def __init__(self, image_dir: str, manga_dir: str, novel_dir: str):
        """初始化所有模块"""
        self.file_module = FileModule(image_dir)
        self.image_module = ImageModule(image_dir)
        self.manga_module = MangaModule(manga_dir)
        self.settings_module = SettingsModule()
        self.download_module = DownloadModule(manga_dir)
        self.novel_module = NovelModule(novel_dir)
    
    # ===== 文件操作 =====
    def file_list_dir(self, path=''):
        return self.file_module.list_dir(path)
    
    def file_delete(self, paths):
        return self.file_module.delete(paths)
    
    def file_move(self, paths, dest):
        return self.file_module.move(paths, dest)
    
    # ===== 图片操作 =====
    def image_list(self, path='', page=1, per_page=None,
                   sort_by=None, sort_order=None):
        settings = self.settings_module.get(path)
        if per_page is None:
            per_page = settings.get('per_page', 40)
        if sort_by is None:
            sort_by = settings.get('sort_by', 'mtime')
        if sort_order is None:
            sort_order = settings.get('sort_order', 'desc')
        result = self.image_module.list_images(
            path, page, per_page, sort_by, sort_order
        )
        result['settings'] = settings
        return result
    
    # ===== 漫画操作 =====
    def manga_list(self):
        return self.manga_module.list_manga()
    
    def manga_search(self, keyword):
        return self.manga_module.search(keyword)
    
    def manga_get_state(self):
        return self.manga_module.get_state()
    
    def manga_toggle_favorite(self, folder_name):
        return self.manga_module.toggle_favorite(folder_name)
    
    def manga_update_recent(self, folder_name, page=0):
        return self.manga_module.update_recent(folder_name, page)
    
    def manga_get_detail(self, folder_name):
        return self.manga_module.get_detail(folder_name)
    
    def manga_get_pages(self, folder_name, chapter_path=""):
        return self.manga_module.get_pages(folder_name, chapter_path)
    
    # ===== 设置操作 =====
    def settings_get(self, path=''):
        return self.settings_module.get(path)
    
    def settings_save(self, path, settings):
        return self.settings_module.save(path, settings)
    
    # ===== 下载中心操作 =====
    def download_submit(self, album_id):
        return self.download_module.submit(album_id)
    
    def download_list(self):
        return self.download_module.get_summary()
    
    def download_get_album_info(self, album_id):
        import os
        import json
        
        info_path = os.path.join(self.download_module.manga_dir, album_id, "album_info.json")
        if not os.path.exists(info_path):
            return {'error': '未找到漫画信息文件', 'exists': False}
        
        try:
            with open(info_path, 'r', encoding='utf-8') as f:
                info = json.load(f)
            info['exists'] = True
            return info
        except Exception as e:
            return {'error': f'读取漫画信息失败: {e}', 'exists': False}
    
    def dialog_select_directory(self):
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        directory = filedialog.askdirectory(title='选择下载目录')
        root.destroy()
        return directory if directory else ''
    
    # ===== 小说操作 =====
    def novel_list(self):
        """获取小说列表（只返回元信息，不加载内容）"""
        return self.novel_module.list_novels()
    
    def novel_get_chapters(self, novel_id: str):
        """获取章节列表"""
        return self.novel_module.get_chapters(novel_id)
    def novel_get_content(self, novel_id: str, chapter_index: int, encoding: str = 'auto'):
        """获取章节内容，支持指定编码"""
        return self.novel_module.get_content(novel_id, chapter_index, encoding)
    def novel_update_progress(self, novel_id: str, chapter_index: int, 
                            scroll_position: float = 0.0, encoding: str = 'auto'):
        """更新阅读进度，保存编码设置"""
        return self.novel_module.update_progress(novel_id, chapter_index, scroll_position, encoding)