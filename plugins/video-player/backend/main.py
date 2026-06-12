import os
import json
from pathlib import Path
from typing import List, Dict
from shell.backend.plugin_base import PluginBase

# 支持的媒体扩展名
ALLOWED_EXTENSIONS = {
    '.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.wmv',
    '.mp3', '.flac', '.wav', '.aac', '.ogg', '.wma'
}

class VideoPlayerPlugin(PluginBase):
    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self.global_data_root = Path(config['directories']['data_root']).resolve()
        self.settings_file = Path(__file__).parent.parent / 'settings.json'
        self._settings = self._load_settings()
        # 自定义根目录（优先使用设置中的 root_dir）
        self.root_dir = Path(self._settings.get('root_dir', str(self.global_data_root))).resolve()

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
            print(f"[VideoPlayer] 保存设置失败: {e}")

    def get_data_root(self) -> Path:
        """重写以支持自定义根目录"""
        return self.root_dir

    def _is_safe(self, rel_path: str) -> bool:
        """路径穿越检查"""
        try:
            target = (self.root_dir / rel_path).resolve()
            return str(target).startswith(str(self.root_dir))
        except Exception:
            return False

    def register_api(self) -> dict:
        return {
            'list_dir': self.list_dir,
            'list_media': self.list_media,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
            'get_root_dir': self.get_root_dir,
        }

    def get_root_dir(self) -> str:
        return str(self.root_dir)

    def list_dir(self, rel_path: str = '') -> List[Dict]:
        """列出子目录（仅文件夹，排除隐藏和缓存目录）"""
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
        children.sort(key=lambda x: x['name'].lower())
        return children

    def list_media(self, rel_path: str = '') -> Dict:
        """列出当前目录下的媒体文件"""
        if not self._is_safe(rel_path):
            return {"files": [], "path": rel_path}
        target = self.root_dir / rel_path
        if not target.exists() or not target.is_dir():
            return {"files": [], "path": rel_path}
        files = []
        try:
            for entry in os.scandir(target):
                if entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                    stat = entry.stat()
                    files.append({
                        "name": entry.name,
                        "path": (Path(rel_path) / entry.name).as_posix(),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime
                    })
        except PermissionError:
            pass
        # 默认按文件名排序
        files.sort(key=lambda x: x['name'].lower())
        return {"files": files, "path": rel_path}

    def get_settings(self) -> Dict:
        """获取当前设置（仅全局根目录）"""
        return {"root_dir": str(self.root_dir)}

    def save_settings(self, settings: Dict) -> Dict:
        """保存设置，支持修改根目录"""
        new_root = settings.get('root_dir')
        if new_root and Path(new_root).is_dir():
            self._settings['root_dir'] = new_root
            self.root_dir = Path(new_root).resolve()
            self._save_settings_to_file()
            return {"success": True}
        return {"success": False, "error": "无效的根目录路径"}
