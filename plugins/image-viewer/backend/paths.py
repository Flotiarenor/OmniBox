"""数据根、派生路径与虚拟路径解析（多根目录 / 命名空间）。

状态自持：根目录、额外根、命名空间映射、派生的缓存文件路径都在这里。换根走
`rebind()`、设置变了走 `invalidate()`，两者都由入口编排 —— 不再有"某个分片顺手给
另一个分片的属性重新赋值"。

**命名空间映射是缓存的派生值。** 规则本身是纯函数（token = 根目录名，重名或与第一根
的一级子目录同名时按配置顺序加序号），但它每次计算都要 `os.scandir(第一根)` 拿
一级子目录名。实测：第一根 200 个目录 + 额外根 200 个目录，一次全树枚举触发 603 次
`os.scandir`，其中第一根目录被重复扫描 202 次 —— 因为额外根下的每个目录都会经
`virtual_path → namespace_for` 重算一遍。额外根下上万目录时这个数字同样上万。
所以这里把它降级为"按设置代次算一次"，由 `invalidate()` 明确作废。

缓存失效点（三条，都在这里关闭）：
- 换根 / 设置变更（`extra_roots` 改动会改变根集合与命名空间）→ 入口调 `invalidate()`；
- 目录增删（统一刷新基建发现、删除/移动/新建目录）→ 相册索引作废时一并调 `invalidate()`，
  因为命名空间要避开第一根的一级子目录名，目录集合变了 token 可能跟着变；
- 进程启动：没有缓存。

命名空间路径的形状：第二个及以后的根在相册树里以 `__<目录名>` 作为顶层节点。第一根
沿用相对路径，既有链接、缓存键与缩略图库都不受影响。
"""

import logging
import os
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from shell.backend.media_catalog import is_safe_path, list_subdirectories
from shell.backend.media_catalog import list_directory as _catalog_list_directory
from shell.backend.ports import SettingsPort

log = logging.getLogger(__name__)

#: 虚拟命名空间路径的保留前缀。真实目录名几乎不可能以它开头，解析时也不会与第一根的
#: 物理目录混淆（`__` 开头的路径段一律按命名空间解释）。
NAMESPACE_MARKER = '__'


def same_path(a: Optional[Path], b: Optional[Path]) -> bool:
    """两个路径是否指向同一位置（大小写/短名/软链接无关）。

    不能只比 `Path` 相等：Windows 上 `Path('C:\\Users\\ADMINI~1\\…').resolve()`
    不一定展开 8.3 短名，而同一位置的另一份写法可能已展开成长名，直接比较会把
    主根目录误判成「另一个根」。
    """
    if a is None or b is None:
        return False
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except Exception:
        return Path(a) == Path(b)


class PathLayout:
    """数据根与全部派生路径；虚拟路径 ↔ 物理路径。"""

    #: 命名空间前缀，供组件拼虚拟路径时使用（避免各自硬编码 `__`）。
    marker = NAMESPACE_MARKER

    def __init__(self, root: Path, settings: SettingsPort,
                 extensions: FrozenSet[str]) -> None:
        self._settings = settings
        self._extensions = extensions
        self.root = root
        self._extra_cache: Optional[List[Path]] = None
        self._first_dir_names: Optional[set] = None
        self._token_cache: Optional[Dict[str, str]] = None
        self._index_cache: Optional[Dict[str, Path]] = None
        self.rebind(root)

    # ===== 派生路径 =====

    def rebind(self, root: Path) -> None:
        """换数据根：重算全部派生路径并丢弃根集合缓存（由入口编排调用）。"""
        self.root = root
        self.cache_dir = root / '.cache'
        self.thumb_dir = self.cache_dir / 'thumbs'
        self.thumb_db_path = self.cache_dir / 'thumbs.db'
        self.meta_file = self.cache_dir / 'image_meta.json'
        self.album_cache_file = self.cache_dir / 'albums_index.json'
        # 全树目录快照单独一份：相册索引按目录名索引，回答不了"有没有新增目录"，
        # 而那份答案要靠 os.walk（机械盘 6s+）拿。
        self.dirs_cache_file = self.cache_dir / 'dirs_index.json'
        self.album_config_file = self.cache_dir / 'albums_config.json'
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.invalidate()

    def invalidate(self) -> None:
        """丢弃"按设置/按目录集合算出来"的缓存（根集合、第一根目录名、命名空间映射）。"""
        self._extra_cache = None
        self._first_dir_names = None
        self._token_cache = None
        self._index_cache = None

    # ===== 根集合 =====

    def extra_roots(self) -> List[Path]:
        """额外图片目录（设置项 `extra_roots`，每行一个）。

        只收当前存在的目录：设置里写下的路径可能已被删除或改成文件，此时它不该
        继续出现在 `/file` 的允许根与相册树里。结果按设置代次缓存。
        """
        if self._extra_cache is None:
            raw = self._settings.setting('extra_roots') or ''
            roots: List[Path] = []
            for line in str(raw).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    path = Path(line).expanduser().resolve()
                except Exception:
                    continue
                if path.is_dir() and not same_path(path, self.root) and path not in roots:
                    roots.append(path)
            self._extra_cache = roots
        return list(self._extra_cache)

    def roots(self) -> List[Path]:
        """全部根目录：`root` 恒为第一个（虚拟路径的默认根）。"""
        return [self.root, *self.extra_roots()]

    def is_primary(self, root: Path) -> bool:
        """该根是不是第一根（第一根不加命名空间前缀）。"""
        return same_path(root, self.root)

    # ===== 命名空间映射 =====

    def first_root_dir_names(self) -> set:
        """第一根的一级子目录名（命名空间不能与它们同名）。结果按设置代次缓存。"""
        if self._first_dir_names is None:
            names: set = set()
            try:
                with os.scandir(self.root) as entries:
                    names.update(entry.name for entry in entries if entry.is_dir())
            except OSError:
                pass
            self._first_dir_names = names
        return set(self._first_dir_names)

    def _token_map_for(self, roots: List[Path]) -> Dict[str, str]:
        """给一组（有序）额外根目录分配 token：重名/与第一根目录冲突时加序号。

        token 是派生值、不能持久化：早期版本把它写进 `.cache/roots_namespace.json`，
        目录增删之后会留下过时序号（`额外图库` 曾被永久卡在 `额外图库 (3)`）。
        """
        remaining = [root.name or 'root' for root in roots]
        taken = self.first_root_dir_names()
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

    def token_map(self) -> Dict[str, str]:
        """当前额外根的 `{根路径: token}`；缓存命中时不碰磁盘。"""
        if self._token_cache is None:
            self._token_cache = self._token_map_for(self.extra_roots())
        return dict(self._token_cache)

    def roots_index(self) -> Dict[str, Path]:
        """命名空间 token → 物理根目录。"""
        if self._index_cache is None:
            roots = self.extra_roots()
            tokens = self.token_map()
            self._index_cache = {tokens[str(root)]: root for root in roots}
        return dict(self._index_cache)

    def namespace_nodes(self) -> List[str]:
        """全部命名空间节点路径（`__<token>`），按根顺序。"""
        return [f'{NAMESPACE_MARKER}{token}' for token in self.roots_index()]

    def namespace_for(self, root: Path) -> str:
        """单个额外根目录的 token（与 `token_map` 同一套规则）。

        该根还没进配置（目录刚被选中、设置尚未保存）时接到队尾，保证"预览出来的
        token"和"保存后真正生效的 token"一致 —— 这条路径不缓存，是刻意保留的例外。
        """
        tokens = self.token_map()
        token = tokens.get(str(root))
        if token is not None:
            return token
        extra = self.extra_roots()
        if not any(same_path(root, item) for item in extra):
            extra.append(root)
        mapping = self._token_map_for(extra)
        for candidate, candidate_token in mapping.items():
            if same_path(Path(candidate), root):
                return candidate_token
        return root.name or 'root'

    # ===== 虚拟路径 ↔ 物理路径 =====

    def split_virtual(self, rel_path: str) -> Tuple[Optional[Path], str]:
        """虚拟路径 → (物理根目录, 根内相对路径)。

        命名空间前缀不参与图片/缩略图/相册路径的物理部分，因此调用方一律用返回的
        「根内相对路径」拼物理路径；未知命名空间返回 (None, '')。
        """
        rel = (rel_path or '').replace('\\', '/').strip('/')
        if not rel:
            return self.root, ''
        head, _, rest = rel.partition('/')
        if head.startswith(NAMESPACE_MARKER):
            token = head[len(NAMESPACE_MARKER):]
            root = self.roots_index().get(token)
            if root is None:
                return None, ''
            return root, rest
        return self.root, rel

    def virtual_path(self, root: Path, rel_in_root: str) -> str:
        """(物理根目录, 根内相对路径) → 虚拟路径。第一根不加前缀。"""
        rel = (rel_in_root or '').strip('/')
        if same_path(root, self.root):
            return rel
        token = self.namespace_for(root)
        prefix = f'{NAMESPACE_MARKER}{token}'
        return f'{prefix}/{rel}' if rel else prefix

    def resolve_dir(self, rel_path: str) -> Tuple[Optional[Path], str]:
        """虚拟目录路径 → (物理目录, 根内相对路径)；命名空间未知时物理目录为 None。"""
        root, inner = self.split_virtual(rel_path)
        if root is None:
            return None, ''
        return (root / inner if inner else root), inner

    def resolve_path(self, rel_path: str) -> Tuple[Optional[Path], str]:
        """虚拟图片路径 → (物理路径, 根内相对路径)；命名空间未知时物理路径为 None。"""
        root, inner = self.split_virtual(rel_path)
        if root is None or not inner:
            return None, ''
        return root / inner, inner

    def is_namespace_node(self, rel_path: str) -> bool:
        """该虚拟路径是否**就是**额外根目录的顶层节点（`__<命名空间>`）。

        只有恰好一层才算：`__额外图库` 是虚拟节点，`__额外图库/作者B` 是它下面的
        真实目录，必须走普通目录扫描。
        """
        head = (rel_path or '').strip('/').partition('/')[0]
        if head != (rel_path or '').strip('/'):
            return False
        token = head[len(NAMESPACE_MARKER):] if head.startswith(NAMESPACE_MARKER) else ''
        return bool(token) and token in self.roots_index()

    def in_namespace(self, rel_path: str) -> bool:
        """该虚拟路径是否以命名空间开头（含命名空间节点本身，不论是否有效）。"""
        head = (rel_path or '').strip('/').partition('/')[0]
        return head.startswith(NAMESPACE_MARKER) and len(head) > len(NAMESPACE_MARKER)

    def virtual_dir_exists(self, rel_path: str) -> bool:
        """虚拟目录是否存在（供「保留可见的空目录」标记清理时判断）。"""
        if self.is_namespace_node(rel_path):
            return True
        target, _ = self.resolve_dir(rel_path)
        return bool(target and target.is_dir())

    def is_safe(self, rel_path: str) -> bool:
        """虚拟路径是否安全落在它所属的根内（未知命名空间一律拒绝）。"""
        root, inner = self.split_virtual(rel_path)
        if root is None:
            return False          # 未知命名空间：拒绝，避免落到第一根的物理目录
        return is_safe_path(root, inner)

    # ===== 壳按名调用的入口（委派到本组件的三个公开视图） =====

    def resolve_file_path(self, rel_path: str) -> Optional[Path]:
        """虚拟路径 → 物理路径（壳的 `/file` 相对路径分支调用它）。

        没有这个方法时，`__额外图库/作者B/图.jpg` 会被壳按**第一根**拼成
        `<主目录>/__额外图库/作者B/图.jpg` —— 那个文件不存在，于是「网格与缩略图都
        正常，点开任何一张原图 404」。`/thumbs` 一直是由插件解释路径的，这里把
        `/file` 对齐到同一套。

        未知命名空间（用户删掉了那个根、或拼写不对）返回 None → 壳回退默认解析
        → 最终 404，而不是把 `__额外图库` 当成第一根下的真实目录去读。
        """
        try:
            target, _ = self.resolve_path(rel_path)
        except Exception:
            return None
        return target

    def get_root_dir(self) -> str:
        """第一根目录的绝对路径（API：`get_root_dir`）。"""
        return str(self.root)

    def list_roots(self) -> List[Dict]:
        """全部根目录（第一根在前），供设置页管理多文件夹，也供附属插件按根遍历。

        `namespace` 是额外根的命名空间 token（第一根为空串）；`prefix` 是拼虚拟路径时
        直接用的前缀（第一根为空串，额外根形如 `__额外图库`）。**附属插件用 `prefix`**：
        把 `<prefix>/<根内相对路径>` 交给壳的 `/file`、`/thumbs` 与 `delete_files()` 即可，
        不必自己认 `__` 记号，也不必复刻命名空间的取名规则。
        """
        roots: List[Dict] = []
        for index, root in enumerate(self.roots()):
            roots.append({
                'path': str(root),
                'label': root.name or str(root),
                'is_primary': index == 0,
                'exists': root.is_dir(),
                'namespace': '' if index == 0 else self.namespace_for(root),
                'prefix': self.virtual_path(root, ''),
            })
        return roots

    def browse_dir(self, path: str = '') -> Dict:
        """浏览本机目录（绝对路径），供设置页添加额外图片目录。

        目录枚举与「有哪些媒体类型」的判断走共享基建 `list_subdirectories`，
        所以这里能同时标出含图片 / 视频 / 音乐的目录；空路径是「我的电脑」层。
        """
        result = list_subdirectories(path)
        if result.get('error'):
            return result
        for entry in result.get('entries', []):
            entry['is_image_dir'] = 'image' in (entry.get('kinds') or [])
        return result

    def list_directory(self, root: Path, rel_path: str) -> List[Dict]:
        """目录树浏览（移动目标选择等）只返回子目录。"""
        return _catalog_list_directory(
            root, rel_path, allowed_extensions=set(self._extensions),
            include_files=False)['dirs']
