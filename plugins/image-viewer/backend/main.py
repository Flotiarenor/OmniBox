import os
import json
import shutil
import hashlib
from pathlib import Path
from typing import List, Dict
from shell.backend.plugin_base import PluginBase

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'}

class ImageViewerPlugin(PluginBase):
    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self.global_data_root = Path(config['directories']['data_root']).resolve()
        self.settings_file = Path(__file__).parent.parent / 'settings.json'
        self._settings = self._load_settings()
        self.root_dir = Path(self._settings.get('root_dir', str(self.global_data_root))).resolve()
        self.cache_dir = self.root_dir / '.cache'
        self.thumb_dir = self.cache_dir / 'thumbs'
        self.meta_file = self.cache_dir / 'image_meta.json'
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self._meta_cache = self._load_meta()
        self._list_cache = {}

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
            print(f"[ImageViewer] 保存设置失败: {e}")

    def _load_meta(self) -> dict:
        if self.meta_file.exists():
            try:
                with open(self.meta_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_meta(self):
        try:
            with open(self.meta_file, 'w', encoding='utf-8') as f:
                json.dump(self._meta_cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[ImageViewer] 保存元数据失败: {e}")

    def get_data_root(self) -> Path:
        return self.root_dir

    def _is_safe(self, rel_path: str) -> bool:
        try:
            target = (self.root_dir / rel_path).resolve()
            return str(target).startswith(str(self.root_dir))
        except Exception:
            return False

    def register_api(self) -> dict:
        return {
            'list_images': self.list_images,
            'list_dir': self.list_dir,
            'delete_files': self.delete_files,
            'move_files': self.move_files,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
            'get_root_dir': self.get_root_dir,
            'clear_folder_settings': self.clear_folder_settings,
        }

    def get_root_dir(self) -> str:
        return str(self.root_dir)

    def _get_dir_mtime(self, rel_path: str) -> float:
        try:
            return os.stat(self.root_dir / rel_path).st_mtime
        except:
            return 0

    def _get_image_size(self, abs_path: str, mtime: float) -> tuple:
        key = hashlib.md5(abs_path.encode()).hexdigest()
        if key in self._meta_cache:
            data = self._meta_cache[key]
            if data.get('mtime') == mtime:
                return data.get('width', 0), data.get('height', 0)
        width, height = 0, 0
        try:
            from PIL import Image
            with Image.open(abs_path) as img:
                width, height = img.size
        except Exception:
            pass
        self._meta_cache[key] = {'mtime': mtime, 'width': width, 'height': height}
        return width, height

    def _get_thumb(self, rel_path: str) -> Path:
        thumb_path = self.thumb_dir / rel_path
        if thumb_path.exists():
            return thumb_path
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image
            img = Image.open(self.root_dir / rel_path)
            img.thumbnail((300, 300))
            img.save(thumb_path)
        except Exception:
            shutil.copy(self.root_dir / rel_path, thumb_path)
        return thumb_path

    def list_images(self, rel_path: str = '', page: int = 1,
                    per_page: int = 40, sort_by: str = 'mtime',
                    sort_order: str = 'desc') -> Dict:
        if not self._is_safe(rel_path):
            return {"images": [], "page": 1, "total": 0, "settings": {}}

        cache_key = (rel_path, sort_by, sort_order)
        dir_mtime = self._get_dir_mtime(rel_path)

        if cache_key in self._list_cache:
            cached_mtime, cached_images = self._list_cache[cache_key]
            if cached_mtime == dir_mtime:
                total = len(cached_images)
                start = (page - 1) * per_page
                end = start + per_page
                return {
                    "images": cached_images[start:end],
                    "page": page,
                    "total": total,
                    "has_next": end < total,
                    "has_prev": page > 1,
                    "settings": self.get_settings(rel_path)
                }

        target_dir = self.root_dir / rel_path
        images = []
        try:
            with os.scandir(target_dir) as entries:
                for entry in entries:
                    if entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                        mtime = entry.stat().st_mtime
                        url_path = (Path(rel_path) / entry.name).as_posix()
                        width, height = self._get_image_size(entry.path, mtime)
                        self._get_thumb(url_path)
                        images.append({
                            'url': url_path,
                            'mtime': mtime,
                            'width': width,
                            'height': height
                        })
        except FileNotFoundError:
            pass

        reverse = (sort_order == 'desc')
        if sort_by == 'name':
            images.sort(key=lambda x: x['url'].lower(), reverse=reverse)
        else:
            images.sort(key=lambda x: x['mtime'], reverse=reverse)

        self._list_cache[cache_key] = (dir_mtime, images)
        self._save_meta()

        total = len(images)
        start = (page - 1) * per_page
        end = start + per_page
        return {
            "images": images[start:end],
            "page": page,
            "total": total,
            "has_next": end < total,
            "has_prev": page > 1,
            "settings": self.get_settings(rel_path)
        }

    def list_dir(self, rel_path: str = '') -> List[Dict]:
        if not self._is_safe(rel_path):
            return []
        target = self.root_dir / rel_path
        if not target.exists() or not target.is_dir():
            return []
        children = []
        try:
            for entry in os.scandir(target):
                if entry.is_dir() and not entry.name.startswith('.') and entry.name != '.cache':
                    child_path = (Path(rel_path) / entry.name).as_posix()
                    children.append({"name": entry.name, "path": child_path})
        except PermissionError:
            pass
        children.sort(key=lambda x: x['name'])
        return children

    def delete_files(self, rel_paths: List[str]) -> Dict:
        deleted, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f"非法路径: {rel}")
                continue
            abs_path = self.root_dir / rel
            try:
                if abs_path.exists():
                    abs_path.unlink()
                    deleted.append(rel)
            except Exception as e:
                errors.append(f"删除失败 {rel}: {str(e)}")
        self._list_cache.clear()
        return {"deleted": deleted, "errors": errors}

    def move_files(self, rel_paths: List[str], dest_rel: str) -> Dict:
        if not self._is_safe(dest_rel):
            return {"moved": [], "errors": ["目标目录非法"]}
        dest_dir = self.root_dir / dest_rel
        if not dest_dir.is_dir():
            return {"moved": [], "errors": ["目标目录不存在"]}
        moved, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f"非法源路径: {rel}")
                continue
            src = self.root_dir / rel
            try:
                if src.exists():
                    dest_file = dest_dir / src.name
                    if dest_file.exists() and src != dest_file:
                        stem, suffix = dest_file.stem, dest_file.suffix
                        counter = 1
                        while dest_file.exists():
                            dest_file = dest_dir / f"{stem}_{counter}{suffix}"
                            counter += 1
                    shutil.move(str(src), str(dest_file))
                    moved.append(rel)
            except Exception as e:
                errors.append(f"移动失败 {rel}: {str(e)}")
        self._list_cache.clear()
        return {"moved": moved, "errors": errors}

    def get_settings(self, rel_path: str = '') -> Dict:
        folders = self._settings.get('folders', {})
        global_settings = folders.get('__global__', {})
        hard_defaults = {
            "row_height": 200,
            "per_page": 40,
            "sort_by": "mtime",
            "sort_order": "desc"
        }
        key = rel_path or '__global__'
        folder_settings = folders.get(key, {})
        return {**hard_defaults, **global_settings, **folder_settings}

    def save_settings(self, rel_path: str, settings: Dict) -> Dict:
        if 'folders' not in self._settings:
            self._settings['folders'] = {}
        key = rel_path or '__global__'
        root_dir = settings.pop('root_dir', None)
        self._settings['folders'][key] = settings
        if root_dir is not None and root_dir and Path(root_dir).is_dir():
            self._settings['root_dir'] = root_dir
            self.root_dir = Path(root_dir).resolve()
            self.cache_dir = self.root_dir / '.cache'
            self.thumb_dir = self.cache_dir / 'thumbs'
            self.meta_file = self.cache_dir / 'image_meta.json'
            self.thumb_dir.mkdir(parents=True, exist_ok=True)
            self._meta_cache = self._load_meta()
            self._list_cache.clear()
        self._save_settings_to_file()
        return {"success": True}

    def clear_folder_settings(self, rel_path: str) -> Dict:
        """删除指定文件夹的独立设置，使其回退到全局设置"""
        if 'folders' in self._settings and rel_path in self._settings['folders']:
            del self._settings['folders'][rel_path]
            self._save_settings_to_file()
        return {"success": True}
