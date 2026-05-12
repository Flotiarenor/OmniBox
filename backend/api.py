from backend.modules.file_module import FileModule
from backend.modules.image_module import ImageModule
from backend.modules.settings_module import SettingsModule


class AppAPI:
    def __init__(self, image_dir: str):
        self.file_module = FileModule(image_dir)
        self.image_module = ImageModule(image_dir)
        self.settings_module = SettingsModule()

    # ===== 文件操作 =====
    def file_list_dir(self, path=''):
        return self.file_module.list_dir(path)

    def file_delete(self, paths):
        return self.file_module.delete(paths)

    def file_move(self, paths, dest):
        return self.file_module.move(paths, dest)

    # ===== 图片操作 =====
    def image_list(self, path='', page=1, per_page=None, sort_by=None, sort_order=None):
        settings = self.settings_module.get(path)
        if per_page is None:
            per_page = settings.get('per_page', 40)
        if sort_by is None:
            sort_by = settings.get('sort_by', 'mtime')
        if sort_order is None:
            sort_order = settings.get('sort_order', 'desc')

        result = self.image_module.list_images(path, page, per_page, sort_by, sort_order)
        result['settings'] = settings
        return result

    # ===== 设置操作 =====
    def settings_get(self, path=''):
        return self.settings_module.get(path)

    def settings_save(self, path, settings):
        return self.settings_module.save(path, settings)