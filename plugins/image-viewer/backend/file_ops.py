"""新建 / 删除 / 移动目录与文件、定向重建缩略图。

状态自持：无长期状态（缓存都在缩略图库、相册索引与可见性配置里）。这一步的业务是
"动磁盘之后让派生数据跟上"，所以它拿到的依赖正好是那三处：缩略图库、相册索引、
可见性配置；路径安全与受保护清单由路径组件与宿主提供。

删除与移动是**不可逆**操作，两道判定都必须在这里做，壳拦不到 `/api` 下的插件方法：

- 根内校验（`is_safe` 拒绝未知命名空间与跑出根的路径，`is_namespace_node` 拒绝虚拟
  节点本身）；
- 受保护清单（`is_protected` 由宿主注入）：根可以被设置改写成包含 `<config>` 的目录，
  只做根内校验就能删掉凭据文件 —— 而"读"是挡住的。
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List

log = logging.getLogger(__name__)


class FileOps:
    """目录与文件的新增、删除、移动，以及定向缩略图重建。"""

    def __init__(self, paths: Any, thumbs: Any, config: Any, albums: Any,
                 is_protected: Callable[[Path], bool], extensions: Any,
                 invalidate_derived: Callable[..., Dict[str, Any]]) -> None:
        self._paths = paths
        self._thumbs = thumbs
        self._config = config
        self._albums = albums
        self._is_protected = is_protected
        self._extensions = extensions
        # 动过磁盘之后让派生数据（相册索引 / 列表缓存 / 漫画根解析）失效的宿主入口。
        # 只清列表缓存不够：相册索引有自己的 TTL，会让"删了图片但相册树计数不变"
        # 持续到过期为止。删除 / 移动只改文件，传 `keep_snapshot=True` 保住目录快照。
        self._invalidate_derived = invalidate_derived

    # ===== 目录 =====

    def create_folder(self, rel_path: str) -> Dict:
        """在根目录（或指定相对目录）下新建相册文件夹。

        没有直接图片也没有下级图片的空目录默认不显示（见可见性配置的说明），这里把
        新建的目录记进「保留可见」标记，避免刚建完就消失；等里面进了图片
        （`image_count > 0`）或目录被删除时标记自动清理。
        """
        rel_path = (rel_path or '').replace('\\', '/').strip('/')
        if not rel_path or not self._paths.is_safe(rel_path):
            return {'success': False, 'error': '文件夹名称非法'}
        if self._paths.in_namespace(rel_path):
            return {'success': False, 'error': '不能在根目录节点下创建文件夹'}
        target, _ = self._paths.resolve_dir(rel_path)
        if target is None:
            return {'success': False, 'error': '文件夹名称非法'}
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return {'success': False, 'error': str(exc)}
        self._config.mark_visible(rel_path)
        self._albums.invalidate()
        return {'success': True, 'path': rel_path}

    def delete_folder(self, rel_path: str) -> Dict:
        r"""删除一个**空**目录（含只剩空子目录的情况），并清掉它的可见标记。

        只允许删除「递归都没有图片」的目录：有图片的目录必须先清空图片，避免一个误点
        连带删掉整棵作品树。

        路径必须落在它所属的根目录内：`../`、绝对路径、未知命名空间一律拒绝。这是
        **删除**操作，越权代价是根目录外的整棵目录树，所以不能只依赖调用方传对相对
        路径。

        根目录自身与壳的受保护清单另外两道判定：`'.'` / `'a/..'` 都通过根内校验（解析
        结果就是根目录本身），于是"删空目录"能删掉整个图库根；而根可以被设置改写成
        包含凭据目录的位置，此时那只是"根内、没有图片"的普通目录。
        """
        rel_path = (rel_path or '').replace('\\', '/').strip('/')
        # 两个校验缺一不可：
        #   - 根内校验拒绝未知命名空间，以及任何跑出所属根的路径（`../victim`、绝对
        #     路径 —— `root / '/abs'` 在 pathlib 里会丢弃 root，必须拦）；
        #   - 命名空间节点本身是**虚拟**节点（没有自己的物理目录），而根内校验对它是
        #     通过的（根内相对路径为空），所以必须单独拒绝。
        if not rel_path or not self._paths.is_safe(rel_path) \
                or self._paths.is_namespace_node(rel_path):
            return {'success': False, 'error': '路径非法'}
        target, _ = self._paths.resolve_dir(rel_path)
        if target is None:
            return {'success': False, 'error': '路径非法'}
        root, _inner = self._paths.split_virtual(rel_path)
        if root is None:
            return {'success': False, 'error': '路径非法'}
        if target.resolve() == root.resolve():
            return {'success': False, 'error': '不能删除根目录本身'}
        if self._is_protected(target):
            return {'success': False, 'error': '该目录受保护，拒绝删除'}
        if not target.is_dir():
            return {'success': False, 'error': '目录不存在'}
        try:
            for _current, _dirs, files in os.walk(target):
                if any(not name.startswith('.')
                       and Path(name).suffix.lower() in self._extensions
                       for name in files):
                    return {'success': False, 'error': '目录内还有图片，请先删除图片'}
            # 二次校验：`os.walk` 到 `rmtree` 之间路径可能被换成指向根外的符号链接 /
            # junction（TOCTOU），删除前用 resolve() 后的物理路径再确认一次。
            if not target.resolve().is_relative_to(root):
                return {'success': False, 'error': '路径非法'}
            shutil.rmtree(target)
        except Exception as exc:
            return {'success': False, 'error': str(exc)}
        self._config.drop_marks_under(rel_path)
        self._albums.invalidate()
        return {'success': True}

    # ===== 文件 =====

    def delete_files(self, rel_paths: List[str]) -> Dict:
        deleted: List[str] = []
        errors: List[str] = []
        for rel in rel_paths:
            if not self._paths.is_safe(rel):
                errors.append(f'非法路径: {rel}')
                continue
            abs_path, _ = self._paths.resolve_path(rel)
            if abs_path is None:
                errors.append(f'非法路径: {rel}')
                continue
            # 删除同样要过受保护清单：根可以被设置改写成包含凭据目录的位置，只做根内
            # 校验就能删掉令牌与插件设置文件（读是挡住的）。
            if self._is_protected(abs_path):
                errors.append(f'受保护路径，拒绝删除: {rel}')
                continue
            try:
                if abs_path.exists():
                    abs_path.unlink()
                    self._thumbs.delete_key(rel)
                    self._thumbs.drop(str(abs_path))
                    deleted.append(rel)
            except Exception as exc:
                errors.append(f'删除失败 {rel}: {exc!s}')
        if deleted:
            # 删成功就立刻失效派生数据：列表缓存按目录 mtime 自愈，但相册索引另有
            # 30 秒 TTL，只清列表会让相册树的张数陈旧（下一次 list_albums 要等过期
            # 或手动校验）。文件级操作**不改目录集合**，目录快照必须保留：整份失效
            # 换来的是一次全树枚举（机械盘 6-23s），远大于它要修的陈旧窗口。动过的
            # 目录单独传下去，条目精确作废、递归计数跟着重算。
            dirs = sorted({str(rel).rpartition('/')[0] for rel in deleted})
            self._invalidate_derived('image-viewer:delete', keep_snapshot=True, dirs=dirs)
            self._thumbs.flush()
        return {'deleted': deleted, 'errors': errors}

    def move_files(self, rel_paths: List[str], dest_rel: str) -> Dict:
        if not self._paths.is_safe(dest_rel) or self._paths.is_namespace_node(dest_rel):
            return {'moved': [], 'errors': ['目标目录非法']}
        dest_dir, _ = self._paths.resolve_dir(dest_rel)
        if dest_dir is None or not dest_dir.is_dir():
            return {'moved': [], 'errors': ['目标目录不存在']}
        if self._is_protected(dest_dir):
            return {'moved': [], 'errors': ['目标目录受保护，拒绝移动']}
        moved: List[str] = []
        errors: List[str] = []
        for rel in rel_paths:
            if not self._paths.is_safe(rel):
                errors.append(f'非法源路径: {rel}')
                continue
            src, _ = self._paths.resolve_path(rel)
            if src is None:
                errors.append(f'非法源路径: {rel}')
                continue
            # 移动等价于"从原位置拿走"：受保护文件被移进媒体根后，读路由的清单就再也
            # 认不出它（路径变了），所以源与目标都要判。
            if self._is_protected(src):
                errors.append(f'受保护路径，拒绝移动: {rel}')
                continue
            try:
                if src.exists():
                    dest_file = dest_dir / src.name
                    if dest_file.exists() and src != dest_file:
                        stem, suffix = dest_file.stem, dest_file.suffix
                        counter = 1
                        while dest_file.exists():
                            dest_file = dest_dir / f'{stem}_{counter}{suffix}'
                            counter += 1
                    shutil.move(str(src), str(dest_file))
                    self._thumbs.delete_key(rel)
                    self._thumbs.drop(str(src))
                    moved.append(rel)
            except Exception as exc:
                errors.append(f'移动失败 {rel}: {exc!s}')
        if moved:
            # 源与目标是两个目录：整份失效同时覆盖两端（源少一张、目标多一张），
            # 按目录挑着作废会漏掉父级的递归计数与封面。移动的是文件，目录集合没变
            # —— 保留目录快照，否则下一次相册重建要重走全树（机械盘 6-23s）。
            dirs = sorted({str(rel).rpartition('/')[0] for rel in moved} | {str(dest_rel)})
            self._invalidate_derived('image-viewer:move', keep_snapshot=True, dirs=dirs)
            self._thumbs.flush()
        return {'moved': moved, 'errors': errors}

    # ===== 定向重建 =====

    def regenerate_thumbs(self, rel_paths: List[str]) -> Dict:
        """重新生成选中图片的缩略图：删除缓存缩略图并重新生成。

        用于修复下载丢失/文件被替换后残留的坏缩略图（如黑图、空图）。同时清理该图片
        的尺寸元数据缓存，避免旧尺寸残留。
        """
        regenerated: List[str] = []
        errors: List[str] = []
        for rel in rel_paths:
            if not self._paths.is_safe(rel):
                errors.append(f'非法路径: {rel}')
                continue
            try:
                self._thumbs.delete_key(rel)
                abs_path, _ = self._paths.resolve_path(rel)
                self._thumbs.drop(str(abs_path or (self._paths.root / rel)))
                new_thumb = self._thumbs.get_thumb_data(rel)
                if new_thumb:
                    regenerated.append(rel)
                else:
                    errors.append(f'缩略图生成失败: {rel}')
            except Exception as exc:
                errors.append(f'缩略图更新失败 {rel}: {exc!s}')
        if regenerated:
            self._thumbs.save_meta()
        return {'regenerated': regenerated, 'errors': errors}
