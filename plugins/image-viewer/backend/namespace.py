"""多根目录 / 虚拟命名空间路径（ImageViewerPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 ImageViewerPlugin.__init__ 持有 ——
分片只把方法挂到同一个类上，因此方法与调用点都没有变。
"""

import logging
import os
from pathlib import Path
from typing import Dict, List

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'image_viewer')
NAMESPACE_MARKER = _common.NAMESPACE_MARKER
is_safe_path = _common.is_safe_path
_same_path = _common.same_path

class NamespaceMixin:
    """多根目录 / 虚拟命名空间路径。"""

    # ===== 多根目录：虚拟路径 ↔ 物理路径 =====
    #
    # 第一根（root_dir）沿用相对路径，行为与历史版本一致；第二根起在相册树里
    # 以 `__<目录名>` 作为顶层节点（命名空间），其下路径为该根内的相对路径。
    # 这样第一根的既有链接、缓存键、缩略图库都不受影响。
    def _rebuild_paths(self):
        """重建缓存类路径字段（root_dir / extra_roots 变更时调用）。"""
        self.cache_dir = self.root_dir / '.cache'
        self.thumb_dir = self.cache_dir / 'thumbs'
        self.thumb_db_path = self.cache_dir / 'thumbs.db'
        self.meta_file = self.cache_dir / 'image_meta.json'
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.album_cache_file = self.cache_dir / 'albums_index.json'
        self.album_config_file = self.cache_dir / 'albums_config.json'

    def _extra_roots(self) -> List[Path]:
        """额外图片目录（设置项 extra_roots，每行一个）。"""
        raw = self.setting('extra_roots') or ''
        roots: List[Path] = []
        for line in str(raw).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                path = Path(line).expanduser().resolve()
            except Exception:
                continue
            if path.is_dir() and not _same_path(path, self.root_dir) and path not in roots:
                roots.append(path)
        return roots

    def _roots(self) -> List[Path]:
        """全部根目录：root_dir 恒为第一个（虚拟路径的默认根）。"""
        return [self.root_dir, *self._extra_roots()]

    def get_file_roots(self) -> List[Path]:
        """文件服务允许访问的根目录（Shell 的 /file 逐根做路径安全检查）。"""
        try:
            return self._roots()
        except Exception:
            return [self.root_dir]

    def _first_root_dir_names(self) -> set:
        """第一根一级子目录名（命名空间不能与它们同名）。"""
        names = set()
        try:
            with os.scandir(self.root_dir) as entries:
                names.update(e.name for e in entries if e.is_dir())
        except OSError:
            pass
        return names

    def _load_namespace_map(self) -> dict:
        """兼容旧缓存文件：命名空间已改为按当前状态实时计算，不再读写它。

        早期版本把 token 持久化在 `.cache/roots_namespace.json`，会在目录增删
        之后留下过时序号，所以现在一律以 `_namespace_map()` 为准。
        """
        return {}

    def _namespace_map(self) -> Dict[str, str]:
        """按当前状态算出全部额外根目录的命名空间 token（纯函数，可重算）。

        规则：token = 根目录名；重名（或与第一根的一级子目录同名）时按**配置
        顺序**加 ` (2)`、` (3)` 序号。同一份配置每次得到同一组 token，即使用户
        后来把冲突的目录删掉，序号也会立刻收回（token 是派生值，不能持久化——
        旧实现把 `额外图库` 永久卡在了 `额外图库 (3)`）。
        """
        roots = self._extra_roots()
        remaining = [root.name or 'root' for root in roots]
        taken = self._first_root_dir_names()
        for index in range(len(roots)):
            base = remaining[index]
            token = base
            counter = 2
            while token in taken:
                token = f'{base} ({counter})'
                counter += 1
            taken.add(token)
            remaining[index] = token
        return {str(root): remaining[i] for i, root in enumerate(roots)}

    def _namespace_map_for(self, roots: List[Path]) -> Dict[str, str]:
        """给一组（有序）额外根目录分配 token：重名/与第一根目录冲突时加序号。"""
        remaining = [root.name or 'root' for root in roots]
        taken = self._first_root_dir_names()
        for index in range(len(roots)):
            base = remaining[index]
            token = base
            counter = 2
            while token in taken:
                token = f'{base} ({counter})'
                counter += 1
            taken.add(token)
            remaining[index] = token
        return {str(root): remaining[i] for i, root in enumerate(roots)}

    def _namespace(self, root: Path) -> str:
        """给单个额外根目录取命名空间 token（与 `_namespace_map` 同一套规则）。"""
        roots = self._extra_roots()
        # 该根还没进配置（目录刚被选中、设置尚未保存）时接到队尾，保证
        # 「预览出来的 token」和「保存后真正生效的 token」一致
        if not any(_same_path(root, r) for r in roots):
            roots.append(root)
        mapping = self._namespace_map_for(roots)
        for candidate, token in mapping.items():
            if _same_path(candidate, root):
                return token
        return root.name or 'root'

    def _roots_index(self) -> Dict[str, Path]:
        """命名空间 token → 物理根目录（每次调用按当前设置重算，保持新鲜）。"""
        index = {}
        for root in self._extra_roots():
            index[self._namespace(root)] = root
        return index

    def _split_virtual(self, rel_path: str) -> tuple:
        """虚拟路径 → (物理根目录, 根内相对路径)。

        命名空间前缀不参与图片/缩略图/相册路径的物理部分，因此调用方一律用
        返回的「根内相对路径」拼物理路径；未知命名空间返回 (None, '')。
        """
        rel = (rel_path or '').replace('\\', '/').strip('/')
        if not rel:
            return self.root_dir, ''
        head, _, rest = rel.partition('/')
        if head.startswith(NAMESPACE_MARKER):
            token = head[len(NAMESPACE_MARKER):]
            root = self._roots_index().get(token)
            if root is None:
                return None, ''
            return root, rest
        return self.root_dir, rel

    def _virtual_path(self, root: Path, rel_in_root: str) -> str:
        """(物理根目录, 根内相对路径) → 虚拟路径。第一根不加前缀。"""
        rel = (rel_in_root or '').strip('/')
        if _same_path(root, self.root_dir):
            return rel
        token = self._namespace(root)
        prefix = f'{NAMESPACE_MARKER}{token}'
        return f'{prefix}/{rel}' if rel else prefix

    def _resolve_dir(self, rel_path: str):
        """虚拟目录路径 → (物理目录, 根内相对路径)；命名空间未知时物理目录为 None。"""
        root, inner = self._split_virtual(rel_path)
        if root is None:
            return None, ''
        return (root / inner if inner else root), inner

    def _resolve_path(self, rel_path: str):
        """虚拟图片路径 → (物理路径, 根内相对路径)；命名空间未知时物理路径为 None。"""
        root, inner = self._split_virtual(rel_path)
        if root is None or not inner:
            return None, ''
        return root / inner, inner

    def _is_namespace_node(self, rel_path: str) -> bool:
        """该虚拟路径是否**就是**额外根目录的顶层节点（`__<命名空间>`）。

        只有恰好一层才算：`__额外图库` 是虚拟节点，`__额外图库/作者B` 是它下面的
        真实目录，必须走普通目录扫描。
        """
        head = (rel_path or '').strip('/').partition('/')[0]
        if head != (rel_path or '').strip('/'):
            return False
        token = head[len(NAMESPACE_MARKER):] if head.startswith(NAMESPACE_MARKER) else ''
        return bool(token) and token in self._roots_index()

    def _in_namespace(self, rel_path: str) -> bool:
        """该虚拟路径是否以命名空间开头（含命名空间节点本身，不论是否有效）。"""
        head = (rel_path or '').strip('/').partition('/')[0]
        return head.startswith(NAMESPACE_MARKER) and len(head) > len(NAMESPACE_MARKER)

    def _virtual_dir_exists(self, rel_path: str) -> bool:
        """虚拟目录是否存在（供「保留可见的空目录」标记清理时判断）。"""
        if self._is_namespace_node(rel_path):
            return True
        target, _ = self._resolve_dir(rel_path)
        return bool(target and target.is_dir())

    def _is_safe(self, rel_path: str) -> bool:
        root, inner = self._split_virtual(rel_path)
        if root is None:
            return False          # 未知命名空间：拒绝，避免落到第一根的物理目录
        return is_safe_path(root, inner)
