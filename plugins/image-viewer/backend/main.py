import json
import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar, Dict, List

from shell.backend.media_catalog import list_subdirectories
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling
from shell.backend.tasks import BackgroundTask
from shell.backend.thumb_cache import ThumbCache

log = logging.getLogger(__name__)

# 虚拟命名空间路径的保留前缀：第二个及以后的根目录在相册树里以
# `__<目录名>` 作为顶层节点。真实目录名几乎不可能以它开头，解析时也不会
# 与第一根的物理目录混淆（`__` 开头的路径段一律按命名空间解释）。
NAMESPACE_MARKER = '__'



_fs = load_sibling(__file__, 'filesystem', 'image_viewer')
ALLOWED_EXTENSIONS = _fs.ALLOWED_EXTENSIONS
drop_image_meta = _fs.drop_image_meta
ensure_thumbnail = _fs.ensure_thumbnail
get_image_size = _fs.get_image_size
is_safe_path = _fs.is_safe_path
list_directory = _fs.list_directory
natural_sort_key = _fs.natural_sort_key
pixiv_number = _fs.pixiv_number
stat_mtime = _fs.stat_mtime


def _pixiv_sort(entries, name_fn, reverse, fuzzy=False):
    """Pixiv 排序：前导数字条目按数字大小排（方向生效），
    无前导数字条目按自然名排并始终位于最后。

    `fuzzy`（模糊匹配）只改**同号/无号条目之间**的比较键：关掉时同号内按
    文件名字符串自然序，打开时按整名的自然序（数字段比数字、其余段比文字），
    于是 `2024-05-10` < `2024-05-24 日富美` 这类「先数字再文字」的条目也能排对。
    """
    numeric, other = [], []
    for it in entries:
        name = name_fn(it)
        num = pixiv_number(name)
        key = (num, natural_sort_key(name if fuzzy else Path(name).name))
        (numeric if num is not None else other).append((key, it))
    numeric.sort(key=lambda t: t[0], reverse=reverse)
    other.sort(key=lambda t: t[0], reverse=reverse)
    return [it for _, it in numeric] + [it for _, it in other]


def _pick_cover(images: List[Dict], pixiv: bool, name_fn=lambda img: img['rel']) -> str:
    """目录封面挑选，返回选中图片的 name_fn 值（`_scan_dir_direct` 里即 rel 相对路径）。

    Pixiv 树（`sort_by == 'time_name'`）取**作品号最大**的那张 —— 画师目录里
    平铺着 `<pid>.jpg` / `<pid>_p0.jpg`（pixiv-sync 的落盘规则），按自然序第一张
    取封面等于永远展示该画师最老的作品；同一作品（同 pid）内再取文件名自然序
    第一张，即 p0 而不是 p1/p10。非 Pixiv 目录保持原有行为：文件名自然序第一张。
    """
    if not images:
        return ''
    if pixiv:
        numbered = [(pixiv_number(Path(name_fn(img)).name), img) for img in images]
        known = [num for num, _ in numbered if num is not None]
        if known:
            newest = max(known)
            same_work = [img for num, img in numbered if num == newest]
            return name_fn(min(same_work, key=lambda img: natural_sort_key(Path(name_fn(img)).name)))
    return name_fn(min(images, key=lambda img: natural_sort_key(Path(name_fn(img)).name)))


def _cover_rank(cover_rel: str, mtime: float, pixiv: bool) -> tuple:
    """封面优先级键（可直接 max() 比较）。

    Pixiv 树按作品号大者优先（= 画师最近的作品），非 Pixiv 目录沿用
    「mtime 更新者优先」；Pixiv 条目整体优先于无号的普通条目。

    作品号一律取**封面文件名的前导数字**：pixiv-sync 落盘的是
    `<pid>.jpg` / `<pid>_pN.jpg`，嵌在目录里的 `.../800_title/800_p0.png`
    同样以作品号开头，所以看文件名既覆盖平铺也覆盖嵌套结构。
    """
    num = pixiv_number(Path(cover_rel).name) if cover_rel else None
    if pixiv and num is not None:
        return (1, num, 0.0)
    return (0, 0.0, mtime)

def _same_path(a, b) -> bool:
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


class ImageViewerPlugin(PluginBase):
    settings_schema: ClassVar[List[Dict[str, Any]]] = [
        {"key": "root_dir", "label": "数据根目录", "type": "text",
         "placeholder": "默认: ./data", "help": "图片浏览的数据根目录"},
        {"key": "extra_roots", "label": "额外图片目录", "type": "textarea",
         "placeholder": "每行一个目录（也可在插件设置面板里增删）",
         "help": "与主根目录一起浏览：每个目录在相册树里显示为顶层节点"},
        {"key": "row_height", "label": "图片行高", "type": "range",
         "min": 100, "max": 400, "default": 200, "help": "Justified 布局的每行目标高度"},
        {"key": "per_page", "label": "每页图片数", "type": "number",
         "min": 10, "max": 200, "default": 40},
        {"key": "sort_by", "label": "排序方式", "type": "select",
         "default": "mtime",
         "options": [{"label": "修改时间", "value": "mtime"},
                     {"label": "文件名", "value": "name"},
                     {"label": "Pixiv 排序支持", "value": "time_name"}]},
        {"key": "sort_order", "label": "排序方向", "type": "select",
         "default": "desc",
         "options": [{"label": "倒序", "value": "desc"}, {"label": "正序", "value": "asc"}]},
        {"key": "pixiv_fuzzy", "label": "模糊匹配", "type": "checkbox",
         "default": False,
         "help": "仅「Pixiv 排序支持」生效：作品名不以前导数字开头时按"
                 "「先数字再文字」排序，并让该文件夹套用 Pixiv 的浏览效果"},
        {"key": "album_sort_by", "label": "作者视图排序方式", "type": "select",
         "default": "mtime",
         "options": [{"label": "更新时间", "value": "mtime"},
                     {"label": "文件名", "value": "name"},
                     {"label": "图片数量", "value": "count"}]},
        {"key": "album_sort_order", "label": "作者视图排序方向", "type": "select",
         "default": "desc",
         "options": [{"label": "倒序", "value": "desc"}, {"label": "正序", "value": "asc"}]},
    ]

    def __init__(self, manifest, config):
        super().__init__(manifest, config)
        root = self.setting('root_dir') or str(super().get_data_root())
        self.root_dir = Path(root).resolve()
        self._rebuild_paths()
        self._meta_cache = self._load_meta()
        self._meta_dirty = False          # 尺寸元数据是否有新增条目需要落盘
        self._list_cache = {}
        self._album_config = self._load_album_config()
        self._album_cache = self._load_album_cache()
        self._albums_cached_at = 0.0      # list_albums 结果 TTL 缓存
        self._albums_cached = None
        # 缩略图缓存（共享基建）与全量重建后台任务（共享基建）
        self.thumb_cache = ThumbCache(self.thumb_db_path)
        self._rebuild = None              # BackgroundTask | None

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

    # ===== 常量 =====
    _ALBUMS_TTL = 30.0            # list_albums 全树扫描结果缓存秒数
    # 相册索引缓存版本：封面挑选规则变更时必须 +1，否则旧缓存会让新规则不生效
    # （目录 mtime 没变 → 增量扫描直接复用旧的 direct_cover）。
    # 4：Pixiv 树封面 = 作品号最大的那张（画师最近的作品）
    # 3：封面 = 文件名自然序第一张（p0）
    # 2：封面 = 最新 mtime 的那张
    _ALBUM_CACHE_VERSION = 4
    _MAX_LIST_CACHE = 200         # 列表缓存最大条目数，超出后淘汰最旧一半
    _MAX_ALL_IMAGES = 5000        # 连续浏览序列最大返回条数，超出截断
    _SCAN_WORKERS = 8             # 扫描/尺寸读取并行线程数

    def _load_meta(self) -> dict:
        if self.meta_file.exists():
            try:
                with open(self.meta_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_meta(self):
        """原子写元数据：先写临时文件再 os.replace，避免中途崩溃损坏缓存。"""
        try:
            tmp = self.meta_file.with_suffix('.json.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._meta_cache, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.meta_file)
        except Exception as e:
            log.error(f"[ImageViewer] 保存元数据失败: {e}")

    def _mark_meta_dirty(self):
        self._meta_dirty = True

    def get_data_root(self) -> Path:
        return self.root_dir

    def _is_safe(self, rel_path: str) -> bool:
        root, inner = self._split_virtual(rel_path)
        if root is None:
            return False          # 未知命名空间：拒绝，避免落到第一根的物理目录
        return is_safe_path(root, inner)


    def _pixiv_mode(self, rel_path: str = '') -> bool:
        """该目录生效的排序是否为「Pixiv 排序支持」（含逐级继承）。

        封面与排序的 Pixiv 规则只由排序方式决定，模糊匹配不参与：它只把
        显式勾选它的目录认定为 Pixiv 树的配置点（见 `get_settings` 的
        `pixiv_explicit`），让「作者/作品名/序号.jpg」这类非数字命名的目录
        也拿到两层浏览效果。
        """
        return self.get_settings(rel_path).get('sort_by') == 'time_name'

    def _pixiv_fuzzy_mode(self, rel_path: str = '') -> bool:
        """该目录是否在「Pixiv 排序支持」下还勾了「模糊匹配」。"""
        s = self.get_settings(rel_path)
        return s.get('sort_by') == 'time_name' and bool(s.get('pixiv_fuzzy'))

    def register_api(self) -> dict:
        return {
            'list_images': self.list_images,
            'list_folder_items': self.list_folder_items,
            'list_dir': self.list_dir,
            'list_albums': self.list_albums,
            'create_folder': self.create_folder,
            'get_album_config': self.get_album_config,
            'set_album_config': self.set_album_config,
            'get_image_info': self.get_image_info,
            'delete_files': self.delete_files,
            'move_files': self.move_files,
            'regenerate_thumbs': self.regenerate_thumbs,
            'refresh': self.refresh,
            'rebuild_all': self.rebuild_all,
            'rebuild_folder': self.rebuild_folder,
            'rebuild_status': self.rebuild_status,
            'rebuild_cancel': self.rebuild_cancel,
            'get_settings': self.get_settings,
            'save_settings': self.save_folder_settings,
            'get_root_dir': self.get_root_dir,
            'clear_folder_settings': self.clear_folder_settings,
            'list_roots': self.list_roots,
            'browse_dir': self.browse_dir,
            'delete_folder': self.delete_folder,
        }

    def get_root_dir(self) -> str:
        return str(self.root_dir)

    def list_roots(self) -> List[Dict]:
        """全部根目录（第一根在前），供设置页管理多文件夹。"""
        roots = []
        for index, root in enumerate(self._roots()):
            roots.append({
                'path': str(root),
                'label': root.name or str(root),
                'is_primary': index == 0,
                'exists': root.is_dir(),
                'namespace': '' if index == 0 else self._namespace(root),
            })
        return roots

    def browse_dir(self, path: str = '') -> Dict:
        """浏览本机目录（绝对路径），供设置页添加额外图片目录。

        目录枚举与「有哪些媒体类型」的判断走共享基建
        `shell.backend.media_catalog`（`list_subdirectories` / `find_kinds`），
        所以这里能同时标出含图片 / 视频 / 音乐的目录；空路径是「我的电脑」层。
        """
        result = list_subdirectories(path)
        if result.get('error'):
            return result
        for entry in result.get('entries', []):
            entry['is_image_dir'] = 'image' in (entry.get('kinds') or [])
        return result

    def ensure_thumb(self, rel_path: str) -> str:
        # 旧版文件式入口，供其他兼容代码使用；新路由优先走 get_thumb_data。
        if not self._is_safe(rel_path):
            return ''
        try:
            thumb = self._get_thumb(rel_path)
            return str(thumb) if thumb and thumb.exists() else ''
        except Exception:
            return ''

    def get_thumb_data(self, rel_path: str):
        """供 Shell /thumbs 路由使用：从 SQLite（ThumbCache）读取/生成缩略图字节。"""
        if not self._is_safe(rel_path):
            return None
        try:
            abs_path, _ = self._resolve_path(rel_path)
            if abs_path is None:
                return None
            return self.thumb_cache.get(rel_path, abs_path)
        except Exception:
            return None

    def get_image_info(self, rel_path: str) -> Dict:
        """返回单张图片的存储大小与分辨率，供全屏查看器右侧信息面板使用。"""
        if not self._is_safe(rel_path):
            return {'success': False, 'error': '非法路径'}
        abs_path, _ = self._resolve_path(rel_path)
        if abs_path is None:
            return {'success': False, 'error': '非法路径'}
        try:
            if not abs_path.is_file():
                return {'success': False, 'error': '不是图片文件'}
            stat = abs_path.stat()
            width, height = self._get_image_size(str(abs_path), stat.st_mtime)
            return {
                'success': True,
                'rel_path': rel_path,
                'size': stat.st_size,
                'width': width,
                'height': height,
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _get_dir_mtime(self, rel_path: str) -> float:
        root, inner = self._split_virtual(rel_path)
        if root is None:
            return 0.0
        return stat_mtime(root, inner)


    def _get_image_size(self, abs_path: str, mtime: float) -> tuple:
        before = len(self._meta_cache)
        result = get_image_size(abs_path, mtime, self._meta_cache)
        if len(self._meta_cache) != before:
            # 新增了尺寸缓存条目才需要落盘，避免每次列表都全量重写 meta 文件
            self._meta_dirty = True
        return result

    def _fill_image_sizes(self, images: List[dict]):
        """并行填充 images 列表的 width/height（要求条目含 path/mtime 字段）。

        首次扫描大文件夹时 Pillow 打开文件读尺寸是主要开销，8 线程并行可
        把 4 万张图片的扫描从 ~5s 降到 ~1.5s。
        """
        if not images:
            return
        with ThreadPoolExecutor(max_workers=self._SCAN_WORKERS) as ex:
            futures = [ex.submit(self._get_image_size, it['path'], it['mtime'])
                       for it in images]
            for it, fut in zip(images, futures, strict=True):
                it['width'], it['height'] = fut.result()

    def _cache_list(self, key, value):
        """写入列表缓存；超过上限时淘汰最旧一半，防止长时间使用内存膨胀。"""
        self._list_cache[key] = value
        if len(self._list_cache) > self._MAX_LIST_CACHE:
            for k in list(self._list_cache)[:self._MAX_LIST_CACHE // 2]:
                del self._list_cache[k]

    def _flush_meta_if_dirty(self):
        """尺寸元数据有新增条目时落盘一次（列表操作末尾调用）。"""
        if self._meta_dirty:
            self._save_meta()
            self._meta_dirty = False

    def _limit_all_images(self, all_images: List[Dict]) -> tuple:
        """连续浏览序列截断保护：超大相册不全量下发，防止内存/带宽放大。"""
        if len(all_images) > self._MAX_ALL_IMAGES:
            return all_images[:self._MAX_ALL_IMAGES], True
        return all_images, False


    def _get_thumb(self, rel_path: str) -> Path:
        root, inner = self._split_virtual(rel_path)
        base = root if root is not None else self.root_dir
        return ensure_thumbnail(base, inner, self.thumb_dir)


    def list_images(self, rel_path: str = '', page: int = 1,
                    per_page: int = 40, sort_by: str = 'mtime',
                    sort_order: str = 'desc') -> Dict:
        if not self._is_safe(rel_path):
            return {"images": [], "page": 1, "total": 0, "settings": {}}
        try:
            page = max(1, int(page))
            per_page = min(200, max(1, int(per_page)))
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

        target_dir, _ = self._resolve_dir(rel_path)
        images = []
        try:
            with os.scandir(target_dir) as entries:
                for entry in entries:
                    if entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                        stat = entry.stat()
                        images.append({
                            'path': entry.path,
                            'name': entry.name,
                            'mtime': stat.st_mtime,
                            'size': stat.st_size,
                        })
        except (FileNotFoundError, TypeError, AttributeError):
            pass

        # 并行读取尺寸：首次扫描大文件夹时 Pillow 打开文件是主要开销
        self._fill_image_sizes(images)

        for img in images:
            url_path = (Path(rel_path) / img['name']).as_posix()
            img.update({'url': url_path, 'width': img.pop('width'), 'height': img.pop('height')})

        reverse = (sort_order == 'desc')
        if sort_by == 'name':
            images.sort(key=lambda x: natural_sort_key(Path(x['url']).name), reverse=reverse)
        elif sort_by == 'time_name':
            # 新标准「时间+文件名」：图片内部一律按文件名自然序（p0 → p1），
            # 时间维度只作用于作品/相册卡片之间的顶层排序
            images.sort(key=lambda x: natural_sort_key(Path(x['url']).name))
        else:
            images.sort(key=lambda x: x['mtime'], reverse=reverse)

        self._cache_list(cache_key, (dir_mtime, images))
        self._flush_meta_if_dirty()

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

    # ===== 混合瀑布流（只处理一层嵌套）：直接图片 + 直接子相册 p0 瓦片 =====

    def _aggregate_children(self, dir_path: Path, rel_path: str) -> tuple:
        """递归统计目录下的图片总数与代表封面（含更深层的子目录）。

        返回 (总图片数, 代表封面)。直接子目录有图片时取该目录的 p0；只有更深层
        才有图片时**继续往下递归**——否则「作者/作品/1.jpg」这类作品文件夹会被
        算成 0 张，命名空间卡片与空目录判定就都不准了。
        代表封面按 pixiv 号倒序取第一个非空封面（= 最近的作品）。
        """
        children = []
        total = 0
        try:
            with os.scandir(dir_path) as entries:
                for e in entries:
                    if e.name.startswith('.') or e.name == '.cache' or not e.is_dir():
                        continue
                    sub_rel = f"{rel_path}/{e.name}" if rel_path else e.name
                    entry = self._scan_dir_direct(Path(e.path), sub_rel)
                    count = entry.get('direct_count', 0)
                    cover = entry.get('direct_cover', '')
                    if not count:
                        deep_total, deep_cover = self._aggregate_children(Path(e.path), sub_rel)
                        count, cover = deep_total, deep_cover
                    total += count
                    children.append((e.name, cover))
        except OSError:
            pass
        children = _pixiv_sort(children, lambda t: t[0], reverse=True)
        cover = next((c for _n, c in children if c), '')
        return total, cover

    def _scan_one_subdir(self, rel_path: str, name: str, cache_dirs: dict):
        """扫描单个直接子目录（供线程池并行调用），返回 (sub_rel, card, images)。

        cache_dirs / _meta_cache 为进程内共享字典，GIL 下并发读写安全。
        """
        sub_rel = f"{rel_path}/{name}" if rel_path else name
        # 命名空间节点（额外根目录的顶层）：没有直接图片，封面与总数递归聚合
        if self._is_namespace_node(sub_rel):
            root, _ = self._split_virtual(sub_rel)
            try:
                mtime = root.stat().st_mtime
            except OSError:
                return None
            total, cover = self._aggregate_children(root, sub_rel)
            cw = ch = 1
            if cover:
                cover_path, _ = self._resolve_path(cover)
                try:
                    st = cover_path.stat()
                    cw, ch = self._get_image_size(str(cover_path), st.st_mtime)
                except (OSError, AttributeError):
                    pass
            card = {
                'type': 'album', 'path': sub_rel, 'name': name[len(NAMESPACE_MARKER):],
                'cover': cover, 'image_count': 0, 'total_count': total,
                'has_children': True, 'mtime': mtime,
                'width': cw or 1, 'height': ch or 1,
                'use_time_name': self._pixiv_mode(sub_rel),
                'root_scope': True,
            }
            return sub_rel, card, []
        dir_path, _ = self._resolve_dir(sub_rel)
        try:
            mtime = dir_path.stat().st_mtime
        except OSError:
            return None
        # 缓存命中条件除目录 mtime 外还要看 Pixiv 排序标志：封面挑选规则由它决定，
        # 切换排序后旧封面必须重算（缓存里没存图片列表，无法就地重挑）
        cached = cache_dirs.get(sub_rel)
        pixiv = self._pixiv_mode(sub_rel)
        if cached and cached.get('mtime') is not None \
                and abs(float(cached.get('mtime', 0)) - float(mtime)) < 0.5 \
                and cached.get('pixiv') == pixiv:
            entry = cached
        else:
            entry = self._scan_dir_direct(dir_path, sub_rel)
            entry['mtime'] = mtime
            cache_dirs[sub_rel] = entry
        cover = entry.get('direct_cover', '')
        cw, ch = 1, 1
        images = []
        try:
            with os.scandir(dir_path) as entries:
                for e in entries:
                    if e.name.startswith('.') or e.name == '.cache':
                        continue
                    if e.is_file() and Path(e.name).suffix.lower() in ALLOWED_EXTENSIONS:
                        st = e.stat()
                        url = (Path(sub_rel) / e.name).as_posix()
                        w, h = self._get_image_size(e.path, st.st_mtime)
                        images.append({'url': url, 'mtime': st.st_mtime,
                                       'width': w, 'height': h})
                        if url == cover:
                            cw, ch = w, h
        except OSError:
            pass
        direct_count = len(images)
        total_count = direct_count
        if not images and entry.get('has_children'):
            # 纯容器子目录：递归聚合（一层），代表封面 + 总图片数
            agg_total, agg_cover = self._aggregate_children(dir_path, sub_rel)
            total_count = agg_total
            if agg_cover:
                cover = agg_cover
        if cover:
            abs_path, _ = self._resolve_path(cover)
            try:
                st = abs_path.stat()
                cw, ch = self._get_image_size(str(abs_path), st.st_mtime)
            except (OSError, AttributeError):
                pass
        card = {
            'type': 'album',
            'path': sub_rel,
            'name': name,
            'cover': cover,
            'image_count': direct_count,
            'total_count': total_count,
            'has_children': entry.get('has_children', False),
            'mtime': mtime,
            'width': cw or 1,
            'height': ch or 1,
            'use_time_name': pixiv,
            # 合成根（第一根的顶层）与额外根的命名空间节点同地位：前端把它当
            # Pixiv 树的配置点。普通作者目录不算，否则会被误判成配置点而少一层
            'root_scope': rel_path == '' and not name.startswith(NAMESPACE_MARKER),
        }
        return sub_rel, card, images

    def _scan_album_items(self, rel_path: str, sub_dirs: List[str]) -> tuple:
        """扫描每个直接子目录（并行）。

        返回 (卡片列表, {sub_rel: [直接图片 dict 列表]})。
        卡片封面 = p0（文件名自然序第一张）；纯容器子目录（无直接图片但有子文件夹）
        用递归聚合：封面 = 最新作品 p0、total_count = 递归总图片数；
        image_count 始终为直接图片数（连续浏览序列按它展开）。
        """
        cache_dirs = self._album_cache.get('dirs')
        if not isinstance(cache_dirs, dict):
            cache_dirs = self._album_cache['dirs'] = {}
        results = {}
        names = sorted(sub_dirs)
        with ThreadPoolExecutor(max_workers=self._SCAN_WORKERS) as ex:
            futures = {ex.submit(self._scan_one_subdir, rel_path, name, cache_dirs): name
                       for name in names}
            for fut, name in futures.items():
                try:
                    out = fut.result()
                except Exception:
                    out = None
                if out is not None:
                    results[name] = out
        cards = []
        album_images = {}
        for name in names:
            if name not in results:
                continue
            sub_rel, card, images = results[name]
            cards.append(card)
            album_images[sub_rel] = images
        return cards, album_images

    def _item_image_sort_key(self, sort_by: str):
        """图片排序键：time_name 下图片一律按文件名自然序（p0 → p1），
        时间维度只作用于作品/相册卡片之间的顶层排序；其余按设置。"""
        if sort_by == 'time_name':
            return lambda i: natural_sort_key(Path(i['url']).name)
        if sort_by == 'mtime':
            return lambda i: i.get('mtime', 0.0)
        return lambda i: natural_sort_key(Path(i['url']).name)

    def list_folder_items(self, rel_path: str = '', page: int = 1,
                          per_page: int = 40, sort_by: str = 'name',
                          sort_order: str = 'asc') -> Dict:
        """混合列表：直接图片 + 直接子相册 p0 瓦片（只处理一层嵌套），瀑布流统一展示。

        子文件夹以 p0 瓦片返回（封面 = p0，time_name 模式下带圆圈数量角标），
        单图以 image 返回。`all_images` 为按瀑布流顺序展开的连续浏览序列：
        每个子文件夹的图片（p0 → p1 → …）依次展开、随后是单图，
        供灯箱向右连续翻看画师的其他作品（包括文件夹与单图）。
        """
        if not self._is_safe(rel_path):
            return {"items": [], "all_images": [], "page": 1, "total": 0, "settings": {}}
        try:
            page = max(1, int(page))
            per_page = min(200, max(1, int(per_page)))
        except (TypeError, ValueError):
            page, per_page = 1, 40

        cache_key = ('items', rel_path, sort_by, sort_order)
        dir_mtime = self._get_dir_mtime(rel_path)
        if cache_key in self._list_cache:
            cached = self._list_cache[cache_key]
            if cached[0] == dir_mtime:
                cached_items = cached[1]
                cached_all = cached[2] if len(cached) > 2 else None
                total = len(cached_items)
                start = (page - 1) * per_page
                end = start + per_page
                image_total = sum(1 for it in cached_items if it.get('type') == 'image')
                all_offset = self._all_images_offset(cached_items, start)
                all_images, truncated = self._limit_all_images(
                    cached_all if cached_all is not None
                    else [im for im in cached_items if im.get('type') == 'image'])
                return {
                    "items": cached_items[start:end],
                    "all_images": all_images,
                    "all_truncated": truncated,
                    "all_offset": all_offset,
                    "page": page, "total": total, "image_total": image_total,
                    "has_next": end < total, "has_prev": page > 1,
                    "settings": self.get_settings(rel_path)
                }

        target_dir, _ = self._resolve_dir(rel_path)
        sub_dirs = []
        images = []
        if target_dir is not None:
            try:
                with os.scandir(target_dir) as entries:
                    for entry in entries:
                        if entry.name.startswith('.') or entry.name == '.cache':
                            continue
                        if entry.is_dir():
                            sub_dirs.append(entry.name)
                        elif entry.is_file() and Path(entry.name).suffix.lower() in ALLOWED_EXTENSIONS:
                            stat = entry.stat()
                            images.append({
                                'type': 'image', 'path': entry.path, 'name': entry.name,
                                'mtime': stat.st_mtime, 'size': stat.st_size,
                            })
            except OSError:
                pass
        # 相册树的合成根节点：额外根目录在这里以命名空间卡片的形式出现
        # （要用完整虚拟路径 `__<token>`，不能用裸 token —— 那会被当成第一根
        #  下的同名物理目录去扫描，结果直接消失）
        if not rel_path:
            sub_dirs.extend(f'{NAMESPACE_MARKER}{token}' for token in self._roots_index())

        # 并行读取直接图片尺寸（首次扫描大文件夹时的主要开销）
        self._fill_image_sizes(images)
        for img in images:
            img['url'] = (Path(rel_path) / img['name']).as_posix()
            img['width'] = img.pop('width')
            img['height'] = img.pop('height')

        cards, album_images = self._scan_album_items(rel_path, sub_dirs)

        reverse = (sort_order == 'desc')
        if sort_by == 'time_name':
            # Pixiv 排序支持：顶层作品/单图按 pixiv 数字号（前导数字）排序，方向生效
            # （倒序 = 大号在前 = 新作品在前），无数字名排最后；模糊匹配时同号/
            # 无号条目改为整名自然序（先数字再文字），让「日期+标题」也能排对。
            # 纯图片文件夹（作品内部）图片仍按文件名自然序 p0 → p1，不受方向影响。
            fuzzy = self._pixiv_fuzzy_mode(rel_path)
            cards = _pixiv_sort(cards, lambda x: x['name'], reverse, fuzzy)
            if cards:
                images = _pixiv_sort(images, lambda x: Path(x['url']).name, reverse, fuzzy)
            else:
                images.sort(key=lambda x: natural_sort_key(Path(x['url']).name))
            items = cards + images
        else:
            if sort_by == 'mtime':
                cards.sort(key=lambda x: x['mtime'], reverse=reverse)
                images.sort(key=lambda x: x['mtime'], reverse=reverse)
            else:
                cards.sort(key=lambda x: natural_sort_key(x['name']), reverse=reverse)
                images.sort(key=lambda x: natural_sort_key(Path(x['url']).name), reverse=reverse)
            items = cards + images

        # 连续浏览序列：按瀑布流顺序展开；每个子文件夹内部按【它自己生效的设置】排序
        # （自己有设置用自己，没有则逐级继承父级），不受当前视图排序影响——
        # 这样改外层排序不会破坏子文件夹内部已设定好的 p0 → p1 顺序。
        all_images = []
        for it in items:
            if it['type'] == 'image':
                all_images.append({'url': it['url'], 'width': it['width'], 'height': it['height']})
            else:
                sub_settings = self.get_settings(it['path'])
                sub_sort = sub_settings.get('sort_by') or sort_by
                sub_reverse = (sub_settings.get('sort_order') or 'desc') == 'desc'
                sub_imgs = sorted(album_images.get(it['path'], []),
                                  key=self._item_image_sort_key(sub_sort),
                                  reverse=(sub_sort != 'time_name' and sub_reverse))
                for si in sub_imgs:
                    all_images.append({'url': si['url'], 'width': si['width'], 'height': si['height']})

        self._cache_list(cache_key, (dir_mtime, items, all_images))
        self._flush_meta_if_dirty()

        all_images, truncated = self._limit_all_images(all_images)
        total = len(items)
        start = (page - 1) * per_page
        end = start + per_page
        all_offset = self._all_images_offset(items, start)
        return {
            "items": items[start:end],
            "all_images": all_images,
            "all_truncated": truncated,
            "all_offset": all_offset,
            "page": page, "total": total,
            "image_total": len(images),
            "has_next": end < total, "has_prev": page > 1,
            "settings": self.get_settings(rel_path)
        }

    def _all_images_offset(self, items: List[Dict], start: int) -> int:
        """当前页首项之前，连续浏览序列 all_images 中已有多少张图片。

        分页后前端用 items[0] 对应 all_images[all_offset]，
        避免点击第 2+ 页的瓦片时灯箱打开到序列开头的错误图片。
        """
        offset = 0
        for it in items[:start]:
            offset += it.get('image_count', 0) if it.get('type') == 'album' else 1
        return offset

    def list_dir(self, rel_path: str = '') -> List[Dict]:
        """目录树浏览（移动目标选择等）：命名空间根展开为该根的子目录。"""
        if self._is_namespace_node(rel_path):
            root, _ = self._split_virtual(rel_path)
            return [{'name': e.name, 'path': self._virtual_path(root, e.name)}
                    for e in sorted(root.iterdir(), key=lambda p: natural_sort_key(p.name))
                    if e.is_dir() and not e.name.startswith('.')]
        root, inner = self._split_virtual(rel_path)
        if root is None:
            return []
        return list_directory(root, inner)
    # ===== 相册索引（持久化 + 按目录 mtime 增量更新） =====

    def _load_album_cache(self) -> dict:
        if self.album_cache_file.exists():
            try:
                with open(self.album_cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # 版本 4：Pixiv 树封面改为作品号最大的那张（画师最近的作品，
                # 同作品内仍取 p0）；版本 3 缓存里 Pixiv 封面是自然序第一张
                # （= 最老的作品），必须作废重扫。
                # 版本 3：封面改为文件名自然序第一张（p0），比 mtime 封面更稳定。
                if isinstance(data, dict) and data.get('version') == self._ALBUM_CACHE_VERSION:
                    return data
            except Exception:
                pass
        return {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}

    def _save_album_cache(self):
        try:
            self.album_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.album_cache_file, 'w', encoding='utf-8') as f:
                json.dump(self._album_cache, f, ensure_ascii=False)
        except Exception as e:
            log.error(f'[ImageViewer] 保存相册索引失败: {e}')
    def _load_album_config(self) -> dict:
        defaults = {'collapsed': [], 'promoted': [], 'expanded': [],
                    'visible_empty_dirs': []}
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
            log.error(f'[ImageViewer] 保存相册配置失败: {e}')

    def _list_album_dirs(self) -> dict:
        """遍历全部根目录树，返回 {虚拟路径: dir_mtime}。

        第一根的键保持相对路径（与既有相册索引缓存、收藏路径一致），第二根起
        加 `__<命名空间>` 前缀；空字符串键是整棵相册树的合成根节点。
        """
        dirs = {}
        newest = 0.0
        for root in self._roots():
            try:
                newest = max(newest, root.stat().st_mtime)
            except OSError:
                continue
            # 额外根目录的顶层是虚拟命名空间节点，本身不在磁盘上，单独补一条：
            # `_build_albums` 靠它合成「子目录网格」用的容器条目
            if not _same_path(root, self.root_dir):
                dirs[self._virtual_path(root, '')] = root.stat().st_mtime
            for current, dir_names, _files in os.walk(root):
                dir_names[:] = [d for d in dir_names
                                if not d.startswith('.') and d != '.cache']
                current = Path(current)
                if current == root:
                    continue
                try:
                    rel = self._virtual_path(root, current.relative_to(root).as_posix())
                    dirs[rel] = current.stat().st_mtime
                except (OSError, ValueError):
                    continue
        dirs[''] = newest
        return dirs

    def _scan_dir_direct(self, dir_path: Path, rel_path: str) -> dict:
        """只扫描一个目录的直接图片（一次 os.scandir，开销可控）。

        目录名语义（name/depth/parent）按**根内**相对路径计算，所以额外根目录
        下的作者/作品与第一根处在同一层级，两层 Pixiv 布局不会被命名空间顶掉；
        文件 `rel` 仍是虚拟路径（`__命名空间/...`），可直接当作缩略图/图片 URL。
        """
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
        # 封面：Pixiv 树取作品号最大的一张（p0），其余取文件名自然序第一张；
        # 相册新旧仍按最新 mtime 计算
        newest = max((img['mtime'] for img in images), default=0.0)
        pixiv = self._pixiv_mode(rel_path)
        cover = _pick_cover(images, pixiv)
        root, inner = self._split_virtual(rel_path)
        parent = self._virtual_path(root, '/'.join(inner.split('/')[:-1])) if inner else None
        return {
            'path': rel_path,
            'name': dir_path.name,
            'depth': inner.count('/') + (1 if inner else 0),
            'parent': parent,
            'direct_count': len(images),
            'direct_cover': cover,
            'direct_mtime': newest,
            'pixiv': pixiv,      # 封面挑选依据，缓存复用时要核对（见 _build_albums）
            'has_children': len(children) > 0,
            'children': sorted(children),
        }

    def _build_albums(self, dirs: dict, cache_dirs: dict) -> tuple:
        """直接扫描变化目录，再自底向上聚合出递归统计。

        `dirs` 是全部根目录的 {虚拟路径: mtime}（见 `_list_album_dirs`）。额外根
        目录的顶层节点是**虚拟**的（`__<命名空间>`），磁盘上不存在，这里为它
        合成一个只含 children 的条目，使聚合循环与第一根走同一条路径。
        """
        cache_dirs = cache_dirs or {}
        entries = {}
        changed = 0
        pixiv_flags = {rel: self._pixiv_mode(rel)
                       for rel in dirs}
        # 先建合成根条目：它的 children 要包含命名空间节点，聚合循环才把
        # 额外根目录的图片算进相册树总数（albums[''].image_count）
        synthetic_children: List[str] = []
        for rel, mtime in dirs.items():
            if self._is_namespace_node(rel) or rel != '':
                continue
            root_entry = cache_dirs.get('') if isinstance(cache_dirs.get(''), dict) else None
            if root_entry and root_entry.get('mtime') is not None \
                    and abs(float(root_entry.get('mtime', 0)) - float(mtime)) < 0.5 \
                    and not root_entry.get('virtual'):
                entries[''] = root_entry
            else:
                entry = self._scan_dir_direct(self.root_dir, '')
                entry['mtime'] = mtime
                entries[''] = entry
                changed += 1
            break
        for rel, mtime in dirs.items():
            if self._is_namespace_node(rel):
                # 虚拟命名空间节点：没有直接图片，children = 该根的一级子目录
                root, _ = self._split_virtual(rel)
                children = []
                if root is not None:
                    try:
                        with os.scandir(root) as it:
                            children = sorted(
                                e.name for e in it
                                if e.is_dir() and not e.name.startswith('.')
                                and e.name != '.cache')
                    except OSError:
                        pass
                entries[rel] = {
                    'path': rel, 'name': rel[len(NAMESPACE_MARKER):],
                    'depth': 0, 'parent': None,
                    'direct_count': 0, 'direct_cover': '', 'direct_mtime': 0.0,
                    'pixiv': False, 'has_children': bool(children),
                    'children': children, 'mtime': mtime, 'virtual': True,
                }
                synthetic_children.append(rel)   # 完整虚拟路径（含 `__` 前缀）
                continue
            if rel == '':
                continue      # 合成根已在上面建好
            # 缓存命中条件除目录 mtime 外还要看 Pixiv 排序标志：封面挑选规则由它
            # 决定（作品号最大 vs 自然序第一张），切换排序后旧封面必须重扫
            cached = cache_dirs.get(rel)
            if cached and cached.get('mtime') is not None \
                    and abs(float(cached.get('mtime', 0)) - float(mtime)) < 0.5 \
                    and cached.get('pixiv') == pixiv_flags[rel] \
                    and not cached.get('virtual'):
                entries[rel] = cached
                continue
            dir_path, _ = self._resolve_dir(rel)
            if dir_path is None:
                continue
            entry = self._scan_dir_direct(dir_path, rel)
            entry['mtime'] = mtime
            entries[rel] = entry
            changed += 1
        if synthetic_children and entries.get(''):
            entries['']['children'] = sorted({*entries['']['children'],
                                             *synthetic_children})

        # 自底向上聚合 image_count / cover / mtime。
        # 同深度时命名空间节点（virtual）先算：它与合成根同为 depth 0，而合成根
        # 要把额外根目录的图片并进相册树总数，必须等它们的结果先出来。
        ordered = sorted(entries.values(),
                         key=lambda e: (e['depth'], 1 if e.get('virtual') else 0),
                         reverse=True)
        totals = {}
        for entry in ordered:
            rel = entry['path']
            pixiv = pixiv_flags[rel]
            total = entry['direct_count']
            cover = entry['direct_cover']
            newest = entry['direct_mtime']
            rank = _cover_rank(cover, newest, pixiv)
            for child_name in entry['children']:
                child_rel = f"{rel}/{child_name}" if rel else child_name
                child_totals = totals.get(child_rel)
                if not child_totals:
                    continue
                total += child_totals[0]
                if child_totals[2] > newest:
                    newest = child_totals[2]
                # 封面：Pixiv 树取作品号最大的子目录（画师最近的作品），
                # 非 Pixiv 目录仍是 mtime 最新的那个；mtime 聚合不受影响
                if child_totals[3] > rank:
                    rank = child_totals[3]
                    cover = child_totals[1]
            totals[rel] = (total, cover, newest, rank)

        albums = []
        for entry in entries.values():
            total, cover, newest, _rank = totals[entry['path']]
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
                'use_time_name': pixiv_flags[entry['path']],
                # 额外根目录的顶层节点：前端把它当作 Pixiv 树的配置点，
                # 与第一根（path == ''）地位一致
                'root_scope': bool(entry.get('virtual')),
                # 递归可读图片数（= image_count）：0 表示整棵下级都没有可读图片，
                # 前端据此隐藏空目录
                'readable': total,
            })

        # 不再预生成所有封面缩略图：交给 /thumbs 按需生成，避免上万次随机小文件 I/O。
        return albums, entries, changed

    def list_albums(self) -> Dict:
        """相册列表：目录 mtime 未变时直接复用持久化索引，避免每次重启全量扫描。

        全树目录枚举（os.walk）在大型图库上耗时数秒，结果带 30 秒 TTL 缓存：
        频繁进入相册页直接返回上次结果；手动刷新（refresh）与全量重建会强制重扫。
        """
        now = time.time()
        if self._albums_cached is not None \
                and (now - self._albums_cached_at) < self._ALBUMS_TTL:
            return {**self._albums_cached, 'cached': True}
        if self._prune_visible_marks():
            self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}
        dirs = self._list_album_dirs()
        albums, entries, changed = self._build_albums(dirs, self._album_cache.get('dirs', {}))
        self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': entries}
        if changed:
            self._save_album_cache()
        result = {'albums': albums, 'config': self._album_config, 'changed': changed}
        self._albums_cached_at = now
        self._albums_cached = result
        return result

    def _invalidate_albums_cache(self):
        """让 list_albums 下一次调用强制重扫（刷新/重建/相册配置变更时调用）。"""
        self._albums_cached_at = 0.0
        self._albums_cached = None

    def get_album_config(self) -> Dict:
        return self._album_config

    def set_album_config(self, rel_path: str, action: str) -> Dict:
        """album 层级控制：collapse/expand（收纳/展开子相册）、promote/unpromote（提升到全部相册）。

        子相册**默认折叠**（见前端 `_isCollapsed`），所以「展开」要落进 `expanded`
        白名单；「收纳」则撤销展开并记进 `collapsed`，覆盖历史配置。
        """
        rel_path = (rel_path or '').strip().strip('/')
        collapsed = set(self._album_config.get('collapsed', []))
        promoted = set(self._album_config.get('promoted', []))
        expanded = set(self._album_config.get('expanded', []))
        if action == 'collapse':
            collapsed.add(rel_path)
            expanded.discard(rel_path)
        elif action == 'expand':
            collapsed.discard(rel_path)
            expanded.add(rel_path)
        elif action == 'promote':
            promoted.add(rel_path)
        elif action == 'unpromote':
            promoted.discard(rel_path)
        else:
            return {'success': False, 'error': f'未知操作: {action}'}
        self._album_config = {
            **self._album_config,
            'collapsed': sorted(collapsed),
            'promoted': sorted(promoted),
            'expanded': sorted(expanded),
        }
        self._save_album_config()
        self._invalidate_albums_cache()
        return {'success': True, 'config': self._album_config}

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
        """
        rel_path = (rel_path or '').replace('\\', '/').strip('/')
        if not rel_path or self._is_namespace_node(rel_path):
            return {'success': False, 'error': '路径非法'}
        target, _ = self._resolve_dir(rel_path)
        if target is None:
            return {'success': False, 'error': '路径非法'}
        if not target.is_dir():
            return {'success': False, 'error': '目录不存在'}
        try:
            for _current, _dirs, files in os.walk(target):
                if any(not f.startswith('.') and Path(f).suffix.lower() in ALLOWED_EXTENSIONS
                       for f in files):
                    return {'success': False, 'error': '目录内还有图片，请先删除图片'}
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

    def _collect_all_images(self, rel_path: str = '') -> List[str]:
        """收集整个相册树（或指定子文件夹）下所有需要生成缩略图的图片虚拟路径。

        空路径 = 全部根目录；命名空间节点 = 只收集该额外根目录。
        """
        rel_path = (rel_path or '').strip().strip('/')
        pairs = []
        if not rel_path:
            pairs = [(root, '') for root in self._roots()]
        else:
            root, inner = self._split_virtual(rel_path)
            if root is not None:
                pairs = [(root, inner)]
        images = []
        for root, inner in pairs:
            base_dir = root / inner if inner else root
            prefix = self._virtual_path(root, inner)
            try:
                for current, dir_names, filenames in os.walk(base_dir):
                    dir_names[:] = [d for d in dir_names
                                    if not d.startswith('.') and d != '.cache']
                    current_path = Path(current)
                    rel_dir = ('' if current_path == base_dir
                               else current_path.relative_to(base_dir).as_posix())
                    for name in filenames:
                        if name.startswith('.'):
                            continue
                        if Path(name).suffix.lower() in ALLOWED_EXTENSIONS:
                            parts = [p for p in (prefix, rel_dir, name) if p]
                            images.append('/'.join(parts))
            except OSError:
                pass
        return images

    def rebuild_all(self, rel_path: str = '', force: bool = True) -> Dict:
        """全量/指定文件夹重建。

        - rel_path 非空时：只重建该文件夹，且不会清空已有缩略图（增量补齐）。
        - rel_path 为空且 force=True 时：清空旧缓存后全量重新生成。
        - rel_path 为空且 force=False 时：全库增量补齐，跳过已有有效缩略图。
        """
        rel_path = (rel_path or '').strip().strip('/')
        if rel_path and not self._is_safe(rel_path):
            return {'started': False, 'success': False, 'error': '非法路径'}
        if self._rebuild and self._rebuild.state == 'running':
            return {'started': False, 'running': True, **self.rebuild_status()}

        self._list_cache.clear()
        if not rel_path and force:
            self._invalidate_albums_cache()
            self._meta_cache = {}
            try:
                if self.meta_file.exists():
                    self.meta_file.unlink()
            except OSError:
                pass
            self.thumb_cache.clear()
            # 旧版散文件缩略图目录已不再使用，全量重建时一并清理。
            try:
                if self.thumb_dir.exists():
                    shutil.rmtree(self.thumb_dir)
            except OSError:
                pass
            self.thumb_dir.mkdir(parents=True, exist_ok=True)
            self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}
            try:
                if self.album_cache_file.exists():
                    self.album_cache_file.unlink()
            except OSError:
                pass

        images = self._collect_all_images(rel_path)
        task = BackgroundTask(kind='rebuild', extra={'rebuild_path': rel_path})
        task.update(total=len(images))
        task.start(self._rebuild_worker, args=(images,))
        self._rebuild = task
        return {'started': True, 'running': True, 'total': len(images)}

    def rebuild_folder(self, rel_path: str) -> Dict:
        """只重建指定文件夹下的缩略图（增量补齐，不清空已有缓存）。"""
        return self.rebuild_all(rel_path=rel_path, force=False)

    def _rebuild_worker(self, task: BackgroundTask, images: List[str]):
        """全量重建 worker：批量并行生成缩略图到 ThumbCache。"""
        def progress(processed, total, current, errors):
            task.update(processed=processed, total=total, current=current, errors=errors)

        try:
            items = [(rel, self.root_dir / rel) for rel in images]
            result = self.thumb_cache.generate_bulk(
                items,
                progress_cb=progress,
                stop_event=task.stop_event,
            )
            task.update(processed=result['processed'], errors=result['errors'])
        except Exception as e:
            task.add_error(f'重建异常: {e}')
        finally:
            task.update(current='')

    def rebuild_status(self) -> Dict:
        """返回全量重建后台任务的进度。"""
        task = self._rebuild
        if not task:
            return {
                'running': False,
                'done': False,
                'success': False,
                'cancelled': False,
                'rebuild_path': '',
                'total': 0,
                'processed': 0,
                'current': '',
                'error_count': 0,
                'errors': [],
            }
        status = task.status()
        return {
            'running': status['running'],
            'done': status['done'],
            'success': status['success'],
            'cancelled': status['cancelled'],
            'rebuild_path': status['extra'].get('rebuild_path', ''),
            'total': status['total'],
            'processed': status['processed'],
            'current': status['current'],
            'error_count': status['error_count'],
            'errors': status['errors'],
        }

    def rebuild_cancel(self) -> Dict:
        """请求取消当前全量重建任务；已生成的缩略图会保留。"""
        if self._rebuild and self._rebuild.state == 'running':
            self._rebuild.cancel()
            return {'success': True}
        return {'success': False, 'error': '没有正在运行的重建任务'}

    def on_unload(self) -> None:
        """进程退出收尾：取消正在跑的缩略图重建任务（已生成的缩略图保留）。"""
        if self._rebuild and self._rebuild.state == 'running':
            self._rebuild.cancel()

    def get_settings(self, rel_path: str = '') -> Dict:
        """获取文件夹生效设置（含全局回退与逐级继承）。

        文件夹设置按「当前文件夹 → 父文件夹 → … → 全局 → 硬默认」逐级
        向上继承：在父文件夹（如 pixiv）上启用 time_name 后，其下所有子文件夹
        自动继承同一排序；某个子文件夹被单独修改（folders[该路径] 存在）时
        以它自己的设置优先，并继续向其子文件夹传播。

        `pixiv_explicit`：当前文件夹自身是否显式启用了 Pixiv 树（自己存了
        「Pixiv 排序支持」或「模糊匹配」）。它是 Pixiv 树的配置点——该层显示
        作者网格，继承它的子层才显示瀑布流；所以对「作者/作品名/序号.jpg」
        这类非数字命名的目录，勾一次模糊匹配就够，不必每个子目录再配一遍。
        """
        folders = self.setting('folders') or {}
        if not isinstance(folders, dict):
            folders = {}
        # 全局设置有两个来源：
        #   1. 插件级键（row_height / per_page / sort_by / …）——壳设置面板与
        #      save_folder_settings('') 走 PluginBase.save_settings() 写入，是当前写入路径；
        #   2. folders['__global__']——更早的兼容位置，当前代码不再写入。
        # 历史缺陷：这里只读 2，于是"保存到全局"当场生效、重启后静默回落到硬默认值。
        # 两者都读，但**以当前写入路径为准**：只把真正存过的插件级键（设置存储或
        # 启动时预设里有这个键）算进来，不能用 self.setting() 的 schema 默认值去
        # 覆盖 __global__，否则老配置里的全局值会被默认值悄悄顶掉。
        stored = {}
        if self._settings_store:
            raw = self._settings_store.get(self.name)
            if isinstance(raw, dict):
                stored.update(raw)
        if isinstance(self._resolved_config, dict):
            for key, value in self._resolved_config.items():
                stored.setdefault(key, value)
        global_settings = {}
        legacy = folders.get('__global__')
        if isinstance(legacy, dict):
            global_settings.update(legacy)
        for item in self.settings_schema:
            key = item.get('key')
            if not key or key == 'root_dir':
                continue
            if key in stored and stored[key] is not None:
                global_settings[key] = stored[key]
        hard_defaults = {
            "row_height": 200,
            "per_page": 40,
            "sort_by": "mtime",
            "sort_order": "desc",
            "pixiv_fuzzy": False,
            "album_sort_by": "mtime",
            "album_sort_order": "desc"
        }
        folder_settings = {}
        # 命名空间节点（额外根目录的顶层）与第一根的合成根同地位：它没有自己的
        # 文件夹级设置，直接吃全局值，成为该根下作者层的继承源头。
        parts = [] if self._is_namespace_node(rel_path) \
            else [p for p in (rel_path or '').split('/') if p]
        for i in range(len(parts), 0, -1):
            key = '/'.join(parts[:i])
            if key in folders:
                folder_settings = folders[key]
                break
        result = {**hard_defaults, **global_settings, **folder_settings}
        result['root_dir'] = str(self.root_dir)
        # 配置点 = 当前文件夹自身显式启用了 Pixiv 树（自己存了排序方式或
        # 模糊匹配）。继承来的不算：所以「在作者目录上勾模糊匹配」就足以把
        # 该层变成作者网格，而它下面的作品层继承后仍走瀑布流。
        own_entry = folders.get(rel_path or '__global__', {})
        result['pixiv_explicit'] = bool(own_entry.get('pixiv_fuzzy')) \
            or own_entry.get('sort_by') == 'time_name'
        return result

    def save_folder_settings(self, rel_path: str = '', settings: Dict | None = None) -> Dict:
        """前端 save_settings 的 API 实现。

        兼容两种调用：
        - save_folder_settings(settings_dict)      → 保存全局
        - save_folder_settings(rel_path, settings) → 保存指定文件夹
        """
        if settings is None:
            if isinstance(rel_path, dict):
                settings = rel_path
                rel_path = ''
            else:
                settings = {}
        if rel_path:
            # 文件夹级设置只保存视图/排序偏好；root_dir 是全局设置，不能写入某个文件夹，
            # 否则会造成“看起来保存了但根目录没变”的困惑。
            folder_settings = dict(settings or {})
            folder_settings.pop('root_dir', None)
            folders = self.setting('folders') or {}
            if not isinstance(folders, dict):
                folders = {}
            folders[rel_path] = folder_settings
            self.update_setting('folders', folders)
            self._invalidate_albums_cache()
            return {"success": True}
        result = super().save_settings(settings)
        if result.get('success'):
            self._invalidate_albums_cache()
        return result

    def on_settings_changed(self, changed_keys):
        tracked = {'root_dir', 'extra_roots'}
        if tracked & set(changed_keys):
            rooted = 'root_dir' in changed_keys
            new_dir = self.setting('root_dir')
            if rooted and new_dir and Path(new_dir).is_dir():
                self.root_dir = Path(new_dir).resolve()
            self._rebuild_paths()
            if rooted:
                # 缓存/缩略图库/命名空间都挂在第一根下，换根后全部重来
                self.thumb_cache = ThumbCache(self.thumb_db_path)
                self._meta_cache = self._load_meta()
            self._album_cache = {'version': self._ALBUM_CACHE_VERSION, 'dirs': {}}
            self._list_cache.clear()
            self._invalidate_albums_cache()
        # 排序/封面规则变更（壳设置面板走的是 PluginBase.save_settings，不经过
        # save_folder_settings）也要立即生效，而不是等 30s TTL 过期
        if {'sort_by', 'sort_order', 'pixiv_fuzzy',
                'album_sort_by', 'album_sort_order'} & set(changed_keys):
            self._list_cache.clear()
            self._invalidate_albums_cache()

    def clear_folder_settings(self, rel_path: str) -> Dict:
        """删除指定文件夹的独立设置，使其回退到全局设置"""
        folders = self.setting('folders') or {}
        if isinstance(folders, dict) and rel_path in folders:
            del folders[rel_path]
            self.update_setting('folders', folders)
            self._invalidate_albums_cache()
        return {"success": True}
