"""尺寸元数据与缩略图字节。

状态自持：尺寸元数据缓存、脏标记、锁、缩略图库（`ThumbCache`）都在这里。落盘时机由
`flush()` 统一 —— 列表、灯箱序列、重建、刷新钩子都调它，而不是各自记住"该存盘了"。

**为什么锁在这里**：尺寸填充是多线程的（`fill_image_sizes` 开 8 个线程读 Pillow），
而落盘发生在请求线程或后台任务线程上。旧实现里 `_meta_dirty` 与 `_meta_cache` 是入口
类上的裸属性，任何分片都能直接改，没有一处明确的读写所有权。现在写路径只有本组件的
几个方法，锁只保护"落盘"这一段（读尺寸时对 dict 的写依赖 GIL，与旧实现一致）。

**写盘走共享基建**：`shell/backend/json_cache.py` 的临时文件 + `os.replace`，进程被杀
不会留下半截 JSON。旧实现的 `image_meta.json` 是直接 `open(w)`，同目录的
`dirs_index.json` 却是原子写 —— 同一插件里两种口径，前者损坏后的表现只是"打开变慢"。

缩略图字节走 `ThumbCache`（SQLite）：`/thumbs` 路由每次请求调 `get_thumb_data`，
命中库就直接返回字节；库外的散文件目录只剩"旧版兼容入口"`ensure_thumb`。
"""

import hashlib
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.json_cache import read_json, write_json
from shell.backend.thumb_cache import ThumbCache

log = logging.getLogger(__name__)


def _meta_key(abs_path: str) -> str:
    """尺寸元数据的键：物理路径的 md5（换根后同一文件的键跟着路径变）。"""
    return hashlib.md5(abs_path.encode()).hexdigest()


def _read_image_size(abs_path: str, mtime: float, meta_cache: dict) -> Tuple[int, int]:
    """从元数据缓存读取图片尺寸，未命中时用 Pillow 读取并回写缓存。

    读取失败时不写缓存：临时不可读（文件被占用/写入中）的文件不会被永久缓存成
    0×0，下次扫描会重试。
    """
    key = _meta_key(abs_path)
    if key in meta_cache:
        data = meta_cache[key]
        if data.get('mtime') == mtime:
            return data.get('width', 0), data.get('height', 0)
    try:
        from PIL import Image
        with Image.open(abs_path) as img:
            width, height = img.size
    except Exception:
        return 0, 0
    meta_cache[key] = {'mtime': mtime, 'width': width, 'height': height}
    return width, height


def _stat_mtime(root: Path, rel_path: str) -> float:
    try:
        return os.stat(root / rel_path).st_mtime
    except Exception:
        return 0.0


def _write_file_thumb(root: Path, rel_path: str, thumb_dir: Path) -> Path:
    """旧版文件式缩略图入口的实现：目标不存在时现场生成一张。

    保留给其他兼容代码使用（如 image-cleaner 代理宿主入口）；新代码请用 `ThumbCache`。
    """
    thumb_path = thumb_dir / rel_path
    if thumb_path.exists():
        return thumb_path
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image
        img = Image.open(root / rel_path)
        img.thumbnail((300, 300))
        img.save(thumb_path)
    except Exception:
        try:
            shutil.copy(root / rel_path, thumb_path)
        except Exception:
            pass
    return thumb_path


class ThumbStore:
    """尺寸元数据缓存、缩略图字节与落盘时机。"""

    def __init__(self, paths: Any, scan_workers: int) -> None:
        self._paths = paths
        self._scan_workers = scan_workers
        self._lock = threading.Lock()
        self._cache = ThumbCache(paths.thumb_db_path)
        self._meta: Dict[str, Any] = read_json(paths.meta_file, default={}) or {}
        self._dirty = False
        #: 强制重建的单调版本号（毫秒）：见 `bump_rebuild_version`
        self._rebuild_version = 0

    # ===== 生命周期 =====

    def rebind(self) -> None:
        """换根后重开缩略图库并重读元数据（由入口编排调用）。"""
        self._cache = ThumbCache(self._paths.thumb_db_path)
        self._meta = read_json(self._paths.meta_file, default={}) or {}
        self._dirty = False

    @property
    def cache(self) -> ThumbCache:
        """缩略图库本体（重建任务与刷新钩子需要它做批量操作）。"""
        return self._cache

    @property
    def meta_entries(self) -> int:
        """诊断用：尺寸元数据条目数。"""
        return len(self._meta)

    # ===== 尺寸元数据 =====

    def get_image_size(self, abs_path: str, mtime: float) -> Tuple[int, int]:
        """读单张图片的尺寸（命中元数据缓存时无 I/O）。"""
        before = len(self._meta)
        result = _read_image_size(abs_path, mtime, self._meta)
        if len(self._meta) != before:
            # 新增了尺寸缓存条目才需要落盘，避免每次列表都全量重写 meta 文件
            self._dirty = True
        return result

    def fill_image_sizes(self, images: List[dict]) -> None:
        """并行填充 images 列表的 width/height（要求条目含 path/mtime 字段）。

        首次扫描大文件夹时 Pillow 打开文件读尺寸是主要开销，多线程可把 4 万张图片的
        扫描从 ~5s 降到 ~1.5s。
        """
        if not images:
            return
        with ThreadPoolExecutor(max_workers=self._scan_workers) as executor:
            futures = [executor.submit(self.get_image_size, item['path'], item['mtime'])
                       for item in images]
            for item, future in zip(images, futures, strict=True):
                item['width'], item['height'] = future.result()

    def dir_mtime(self, rel_path: str) -> float:
        """目录的 mtime（列表缓存的失效判据）；取不到返回 0。"""
        root, inner = self._paths.split_virtual(rel_path)
        if root is None:
            return 0.0
        return _stat_mtime(root, inner)

    def drop(self, abs_path: Any) -> None:
        """丢掉某张图片的尺寸元数据（文件被删除/移动/替换时调用）。"""
        self._meta.pop(_meta_key(str(abs_path)), None)
        self._dirty = True

    def mark_dirty(self) -> None:
        """标记尺寸元数据需要落盘（调用方改过缓存内容但没有新增条目时用）。"""
        self._dirty = True

    def flush(self) -> None:
        """把新增的尺寸元数据落盘一次（列表/重建/刷新钩子的收尾调用）。"""
        if not self._dirty:
            return
        with self._lock:
            if not self._dirty:
                return
            if not write_json(self._paths.meta_file, self._meta, indent=2):
                log.error('[ImageViewer] 保存元数据失败: %s', self._paths.meta_file)
                return
            self._dirty = False

    def save_meta(self) -> None:
        """强制落盘一次（定向重建缩略图后用，此时确实改过缓存内容）。"""
        with self._lock:
            if not write_json(self._paths.meta_file, self._meta, indent=2):
                log.error('[ImageViewer] 保存元数据失败: %s', self._paths.meta_file)
                return
            self._dirty = False

    def bump_rebuild_version(self) -> int:
        """领一个强制重建的单调版本号（毫秒），同一次重建的条目共用一个。

        为什么需要：强制重建只重写缩略图字节、不动源文件 mtime。前端把 mtime 当缩略图
        URL 的版本号，于是"重建了缩略图，URL 不变"，壳的 `/thumbs` 一天强缓存让界面
        继续显示旧图。这个版本号随重建结果交给前端，拼进 URL 才算真的换了一版。

        单调性用「毫秒时间戳与上次+1 取大」保证：同一毫秒内连续两次重建也必须给出不同
        的值，否则那条回归用例（同 mtime 重建两次 → 版本号不同）会偶发失败。
        """
        with self._lock:
            self._rebuild_version = max(int(time.time() * 1000),
                                        self._rebuild_version + 1)
            return self._rebuild_version

    def clear_meta(self) -> None:
        """丢掉尺寸元数据（内存 + 落盘文件），缩略图库不动。"""
        self._meta = {}
        self._dirty = False
        try:
            if self._paths.meta_file.exists():
                self._paths.meta_file.unlink()
        except OSError as exc:
            log.warning('[ImageViewer] 删除尺寸元数据失败: %s', exc)

    def clear(self) -> None:
        """清空尺寸元数据与缩略图库（统一刷新的"重建派生缓存"逃生门用）。"""
        self._meta = {}
        self._dirty = False
        try:
            if self._paths.meta_file.exists():
                self._paths.meta_file.unlink()
        except OSError as exc:
            log.warning('[ImageViewer] 删除尺寸元数据失败: %s', exc)
        try:
            self._cache.clear()
        except Exception as exc:
            log.warning('[ImageViewer] 清空缩略图库失败: %s', exc)

    # ===== 缩略图 =====

    def get_thumb_data(self, rel_path: str) -> Optional[Tuple[bytes, str]]:
        """供壳 `/thumbs` 路由使用：从 SQLite（`ThumbCache`）读取/生成缩略图字节。"""
        if not self._paths.is_safe(rel_path):
            return None
        try:
            abs_path, _ = self._paths.resolve_path(rel_path)
            if abs_path is None:
                return None
            return self._cache.get(rel_path, abs_path)
        except Exception:
            return None

    def ensure_thumb(self, rel_path: str) -> None:
        """旧版文件式入口：散文件缩略图不存在时现场生成一张。

        只为其他兼容代码活着一处（旧壳的 `/thumbs` 兜底与附属插件代理）；新路由优先走
        `get_thumb_data`。返回 `None` 与基类契约一致 —— 调用方按"文件是否出现"判断结果。
        """
        if not self._paths.is_safe(rel_path):
            return
        try:
            root, inner = self._paths.split_virtual(rel_path)
            base = root if root is not None else self._paths.root
            _write_file_thumb(base, inner, self._paths.thumb_dir)
        except Exception:
            return

    def get_image_info(self, rel_path: str) -> Dict:
        """返回单张图片的存储大小与分辨率，供全屏查看器右侧信息面板使用。"""
        if not self._paths.is_safe(rel_path):
            return {'success': False, 'error': '非法路径'}
        abs_path, _ = self._paths.resolve_path(rel_path)
        if abs_path is None:
            return {'success': False, 'error': '非法路径'}
        try:
            if not abs_path.is_file():
                return {'success': False, 'error': '不是图片文件'}
            stat = abs_path.stat()
            width, height = self.get_image_size(str(abs_path), stat.st_mtime)
            return {
                'success': True,
                'rel_path': rel_path,
                'size': stat.st_size,
                'width': width,
                'height': height,
            }
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

    def delete_key(self, rel_path: str) -> None:
        """删掉某个虚拟路径的缩略图行（文件删除/移动后调用）。"""
        self._cache.delete(rel_path)

    def prune_keys(self, valid_keys: set) -> int:
        """按有效键集合对账缩略图库的孤儿行，返回删除行数。"""
        return self._cache.prune(valid_keys)
