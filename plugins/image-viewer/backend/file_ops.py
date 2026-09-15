"""目录 / 文件增删改与刷新（ImageViewerPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 ImageViewerPlugin.__init__ 持有 ——
分片只把方法挂到同一个类上，因此方法与调用点都没有变。
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'image_viewer')
ALLOWED_EXTENSIONS = _common.ALLOWED_EXTENSIONS
drop_image_meta = _common.drop_image_meta

class FileOpsMixin:
    """目录 / 文件增删改与刷新。"""

    def create_folder(self, rel_path: str) -> Dict:
        """在根目录（或指定相对目录）下新建相册文件夹。

        没有直接图片也没有下级图片的空目录默认不显示（见 `visible_empty_dirs`），
        这里把新建的目录记进「保留可见」标记，避免刚建完就消失；等里面进了图片
        （`image_count > 0`）或目录被删除时标记自动清理。
        """
        rel_path = (rel_path or '').replace('\\', '/').strip('/')
        if not rel_path or not self._is_safe(rel_path):
            return {'success': False, 'error': '文件夹名称非法'}
        if self._in_namespace(rel_path):
            return {'success': False, 'error': '不能在根目录节点下创建文件夹'}
        target, _ = self._resolve_dir(rel_path)
        if target is None:
            return {'success': False, 'error': '文件夹名称非法'}
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {'success': False, 'error': str(e)}
        self._mark_visible(rel_path)
        self._invalidate_albums_cache()
        return {'success': True, 'path': rel_path}

    def delete_folder(self, rel_path: str) -> Dict:
        """删除一个**空**目录（含只剩空子目录的情况），并清掉它的可见标记。

        只允许删除「递归都没有图片」的目录：有图片的目录必须先清空图片，
        避免一个误点连带删掉整棵作品树。

        路径必须落在它所属的根目录内：`../`、绝对路径、未知命名空间一律拒绝。
        这是**删除**操作，越权代价是根目录外的整棵目录树，所以不能只依赖调用方
        传对相对路径（`_is_safe` 是唯一把"虚拟相对路径"折算成根内物理路径的校验）。
        """
        rel_path = (rel_path or '').replace('\\', '/').strip('/')
        # 两个校验缺一不可：
        #   - `_is_safe` 拒绝未知命名空间，以及任何跑出所属根的路径（`../victim`、
        #     绝对路径 —— `root / '/abs'` 在 pathlib 里会丢弃 root，必须拦）；
        #   - 命名空间节点本身是**虚拟**节点（没有自己的物理目录），而 `_is_safe`
        #     对它是**通过**的（根内相对路径为空），所以必须单独拒绝。
        # 与 create_folder / move_files 的写法保持一致。
        if not rel_path or not self._is_safe(rel_path) or self._is_namespace_node(rel_path):
            return {'success': False, 'error': '路径非法'}
        target, _ = self._resolve_dir(rel_path)
        if target is None:
            return {'success': False, 'error': '路径非法'}
        if not target.is_dir():
            return {'success': False, 'error': '目录不存在'}
        root, _inner = self._split_virtual(rel_path)
        try:
            for _current, _dirs, files in os.walk(target):
                if any(not f.startswith('.') and Path(f).suffix.lower() in ALLOWED_EXTENSIONS
                       for f in files):
                    return {'success': False, 'error': '目录内还有图片，请先删除图片'}
            # 二次校验：`os.walk` 到 `rmtree` 之间路径可能被换成指向根外的符号链接 /
            # junction（TOCTOU），删除前用 resolve() 后的物理路径再确认一次。
            if root is None or not target.resolve().is_relative_to(root):
                return {'success': False, 'error': '路径非法'}
            shutil.rmtree(target)
        except Exception as e:
            return {'success': False, 'error': str(e)}
        marks = [p for p in (self._album_config.get('visible_empty_dirs') or [])
                 if p != rel_path and not p.startswith(f'{rel_path}/')]
        self._album_config = {**self._album_config, 'visible_empty_dirs': marks}
        self._save_album_config()
        self._invalidate_albums_cache()
        return {'success': True}

    def _mark_visible(self, rel_path: str):
        """把新建的空目录加入「保留可见」标记（同时保留其上级，便于逐层进入）。"""
        current = set(self._album_config.get('visible_empty_dirs') or [])
        parts = [p for p in (rel_path or '').split('/') if p]
        for i in range(1, len(parts) + 1):
            current.add('/'.join(parts[:i]))
        self._album_config = {**self._album_config, 'visible_empty_dirs': sorted(current)}
        self._save_album_config()

    def _set_visible_marks(self, marks: List[str]) -> Dict:
        """覆盖式写入「保留可见」标记（设置页/清理时用）。"""
        cleaned = sorted({p.strip('/') for p in (marks or []) if str(p).strip('/')})
        self._album_config = {**self._album_config, 'visible_empty_dirs': cleaned}
        self._save_album_config()
        self._invalidate_albums_cache()
        return {'success': True, 'config': self._album_config}

    def _prune_visible_marks(self) -> bool:
        """清理已消失的「保留可见」标记，避免配置文件无限增长。

        返回是否真的做了清理：调用方据此决定是否丢弃相册索引缓存
        （标记影响的是前端的空目录过滤，与封面聚合无关，但仍让索引重来更简单）。
        """
        marks = self._album_config.get('visible_empty_dirs') or []
        if not marks:
            return False
        kept = [p for p in marks if self._virtual_dir_exists(p)]
        if kept == list(marks):
            return False
        self._album_config = {**self._album_config, 'visible_empty_dirs': kept}
        self._save_album_config()
        return True

    def delete_files(self, rel_paths: List[str]) -> Dict:
        deleted, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f"非法路径: {rel}")
                continue
            abs_path, _ = self._resolve_path(rel)
            if abs_path is None:
                errors.append(f"非法路径: {rel}")
                continue
            try:
                if abs_path.exists():
                    abs_path.unlink()
                    self.thumb_cache.delete(rel)
                    drop_image_meta(self._meta_cache, str(abs_path))
                    self._meta_dirty = True
                    deleted.append(rel)
            except Exception as e:
                errors.append(f"删除失败 {rel}: {e!s}")
        self._list_cache.clear()
        if deleted:
            self._flush_meta_if_dirty()
        return {"deleted": deleted, "errors": errors}

    def move_files(self, rel_paths: List[str], dest_rel: str) -> Dict:
        if not self._is_safe(dest_rel) or self._is_namespace_node(dest_rel):
            return {"moved": [], "errors": ["目标目录非法"]}
        dest_dir, _ = self._resolve_dir(dest_rel)
        if dest_dir is None or not dest_dir.is_dir():
            return {"moved": [], "errors": ["目标目录不存在"]}
        moved, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f"非法源路径: {rel}")
                continue
            src, _ = self._resolve_path(rel)
            if src is None:
                errors.append(f"非法源路径: {rel}")
                continue
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
                    self.thumb_cache.delete(rel)
                    drop_image_meta(self._meta_cache, str(src))
                    self._meta_dirty = True
                    moved.append(rel)
            except Exception as e:
                errors.append(f"移动失败 {rel}: {e!s}")
        self._list_cache.clear()
        if moved:
            self._flush_meta_if_dirty()
        return {"moved": moved, "errors": errors}

    def regenerate_thumbs(self, rel_paths: List[str]) -> Dict:
        """重新生成选中图片的缩略图：删除缓存缩略图并重新生成。

        用于修复下载丢失/文件被替换后残留的坏缩略图（如黑图、空图）。
        同时清理该图片的尺寸元数据缓存，避免旧尺寸残留。
        """
        regenerated, errors = [], []
        for rel in rel_paths:
            if not self._is_safe(rel):
                errors.append(f'非法路径: {rel}')
                continue
            try:
                self.thumb_cache.delete(rel)
                abs_path, _ = self._resolve_path(rel)
                drop_image_meta(self._meta_cache, str(abs_path or (self.root_dir / rel)))
                new_thumb = self.get_thumb_data(rel)
                if new_thumb:
                    regenerated.append(rel)
                else:
                    errors.append(f'缩略图生成失败: {rel}')
            except Exception as e:
                errors.append(f'缩略图更新失败 {rel}: {e!s}')
        if regenerated:
            self._save_meta()
        return {'regenerated': regenerated, 'errors': errors}

    def refresh(self) -> Dict:
        """清空内存缓存并作废旧相册索引，让新增/替换的图片立即生效（无需重启）。"""
        self._list_cache.clear()
        self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}
        self._invalidate_albums_cache()
        try:
            if self.album_cache_file.exists():
                self.album_cache_file.unlink()
        except OSError:
            pass
        return {'success': True}
