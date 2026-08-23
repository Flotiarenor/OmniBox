import os
import json
import shutil
from pathlib import Path
from typing import List, Dict
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling



_fs = load_sibling(__file__, 'filesystem', 'image_viewer')
ALLOWED_EXTENSIONS = _fs.ALLOWED_EXTENSIONS
ensure_thumbnail = _fs.ensure_thumbnail
get_image_size = _fs.get_image_size
is_safe_path = _fs.is_safe_path
list_directory = _fs.list_directory
stat_mtime = _fs.stat_mtime

class ImageViewerPlugin(PluginBase):
    settings_schema = [
        {"key": "root_dir", "label": "数据根目录", "type": "text",
         "placeholder": "默认: ./data", "help": "图片浏览的数据根目录"},
        {"key": "row_height", "label": "图片行高", "type": "range",
         "min": 100, "max": 400, "default": 200, "help": "Justified 布局的每行目标高度"},
        {"key": "per_page", "label": "每页图片数", "type": "number",
         "min": 10, "max": 200, "default": 40},
        {"key": "sort_by", "label": "排序方式", "type": "select",
         "default": "mtime",
         "options": [{"label": "修改时间", "value": "mtime"}, {"label": "文件名", "value": "name"}]},
        {"key": "sort_order", "label": "排序方向", "type": "select",
         "default": "desc",
         "options": [{"label": "倒序", "value": "desc"}, {"label": "正序", "value": "asc"}]},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir') or str(super().get_data_root())
        self.root_dir = Path(root).resolve()
        self.cache_dir = self.root_dir / '.cache'
        self.thumb_dir = self.cache_dir / 'thumbs'
        self.meta_file = self.cache_dir / 'image_meta.json'
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.album_cache_file = self.cache_dir / 'albums_index.json'
        self.album_config_file = self.cache_dir / 'albums_config.json'
        self._meta_cache = self._load_meta()
        self._list_cache = {}
        self._album_config = self._load_album_config()
        self._album_cache = self._load_album_cache()

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
        return is_safe_path(self.root_dir, rel_path)


    def register_api(self) -> dict:
        return {
            'list_images': self.list_images,
            'list_dir': self.list_dir,
            'list_albums': self.list_albums,
            'create_folder': self.create_folder,
            'get_album_config': self.get_album_config,
            'set_album_config': self.set_album_config,
            'delete_files': self.delete_files,
            'move_files': self.move_files,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
            'get_root_dir': self.get_root_dir,
            'clear_folder_settings': self.clear_folder_settings,
        }

    def get_root_dir(self) -> str:
        return str(self.root_dir)

    def ensure_thumb(self, rel_path: str) -> str:
        # 供 Shell /thumbs 路由按需调用：缩略图不存在时现场生成。
        if not self._is_safe(rel_path):
            return ''
        try:
            thumb = self._get_thumb(rel_path)
            return str(thumb) if thumb and thumb.exists() else ''
        except Exception:
            return ''

    def _get_dir_mtime(self, rel_path: str) -> float:
        return stat_mtime(self.root_dir, rel_path)


    def _get_image_size(self, abs_path: str, mtime: float) -> tuple:
        return get_image_size(abs_path, mtime, self._meta_cache)


    def _get_thumb(self, rel_path: str) -> Path:
        return ensure_thumbnail(self.root_dir, rel_path, self.thumb_dir)


    def list_images(self, rel_path: str = '', page: int = 1,
                    per_page: int = 40, sort_by: str = 'mtime',
                    sort_order: str = 'desc') -> Dict:
        if not self._is_safe(rel_path):
            return {"images": [], "page": 1, "total": 0, "settings": {}}
        try:
            page = max(1, int(page))
            per_page = max(1, int(per_page))
        except (TypeError, ValueError):
            page, per_page = 1, 40

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
        return list_directory(self.root_dir, rel_path)
    # ===== 相册索引（持久化 + 按目录 mtime 增量更新） =====

    def _load_album_cache(self) -> dict:
        if self.album_cache_file.exists():
            try:
                with open(self.album_cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get('version') == 2:
                    return data
            except Exception:
                pass
        return {'version': 2, 'dirs': {}}

    def _save_album_cache(self):
        try:
            self.album_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.album_cache_file, 'w', encoding='utf-8') as f:
                json.dump(self._album_cache, f, ensure_ascii=False)
        except Exception as e:
            print(f'[ImageViewer] 保存相册索引失败: {e}')

    def _load_album_config(self) -> dict:
        defaults = {'collapsed': [], 'promoted': []}
        if self.album_config_file.exists():
            try:
                with open(self.album_config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {**defaults, **data}
            except Exception:
                pass
        return defaults

    def _save_album_config(self):
        try:
            self.album_config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.album_config_file, 'w', encoding='utf-8') as f:
                json.dump(self._album_config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f'[ImageViewer] 保存相册配置失败: {e}')

    def _list_album_dirs(self) -> dict:
        """只遍历目录树本身（不读文件），返回 {rel_path: dir_mtime}。"""
        dirs = {}
        try:
            stat = self.root_dir.stat()
            dirs[''] = stat.st_mtime
        except OSError:
            return dirs
        for current, dir_names, _files in os.walk(self.root_dir):
            dir_names[:] = [d for d in dir_names
                            if not d.startswith('.') and d != '.cache']
            current = Path(current)
            if current == self.root_dir:
                continue
            try:
                rel = current.relative_to(self.root_dir).as_posix()
                dirs[rel] = current.stat().st_mtime
            except (OSError, ValueError):
                continue
        return dirs

    def _scan_dir_direct(self, dir_path: Path, rel_path: str) -> dict:
        """只扫描一个目录的直接图片（一次 os.scandir，开销可控）。"""
        images = []
        children = []
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    if entry.name.startswith('.') or entry.name == '.cache':
                        continue
                    if entry.is_dir():
                        children.append(entry.name)
                    elif entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                        try:
                            stat = entry.stat()
                        except OSError:
                            continue
                        images.append({
                            'rel': (Path(rel_path) / entry.name).as_posix() if rel_path else entry.name,
                            'mtime': stat.st_mtime,
                        })
        except OSError:
            pass
        images.sort(key=lambda x: x['mtime'], reverse=True)
        return {
            'path': rel_path,
            'name': dir_path.name if rel_path else '未分类',
            'depth': rel_path.count('/') + (1 if rel_path else 0),
            'parent': '/'.join(rel_path.split('/')[:-1]) if rel_path else None,
            'direct_count': len(images),
            'direct_cover': images[0]['rel'] if images else '',
            'direct_mtime': images[0]['mtime'] if images else 0.0,
            'has_children': len(children) > 0,
            'children': sorted(children),
        }

    def _build_albums(self, dirs: dict, cache_dirs: dict) -> tuple:
        """直接扫描变化目录，再自底向上聚合出递归统计。"""
        cache_dirs = cache_dirs or {}
        entries = {}
        changed = 0
        for rel, mtime in dirs.items():
            cached = cache_dirs.get(rel)
            if cached and cached.get('mtime') is not None \
                    and abs(float(cached.get('mtime', 0)) - float(mtime)) < 0.5:
                entries[rel] = cached
                continue
            dir_path = self.root_dir / rel if rel else self.root_dir
            entry = self._scan_dir_direct(dir_path, rel)
            entry['mtime'] = mtime
            entries[rel] = entry
            changed += 1

        # 自底向上聚合 image_count / cover / mtime
        ordered = sorted(entries.values(), key=lambda e: e['depth'], reverse=True)
        totals = {}
        for entry in ordered:
            rel = entry['path']
            total = entry['direct_count']
            cover = entry['direct_cover']
            newest = entry['direct_mtime']
            for child_name in entry['children']:
                child_rel = f"{rel}/{child_name}" if rel else child_name
                child_totals = totals.get(child_rel)
                if not child_totals:
                    continue
                total += child_totals[0]
                if child_totals[2] > newest:
                    newest = child_totals[2]
                    cover = child_totals[1]
            totals[rel] = (total, cover, newest)

        albums = []
        for entry in entries.values():
            total, cover, newest = totals[entry['path']]
            albums.append({
                'name': entry['name'],
                'path': entry['path'],
                'parent': entry['parent'],
                'image_count': total,
                'direct_count': entry['direct_count'],
                'has_children': entry['has_children'],
                'cover': cover,
                'mtime': newest,
                'depth': entry['depth'],
            })

        # 为有封面的相册预生成缩略图（之后 /thumbs 请求直接命中缓存）
        for album in albums:
            if album['cover']:
                try:
                    ensure_thumbnail(self.root_dir, album['cover'], self.thumb_dir)
                except Exception:
                    pass

        return albums, entries, changed

    def list_albums(self) -> Dict:
        """相册列表：目录 mtime 未变时直接复用持久化索引，避免每次重启全量扫描。"""
        dirs = self._list_album_dirs()
        albums, entries, changed = self._build_albums(dirs, self._album_cache.get('dirs', {}))
        self._album_cache = {'version': 2, 'dirs': entries}
        if changed:
            self._save_album_cache()
        return {'albums': albums, 'config': self._album_config, 'changed': changed}

    def get_album_config(self) -> Dict:
        return self._album_config

    def set_album_config(self, rel_path: str, action: str) -> Dict:
        """album 层级控制：collapse/expand（收纳子相册）、promote/unpromote（提升到全部相册）。"""
        rel_path = (rel_path or '').strip().strip('/')
        collapsed = set(self._album_config.get('collapsed', []))
        promoted = set(self._album_config.get('promoted', []))
        if action == 'collapse':
            collapsed.add(rel_path)
        elif action == 'expand':
            collapsed.discard(rel_path)
        elif action == 'promote':
            promoted.add(rel_path)
        elif action == 'unpromote':
            promoted.discard(rel_path)
        else:
            return {'success': False, 'error': f'未知操作: {action}'}
        self._album_config = {
            'collapsed': sorted(collapsed),
            'promoted': sorted(promoted),
        }
        self._save_album_config()
        return {'success': True, 'config': self._album_config}

    def create_folder(self, rel_path: str) -> Dict:
        """在根目录（或指定相对目录）下新建相册文件夹。"""
        rel_path = (rel_path or '').replace('\\', '/').strip('/')
        if not rel_path or not self._is_safe(rel_path):
            return {'success': False, 'error': '文件夹名称非法'}
        target = self.root_dir / rel_path
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'path': rel_path}


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
        folders = self.setting('folders') or {}
        if not isinstance(folders, dict):
            folders = {}
        global_settings = folders.get('__global__', {})
        hard_defaults = {
            "row_height": 200,
            "per_page": 40,
            "sort_by": "mtime",
            "sort_order": "desc"
        }
        key = rel_path or '__global__'
        folder_settings = folders.get(key, {})
        result = {**hard_defaults, **global_settings, **folder_settings}
        result['root_dir'] = str(self.root_dir)
        return result

    def save_settings(self, rel_path='', settings=None) -> Dict:
        """兼容两种调用。全局设置走 super()；per-folder 设置只写入 folders，避免污染全局。"""
        if settings is None:
            settings = rel_path or {}
            rel_path = ''
        result = {"success": True}
        key = rel_path or '__global__'
        if not rel_path:
            # 标准字段走 SettingsStore，触发 on_settings_changed
            result = super().save_settings(settings)
        folders = self.setting('folders') or {}
        if not isinstance(folders, dict):
            folders = {}
        folders[key] = settings
        self.update_setting('folders', folders)
        return result

    def on_settings_changed(self, changed_keys):
        if 'root_dir' in changed_keys:
            new_dir = self.setting('root_dir')
            if new_dir and Path(new_dir).is_dir():
                self.root_dir = Path(new_dir).resolve()
                self.cache_dir = self.root_dir / '.cache'
                self.thumb_dir = self.cache_dir / 'thumbs'
                self.meta_file = self.cache_dir / 'image_meta.json'
                self.thumb_dir.mkdir(parents=True, exist_ok=True)
                self._meta_cache = self._load_meta()
                self._list_cache.clear()

    def clear_folder_settings(self, rel_path: str) -> Dict:
        """删除指定文件夹的独立设置，使其回退到全局设置"""
        folders = self.setting('folders') or {}
        if isinstance(folders, dict) and rel_path in folders:
            del folders[rel_path]
            self.update_setting('folders', folders)
        return {"success": True}
