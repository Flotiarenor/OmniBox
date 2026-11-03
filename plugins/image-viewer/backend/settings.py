"""按目录的设置读取与逐级继承。

状态自持：设置来源缓存、按路径的结果缓存、Pixiv 标记缓存都在这里；写入路径负责让
它们失效。读取属于本插件的业务规则（逐级继承、配置点判定），写入则一律交给宿主：
全局键走 `PersistPort.save_settings`（schema 校验、字段级错误、凭据掩码、限权都在
那里），文件夹级键存进 `folders` 子树。

为什么"按目录读设置"叫 `for_path` 而不是 `get_settings`：`PluginBase.get_settings()`
是壳与设置弹窗依赖的基类契约（无参数、返回已脱敏的全局设置）。旧实现用同名方法覆盖
了它，签名完全不同 —— 今天没出事只是因为本插件没有 `secret` 键。改名后基类的脱敏与
默认值语义重新生效。

为什么设置来源要自己缓存：`SettingsStore.get()` 每次 `open() + json.load()` 读盘，
而 `for_path()` 一条路径要查它三次；相册索引重建会对全树每个目录问一次
（11 万张 / 9168 个目录实测 7.4 秒，其中大部分是 9168×3 次读盘）。缓存由写入路径
（本组件的 `save_folder` / `clear_folder`、入口的 `on_settings_changed`）作废。
"""

import logging
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from shell.backend.plugin_base import parse_setting_number, validate_number_setting
from shell.backend.ports import PersistPort, SettingsPort

log = logging.getLogger(__name__)


class SettingsResolver:
    """按目录生效的设置（含全局回退与逐级继承）。"""

    def __init__(self, paths: Any, settings: SettingsPort, persist: PersistPort,
                 read_raw: Callable[[], Dict[str, Any]],
                 schema: Sequence[Dict[str, Any]],
                 hard_defaults: Mapping[str, Any],
                 on_written: Callable[[], None]) -> None:
        #: 需要 `virtual_path` / `is_namespace_node` 与 `root`：路径命名空间会影响设置继承，
        #: 结果里的 `root_dir` 也取自这里。
        self._paths = paths
        self._settings = settings
        self._persist = persist
        #: 未脱敏的设置来源（启动预设 ∪ 设置文件）。组件不直接碰宿主的设置存储，
        #: 由入口提供一个读取闭包 —— 这是唯一需要"绕过 setting() 默认值"的地方。
        self._read_raw = read_raw
        self._schema = schema
        self._hard_defaults = hard_defaults
        #: 文件夹级写入之后要让相册索引重来（排序/封面规则可能变了）。
        self._on_written = on_written
        self._source_cache: Optional[Dict[str, Any]] = None
        self._by_path: Dict[str, Dict[str, Any]] = {}
        self._pixiv_flags: Optional[Dict[str, bool]] = None

    # ===== 缓存 =====

    def invalidate(self) -> None:
        """设置变了：来源缓存、按路径结果、Pixiv 标记整份作废。"""
        self._source_cache = None
        self._by_path = {}
        self._pixiv_flags = None

    def _source(self) -> Dict[str, Any]:
        """本插件的原始设置字典（带缓存）。

        顺序是"启动时已解决的设置先铺底、当前设置文件覆盖"，不能反过来：反过来说
        启动时那份快照会永久盖住磁盘上的新值 —— 表现为"改了全局排序，界面和索引
        算出来的 Pixiv 标记对不上"。
        """
        cached = self._source_cache
        if cached is not None:
            return cached
        data = dict(self._read_raw())
        self._source_cache = data
        return data

    def _folders(self) -> Dict[str, Any]:
        """`folders` 设置（按目录的设置树）。与 `_source()` 同一份缓存来源。"""
        folders = self._source().get('folders') or {}
        return folders if isinstance(folders, dict) else {}

    @property
    def stats(self) -> Dict[str, int]:
        """诊断用：按路径结果缓存当前有多少条。"""
        return {'settings_by_path': len(self._by_path)}

    # ===== Pixiv 标记 =====

    def pixiv_mode(self, rel_path: str = '') -> bool:
        """该目录生效的排序是否为「Pixiv 排序支持」（含逐级继承）。

        封面与排序的 Pixiv 规则只由排序方式决定，模糊匹配不参与：它只把显式勾选它的
        目录认定为 Pixiv 树的配置点（见 `for_path` 的 `pixiv_explicit`），让
        「作者/作品名/序号.jpg」这类非数字命名的目录也拿到两层浏览效果。

        结果按目录缓存：`_build_albums` 会对全树每个目录问一次，未缓存时每次都要走
        一遍 `for_path` 的继承链与设置来源查询 —— 9168 个目录实测 3.3s，而它只要
        一个布尔值。
        """
        flags = self._pixiv_flags
        if flags is None:
            flags = self._pixiv_flags = {}
        cached = flags.get(rel_path)
        if cached is not None:
            return cached
        value = self.for_path(rel_path).get('sort_by') == 'time_name'
        flags[rel_path] = value
        return value

    def pixiv_fuzzy_mode(self, rel_path: str = '') -> bool:
        """该目录是否在「Pixiv 排序支持」下还勾了「模糊匹配」。"""
        settings = self.for_path(rel_path)
        return settings.get('sort_by') == 'time_name' and bool(settings.get('pixiv_fuzzy'))

    def compute_pixiv_flags(self, paths) -> Dict[str, bool]:
        """一次算出一批目录的"是否 Pixiv 树"，**不逐个查设置**。

        同一份答案用 `pixiv_mode()` 逐个问的话，每个目录都要走一遍 `for_path` 的继承
        链与设置来源查询：9168 个目录实测 2.7-3.4 秒。这里改成自顶向下算一遍：

        - 排序只由「当前目录 → 父目录 → … → 全局」里**最近一个显式设置了 sort_by
          的层**决定（`for_path` 的继承语义），因此按路径长度升序处理，父层算过
          子层就能直接继承；
        - 只对"自己设置了 sort_by"的目录取设置（本机配置里是 5 个），其余全是字典
          查表。

        结果同时写进 `_pixiv_flags`，后续 `pixiv_mode()` 直接命中。
        """
        folders = self._folders()
        global_sort = self._global_sort_by()
        # 有效排序字符串的传递表：**必须存字符串而不是布尔值** —— 拿布尔值当
        # "有效排序"往下传，子层比较 `True == 'time_name'` 会恒为 False，于是
        # 继承链在第二层就断掉（曾经写错过一次，表现为 9027 个目录里只认出 3 个）。
        effective_sort: Dict[str, str] = {}
        scope = set(str(p) for p in paths)
        scope.update(str(rel) for rel in folders)
        for rel in sorted(scope, key=lambda p: (p.count('/'), p)):
            own = folders.get(rel)
            if isinstance(own, dict) and own.get('sort_by'):
                effective = str(own['sort_by'])
            else:
                effective = effective_sort.get(rel.rpartition('/')[0], global_sort)
            effective_sort[rel] = effective
        self._pixiv_flags = {rel: (value == 'time_name')
                             for rel, value in effective_sort.items()}
        return self._pixiv_flags

    def _global_sort_by(self) -> str:
        """全局生效的排序方式（`for_path('')` 的 sort_by）。"""
        stored = self._source()
        value = stored.get('sort_by')
        if value:
            return str(value)
        legacy = self._folders().get('__global__')
        if isinstance(legacy, dict) and legacy.get('sort_by'):
            return str(legacy['sort_by'])
        for item in self._schema:
            if item.get('key') == 'sort_by':
                return str(item.get('default') or 'mtime')
        return 'mtime'

    # ===== 按目录读取 =====

    def for_path(self, rel_path: str = '') -> Dict:
        """获取文件夹生效设置（含全局回退与逐级继承）。

        结果按 `rel_path` 缓存：这条路径在"列一次目录"与"重建全树索引"里都是每个目录
        调一次，未缓存时 9168 次纯 Python 字典合并实测 3.4s。

        文件夹设置按「当前文件夹 → 父文件夹 → … → 全局 → 硬默认」逐级向上继承：在父
        文件夹（如 pixiv）上启用 time_name 后，其下所有子文件夹自动继承同一排序；某个
        子文件夹被单独修改（folders[该路径] 存在）时以它自己的设置优先，并继续向其子
        文件夹传播。

        `pixiv_explicit`：当前文件夹自身是否显式启用了 Pixiv 树（自己存了「Pixiv 排序
        支持」或「模糊匹配」）。它是 Pixiv 树的配置点 —— 该层显示作者网格，继承它的子层
        才显示瀑布流；所以对「作者/作品名/序号.jpg」这类非数字命名的目录，勾一次模糊
        匹配就够，不必每个子目录再配一遍。
        """
        hit = self._by_path.get(rel_path)
        if hit is not None:
            return dict(hit)          # 拷贝：调用方可能改返回值（如补 root_dir）
        result = self._compute(rel_path)
        self._by_path[rel_path] = result
        return dict(result)

    def _compute(self, rel_path: str) -> Dict:
        """`for_path` 的计算本体（结果会被缓存）。"""
        folders = self._settings.setting('folders') or {}
        if not isinstance(folders, dict):
            folders = {}
        # 全局设置有两个来源：
        #   1. 插件级键（row_height / per_page / sort_by / …）——壳设置面板与
        #      save_folder('') 走 PluginBase.save_settings() 写入，是当前写入路径；
        #   2. folders['__global__']——更早的兼容位置，当前代码不再写入。
        # 历史缺陷：这里只读 2，于是"保存到全局"当场生效、重启后静默回落到硬默认值。
        # 两者都读，但**以当前写入路径为准**：只把真正存过的插件级键（设置存储或启动
        # 时预设里有这个键）算进来，不能用 setting() 的 schema 默认值去覆盖
        # __global__，否则老配置里的全局值会被默认值悄悄顶掉。
        stored = dict(self._source())
        global_settings: Dict[str, Any] = {}
        legacy = folders.get('__global__')
        if isinstance(legacy, dict):
            global_settings.update(legacy)
        for item in self._schema:
            key = item.get('key')
            if not key or key == 'root_dir':
                continue
            if key in stored and stored[key] is not None:
                global_settings[key] = stored[key]
        folder_settings: Dict[str, Any] = {}
        # 命名空间节点（额外根目录的顶层）与第一根的合成根同地位：它没有自己的文件夹级
        # 设置，直接吃全局值，成为该根下作者层的继承源头。
        parts = [] if self._paths.is_namespace_node(rel_path) \
            else [p for p in (rel_path or '').split('/') if p]
        for index in range(len(parts), 0, -1):
            key = '/'.join(parts[:index])
            if key in folders:
                folder_settings = folders[key]
                break
        result = {**self._hard_defaults, **global_settings, **folder_settings}
        result['root_dir'] = str(self._paths.root)
        # 配置点 = 当前文件夹自身显式启用了 Pixiv 树（自己存了排序方式或模糊匹配）。
        # 继承来的不算：所以「在作者目录上勾模糊匹配」就足以把该层变成作者网格，而它
        # 下面的作品层继承后仍走瀑布流。
        own_entry = folders.get(rel_path or '__global__', {})
        result['pixiv_explicit'] = bool(own_entry.get('pixiv_fuzzy')) \
            or own_entry.get('sort_by') == 'time_name'
        return result

    # ===== 写入 =====

    def save_folder(self, rel_path: Any = '', settings: Optional[Dict] = None) -> Dict:
        """前端 save_settings 的 API 实现。

        兼容两种调用：
        - save_folder(settings_dict)      → 保存全局
        - save_folder(rel_path, settings) → 保存指定文件夹
        """
        if settings is None:
            if isinstance(rel_path, dict):
                settings = rel_path
                rel_path = ''
            else:
                settings = {}
        if rel_path:
            return self._save_folder_level(rel_path, settings or {})
        result = self._persist.save_settings(settings or {})
        if result.get('success'):
            self.invalidate()
            self._on_written()
        return result

    def _save_folder_level(self, rel_path: str, submitted: Dict) -> Dict:
        # 文件夹级设置只保存视图/排序偏好；root_dir / extra_roots 决定 `/file` 的允许根，
        # 是全局设置，不能写进某个文件夹。
        folders = dict(self._folders())
        existing = dict(folders.get(rel_path) or {})
        existing.pop('root_dir', None)
        existing.pop('extra_roots', None)
        # 与 PluginBase.save_settings 同一套 number/range 校验：任一字段非法则整次不
        # 落盘，返回可直接展示的字段级 errors（前端 `_saveStep` 会据此保持弹窗并显示
        # 错误）。已生效的同值原样回传不拒，避免表单整体提交时被历史越界值卡住 ——
        # 与基类的口径一致。
        effective = self._compute(rel_path)
        errors: Dict[str, str] = {}
        for item in self._schema:
            key = item.get('key')
            if not key or key not in submitted or key in ('root_dir', 'extra_roots'):
                continue
            if item.get('type') in ('number', 'range'):
                parsed = parse_setting_number(submitted[key])
                old_parsed = parse_setting_number(effective.get(key))
                if parsed is not None and old_parsed is not None and parsed == old_parsed:
                    existing[key] = parsed
                    continue
                message, normalized = validate_number_setting(item, submitted[key])
                if message:
                    errors[str(key)] = message
                else:
                    existing[key] = normalized
            else:
                existing[key] = submitted[key]
        if errors:
            return {
                'success': False,
                'error': '；'.join(errors.values()),
                'errors': errors,
            }
        folders[rel_path] = existing
        self._persist.update_setting('folders', folders)
        self.invalidate()
        self._on_written()
        return {'success': True}

    def clear_folder(self, rel_path: str) -> Dict:
        """删除指定文件夹的独立设置，使其回退到全局设置。"""
        folders = self._settings.setting('folders') or {}
        if isinstance(folders, dict) and rel_path in folders:
            del folders[rel_path]
            self._persist.update_setting('folders', folders)
            self.invalidate()
            self._on_written()
        return {'success': True}
