# backend/api.py
from backend.modules.file_module import FileModule
from backend.modules.image_module import ImageModule
from backend.modules.manga_module import MangaModule
from backend.modules.settings_module import SettingsModule
from backend.modules.download_module import DownloadModule


class AppAPI:
    def __init__(self, image_dir: str, manga_dir: str):
        self.file_module = FileModule(image_dir)
        self.image_module = ImageModule(image_dir)
        self.manga_module = MangaModule(manga_dir)
        self.settings_module = SettingsModule()
        self.download_module = DownloadModule(manga_dir)

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

    # ===== 下载中心操作（精简后） =====
    def download_submit(self, album_id):
        """
        提交下载任务
        """
        return self.download_module.submit(album_id)

    def download_list(self):
        """
        获取下载列表（只读）
        返回当前所有下载任务的状态摘要
        """
        return self.download_module.get_summary()

    def download_get_album_info(self, album_id):
        """
        获取已下载漫画的简介信息
        """
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
        """打开系统目录选择对话框"""
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        directory = filedialog.askdirectory(title='选择下载目录')
        root.destroy()
        return directory if directory else ''