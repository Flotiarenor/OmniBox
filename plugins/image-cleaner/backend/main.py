"""image-viewer 的 Companion 插件：全相册重复 / 相似图片清理。

设计参考 docs/plugin-guide.md §2.1 与 docs/image-cleaner-design.md。
该插件不修改 image-viewer 宿主，只通过依赖实例复用其数据根目录与删除能力。
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from shell.backend.media_catalog import IMAGE_EXTENSIONS
from shell.backend.plugin_base import PluginBase

log = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS


class ImageCleanerPlugin(PluginBase):
    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        {"key": "threshold", "label": "相似判定阈值", "type": "range",
         "min": 0, "max": 16, "default": 8,
         "help": "汉明距离越小越严格，0 表示只有完全一致的 dHash 才判为相似"},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        self._host = None
        self._dhash_cache = {}
        self._dhash_cache_file = None

    # ---------- 宿主访问与文件路由复用 ----------

    def _get_host(self):
        if self._host is None:
            self._host = self.get_dependency('image-viewer')
            if self._host is None:
                raise RuntimeError('image-cleaner 需要 image-viewer 插件已加载并声明依赖')
        return self._host

    def get_data_root(self) -> Path:
        return self._get_host().get_data_root()

    def get_file_roots(self) -> List[Path]:
        return self._get_host().get_file_roots()

    @property
    def thumb_dir(self) -> Path:
        return self._get_host().thumb_dir

    def ensure_thumb(self, rel_path: str) -> str:
        return self._get_host().ensure_thumb(rel_path)

    def get_thumb_data(self, rel_path: str):
        return self._get_host().get_thumb_data(rel_path)

    def register_api(self) -> dict:
        return {
            'duplicate_scan': self.duplicate_scan,
            'similar_scan': self.similar_scan,
            'get_cached_scan': self.get_cached_scan,
            'delete_files': self.delete_files,
            'get_status': self.get_status,
        }

    def get_status(self) -> Dict:
        host = self._get_host()
        return {
            'host': host.name,
            'root_dir': str(host.get_data_root()),
            'scope': 'all',
        }

    def get_extensions(self) -> List[dict]:
        """注册到 image-viewer 左侧栏的通用扩展入口。"""
        return [{
            'host': 'image-viewer',
            'id': 'image-cleaner',
            'label': '相册清理',
            'icon': 'icon:brush-cleaning',
            'description': '扫描全部相册中的重复 / 相似图片',
            'section': '相册清理',  # 侧边栏独立分组标题（不与其他扩展挤在一个标题下）
            'embedUrl': '/plugins/image-cleaner/frontend/index.html',
            'placement': 'sidebar',
            'scope': 'all',
        }]

    # ---------- 扫描结果缓存 ----------

    def _scan_cache_path(self) -> Path:
        return self._get_host().get_data_root() / '.cache' / 'image-cleaner' / 'scan_cache.json'

    def _load_scan_cache(self) -> dict:
        try:
            path = self._scan_cache_path()
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_scan_cache(self, cache: dict):
        try:
            path = self._scan_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception as e:
            log.error(f"[{self.name}] 保存扫描结果缓存失败: {e}")

    def _save_scan_result(self, mode: str, groups: list, scanned: int):
        cache = self._load_scan_cache()
        cache[mode] = {
            'groups': groups,
            'scanned': scanned,
            'saved_at': __import__('time').time(),
        }
        self._save_scan_cache(cache)

    def get_cached_scan(self, mode: str) -> Dict:
        """返回上次扫描结果；没有缓存时返回空结果。"""
        if mode not in ('dupe', 'similar'):
            return {'groups': [], 'scanned': 0, 'cached': False}
        cache = self._load_scan_cache()
        item = cache.get(mode)
        if not item or not isinstance(item, dict):
            return {'groups': [], 'scanned': 0, 'cached': False}
        return {
            'groups': item.get('groups', []),
            'scanned': item.get('scanned', 0),
            'cached': True,
        }

    # ---------- 全相册文件收集 ----------

    def _all_album_files(self) -> list:
        """递归扫描全部相册（跳过隐藏目录和 .cache），返回所有图片文件信息。"""
        root = self._get_host().get_data_root()
        files = []
        try:
            if not root.exists() or not root.is_dir():
                return files
            for current, dir_names, filenames in os.walk(root):
                dir_names[:] = [d for d in dir_names
                                if not d.startswith('.') and d != '.cache']
                current_path = Path(current)
                try:
                    rel_dir = current_path.relative_to(root).as_posix()
                except ValueError:
                    continue
                for name in filenames:
                    if name.startswith('.'):
                        continue
                    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
                        continue
                    abs_path = current_path / name
                    try:
                        stat = abs_path.stat()
                    except OSError:
                        continue
                    rel = (Path(rel_dir) / name).as_posix() if rel_dir else name
                    files.append({
                        'rel': rel,
                        'abs': str(abs_path),
                        'size': stat.st_size,
                        'mtime': stat.st_mtime,
                        'album': rel_dir or '',
                    })
        except OSError:
            pass
        return files

    # ---------- 完全重复 ----------

    @staticmethod
    def _file_quick_hash(abs_path: str, size: int) -> Optional[str]:
        """先读首尾各 64KB + 文件大小做快速指纹，避免整文件 MD5。

        读取失败返回 None（而不是"空内容的 md5"，见 _file_full_hash 的说明）。
        """
        h = hashlib.md5()
        h.update(str(size).encode())
        try:
            with open(abs_path, 'rb') as f:
                head = f.read(65536)
                if len(head) == 0 and size > 0:
                    # 声明有内容却读不出任何字节：文件正在被写入/被占用/权限不足
                    return None
                h.update(head)
                if size > 131072:
                    f.seek(max(0, size - 65536))
                    h.update(f.read(65536))
        except OSError:
            return None
        return h.hexdigest()

    @staticmethod
    def _file_full_hash(abs_path: str) -> Optional[str]:
        """整文件 MD5；**任何读取失败都返回 None，绝不返回"空内容的摘要"**。

        历史缺陷：失败时 `pass` 后照样返回 hashlib.md5() 的空摘要
        （d41d8cd98f00b204e9800998ecf8427e），于是所有读不到的文件共享同一个
        digest，被 duplicate_scan 当成"完全重复"分组。而这个插件的下一步是
        delete_files → unlink：一组"假重复"被一键删除 = 真实照片永久丢失。
        文件在扫描期间被写入（同步/复制进行中）是最容易触发的场景。

        额外做一致性校验：读取过程中文件大小变化、或**实际读到的字节数与
        声明大小不符**（正在被写入、被截断），摘要都不可信，同样返回 None。
        """
        h = hashlib.md5()
        read_bytes = 0
        try:
            with open(abs_path, 'rb') as f:
                before = os.fstat(f.fileno()).st_size
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    read_bytes += len(chunk)
                    h.update(chunk)
                after = os.fstat(f.fileno()).st_size
        except OSError:
            return None
        if before != after or read_bytes != before:
            return None
        return h.hexdigest()

    def duplicate_scan(self) -> Dict:
        """扫描全部相册，返回跨相册的完全重复图片分组。"""
        files = self._all_album_files()
        if not files:
            return {'groups': [], 'scanned': 0}

        scanned = len(files)
        by_size = {}
        for f in files:
            by_size.setdefault(f['size'], []).append(f)

        groups = []
        for size, batch in by_size.items():
            if len(batch) < 2:
                continue
            by_quick = {}
            for f in batch:
                key = self._file_quick_hash(f['abs'], f['size'])
                if key is None:
                    continue  # 读不到的文件不参与比较，更不能参与删除
                by_quick.setdefault(key, []).append(f)
            for candidates in by_quick.values():
                if len(candidates) < 2:
                    continue
                by_full = {}
                for f in candidates:
                    digest = self._file_full_hash(f['abs'])
                    if digest is None:
                        continue  # 同上：摘要不可信就退出候选集
                    by_full.setdefault(digest, []).append(f)
                for digest, dups in by_full.items():
                    if len(dups) >= 2:
                        groups.append({
                            'hash': digest,
                            'size': size,
                            'files': [d['rel'] for d in dups],
                        })
        groups.sort(key=lambda g: -len(g['files']))
        self._save_scan_result('dupe', groups, scanned)
        return {'groups': groups, 'scanned': scanned}

    # ---------- 视觉相似 ----------

    def _dhash_cache_path(self) -> Path:
        if self._dhash_cache_file is None:
            self._dhash_cache_file = self._get_host().get_data_root() / '.cache' / 'image-cleaner' / 'dhash.json'
        return self._dhash_cache_file

    def _load_dhash_cache(self):
        if self._dhash_cache:
            return
        try:
            path = self._dhash_cache_path()
            if path.exists():
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._dhash_cache = data
        except Exception:
            self._dhash_cache = {}

    def _save_dhash_cache(self):
        try:
            path = self._dhash_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self._dhash_cache, f, ensure_ascii=False)
        except Exception as e:
            log.error(f"[{self.name}] 保存 dHash 缓存失败: {e}")

    def _image_dhash(self, abs_path: str, mtime: float) -> int:
        """64-bit 差异哈希（dHash），结果写入 image-cleaner 自己的缓存。"""
        key = 'dhash:' + hashlib.md5(abs_path.encode()).hexdigest()
        self._load_dhash_cache()
        cached = self._dhash_cache.get(key)
        if cached and cached.get('mtime') == mtime:
            return int(cached.get('hash', 0))

        value = 0
        try:
            from PIL import Image
            with Image.open(abs_path) as img:
                small = img.convert('L').resize((9, 8))
            pixels = list(small.getdata())
            for row in range(8):
                for col in range(8):
                    value <<= 1
                    if pixels[row * 9 + col] > pixels[row * 9 + col + 1]:
                        value |= 1
        except Exception:
            value = 0
        self._dhash_cache[key] = {'mtime': mtime, 'hash': value}
        return value

    def similar_scan(self, threshold: int | None = None) -> Dict:
        """扫描全部相册，返回跨相册的视觉相似图片分组。"""
        files = self._all_album_files()
        if len(files) < 2:
            return {'groups': [], 'scanned': len(files)}

        if threshold is None:
            threshold = self.setting('threshold', 8)
        threshold = max(0, min(16, int(threshold)))
        hashes = []
        valid = []
        for f in files:
            h = self._image_dhash(f['abs'], f['mtime'])
            if h:
                hashes.append(h)
                valid.append(f)

        n = len(valid)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(n):
            hi = hashes[i]
            for j in range(i + 1, n):
                # 尺寸差异过大不可能是相似图
                if max(valid[i]['size'], valid[j]['size']) > min(valid[i]['size'], valid[j]['size']) * 6:
                    continue
                if (hi ^ hashes[j]).bit_count() <= threshold:
                    union(i, j)

        clusters = {}
        for i in range(n):
            clusters.setdefault(find(i), []).append(valid[i]['rel'])
        groups = [{'files': sorted(files)} for files in clusters.values() if len(files) >= 2]
        groups.sort(key=lambda g: -len(g['files']))
        self._save_dhash_cache()
        self._save_scan_result('similar', groups, n)
        return {'groups': groups, 'scanned': n}

    # ---------- 删除 ----------

    def delete_files(self, rel_paths: List[str]) -> Dict:
        """复用 image-viewer 的删除接口，保持路径安全与宿主缓存清理一致。"""
        return self._get_host().delete_files(rel_paths)
