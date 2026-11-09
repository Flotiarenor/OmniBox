"""相册树的一次全树构建：目录集合 + 目录级条目 → 相册列表。

为什么单独一个组件：`AlbumIndex` 管的是"索引什么时候该重算、存到哪"（TTL、版本、
落盘、代次），而"一次重建怎么从目录集合算出相册树"是纯计算 —— 输入是 `{虚拟路径:
mtime}` 与目录级条目缓存，输出是相册列表与写回后的条目。混在一个文件里时，改聚合
（计数、封面、单元数）会牵动缓存语义，反过来也一样。

全树聚合的规则（决定相册树的计数与封面）：

- 直接子目录有图片就取该目录的 p0；只有更深层才有图片就继续往下递归（否则
  「作者/作品/1.jpg」会被算成 0 张）；
- 代表封面按 Pixiv 作品号倒序取第一个非空封面（= 最近的作品）；非 Pixiv 目录取 mtime
  最新、同 mtime 比名字；封面自身的 mtime 跟着封面一起上浮，它是前端缩略图 URL 的
  版本号（替换同名封面时目录 mtime 不变，只有它会变）；
- 同深度时命名空间节点（虚拟）先算：它与合成根同为 depth 0，而合成根要把额外根目录的
  图片并进相册树总数，必须等它们的结果先出来；
- 除图片张数外还聚合**更新单元数**（`unit_count`）：同一目录里同作品号的多页、连号页码
  的一本书在时间线里只占一张卡，所以单元数要像张数一样自底向上汇总，且两者必须用同一份
  分组实现（`naming.unit_groups`），否则「最近更新」的单元总数与扫描出来的单元数会对不上。

状态自持：无（只读注入的组件，每次构建新建字典返回）。缩略图**不预生成**：预生成上万次
随机小文件 I/O 得不偿失，交给 `/thumbs` 按需生成。
"""

import os
import time
from typing import Any, Dict, List, Tuple


class AlbumTreeBuilder:
    """把目录集合与目录级条目聚合成相册树（每次重建调用一次）。

    依赖全部由入口注入：目录级条目存储（按需扫描与缓存）、设置解析（Pixiv 标志）、
    命名规则（封面排序键、更新单元分组）与漫画书范围（哪些目录整本算一个单元）。
    """

    def __init__(self, paths: Any, entries: Any, settings: Any, naming: Any,
                 books: Any = None) -> None:
        self._paths = paths
        self._entries = entries
        self._settings = settings
        self._naming = naming
        self._books = books

    def build(self, dirs: dict, cache_dirs: dict
              ) -> Tuple[List[Dict], Dict, int, Dict[str, Any]]:
        """直接扫描变化目录，再自底向上聚合出递归统计。

        `dirs` 是全部根目录的 {虚拟路径: mtime}。额外根目录的顶层节点是**虚拟**的
        （`__<命名空间>`），磁盘上不存在，这里为它合成一个只含 children 的条目，
        使聚合循环与第一根走同一条路径。

        返回 (相册列表, 目录级条目, 重扫目录数, 各阶段耗时)。
        """
        cache_dirs = cache_dirs or {}
        entries: Dict[str, Dict] = {}
        changed = 0
        flags_started = time.perf_counter()
        # 一次算完全树（见 `SettingsResolver.compute_pixiv_flags`）：逐个问 `pixiv_mode()`
        # 会让每个目录各走一遍设置继承链，9168 个目录实测 2.7-3.4s
        pixiv_flags = self._settings.compute_pixiv_flags(list(dirs))
        flags_ms = (time.perf_counter() - flags_started) * 1000
        loop_started = time.perf_counter()
        # 先建合成根条目：它的 children 要包含命名空间节点，聚合循环才把额外根目录的
        # 图片算进相册树总数（albums[''].image_count）
        synthetic_children: List[str] = []
        for rel, mtime in dirs.items():
            if self._paths.is_namespace_node(rel) or rel != '':
                continue
            root_entry = cache_dirs.get('') if isinstance(cache_dirs.get(''), dict) else None
            if root_entry and self._same_mtime(root_entry, mtime) \
                    and not root_entry.get('virtual'):
                entries[''] = root_entry
            else:
                # 走带缓存的扫描：与列表路径共用同一份条目，避免"相册索引重建"与
                # "列一次目录"各扫一遍同一个目录
                entries[''] = self._entries.scan('', self._paths.root, mtime)
                changed += 1
            break
        for rel, mtime in dirs.items():
            if self._paths.is_namespace_node(rel):
                # 虚拟命名空间节点：没有直接图片，children = 该根的一级子目录
                root, _ = self._paths.split_virtual(rel)
                children: List[str] = []
                if root is not None:
                    try:
                        with os.scandir(root) as entries_iter:
                            children = sorted(
                                item.name for item in entries_iter
                                if item.is_dir() and not item.name.startswith('.')
                                and item.name != '.cache')
                    except OSError:
                        pass
                entries[rel] = {
                    'path': rel, 'name': rel[len(self._paths.marker):],
                    'depth': 0, 'parent': None,
                    'direct_count': 0, 'direct_units': 0,
                    'direct_cover': '', 'direct_cover_mtime': 0.0, 'direct_mtime': 0.0,
                    'pixiv': pixiv_flags[rel], 'has_children': bool(children),
                    'children': children, 'mtime': mtime, 'virtual': True,
                }
                synthetic_children.append(rel)   # 完整虚拟路径（含 `__` 前缀）
                continue
            if rel == '':
                continue      # 合成根已在上面建好
            # 缓存命中条件除目录 mtime 外还要看 Pixiv 排序标志：封面挑选规则由它决定
            # （作品号最大 vs 自然序第一张），切换排序后旧封面必须重扫
            cached = cache_dirs.get(rel)
            if cached and self._same_mtime(cached, mtime) \
                    and cached.get('pixiv') == pixiv_flags[rel] \
                    and not cached.get('virtual'):
                entries[rel] = cached
                continue
            dir_path, _ = self._paths.resolve_dir(rel)
            if dir_path is None:
                continue
            entries[rel] = self._entries.scan(rel, dir_path, mtime)
            changed += 1
        loop_ms = (time.perf_counter() - loop_started) * 1000
        if synthetic_children and entries.get(''):
            entries['']['children'] = sorted({*entries['']['children'],
                                              *synthetic_children})

        # 自底向上聚合 image_count / cover / mtime。
        order_started = time.perf_counter()
        ordered = sorted(entries.values(),
                         key=lambda entry: (entry['depth'], 1 if entry.get('virtual') else 0),
                         reverse=True)
        order_ms = (time.perf_counter() - order_started) * 1000
        agg_started = time.perf_counter()
        # 漫画根下的一级子目录是「一本书」：它整本算一个更新单元，章（它的子目录）不再
        # 各自成单元。书的范围只在这里判定一次，条目里的 `direct_units` 不认识漫画根。
        book_paths = self._books.book_flags(list(dirs)) if self._books is not None else set()
        # (总数, 封面, 最新 mtime, 封面排序键, 更新单元数, 封面自身 mtime)
        totals: Dict[str, Tuple[int, str, float, tuple, int, float]] = {}
        child_visits = 0
        for entry in ordered:
            rel = entry['path']
            pixiv = pixiv_flags[rel]
            total = entry['direct_count']
            units = entry['direct_units']
            cover = entry['direct_cover']
            cover_mtime = float(entry.get('direct_cover_mtime') or 0.0)
            newest = entry['direct_mtime']
            rank = self._naming.cover_rank(cover, newest, pixiv)
            for child_name in entry['children']:
                child_visits += 1
                child_rel = f'{rel}/{child_name}' if rel else child_name
                child_totals = totals.get(child_rel)
                if not child_totals:
                    continue
                total += child_totals[0]
                units += child_totals[4]
                if child_totals[2] > newest:
                    newest = child_totals[2]
                # 封面：Pixiv 树取作品号最大的子目录（画师最近的作品），非 Pixiv 目录
                # 仍是 mtime 最新的那个；mtime 聚合不受影响。封面版本号跟封面一起走，
                # 否则换成子目录的封面后前端仍带着上一版 URL。
                if child_totals[3] > rank:
                    rank = child_totals[3]
                    cover = child_totals[1]
                    cover_mtime = child_totals[5]
            if rel in book_paths:
                # 整本一张卡：吞掉章与更深层目录各自算出的单元数，空书不占单元
                units = 1 if total else 0
            totals[rel] = (total, cover, newest, rank, units, cover_mtime)
        agg_ms = (time.perf_counter() - agg_started) * 1000
        breakdown = {
            'flags': round(flags_ms, 1),
            'loop': round(loop_ms, 1),
            'order': round(order_ms, 1),
            'agg': round(agg_ms, 1),
            'entries': len(entries),
            'child_visits': child_visits,
        }

        albums: List[Dict] = []
        for entry in entries.values():
            total, cover, newest, _rank, units, cover_mtime = totals[entry['path']]
            # 把全树聚合的结果写回缓存条目：`children_total`（列表时的按需聚合）与这里
            # 读的是同一组字段，两条路径互为缓存 —— 列一次目录便不必再走整棵子树，
            # 反过来相册索引重建也不重扫已按需算过的目录。
            entry['total_mtime'] = entry.get('mtime')
            entry['total_count'] = total
            entry['total_units'] = units
            entry['total_cover'] = cover
            entry['total_cover_mtime'] = cover_mtime
            albums.append({
                'name': entry['name'],
                'path': entry['path'],
                'parent': entry['parent'],
                'image_count': total,
                'direct_count': entry['direct_count'],
                # 递归更新单元数（按作品分组后的卡数）：时间线「按图片」的分页与总数
                # 用它，与 image_count 一起由同一次聚合算出
                'unit_count': units,
                # 这本书由漫画阅读插件的漫画根界定：时间线「按图片」把它整本列成一张卡，
                # 书的章不再单独成单元
                'book': entry['path'] in book_paths,
                'has_children': entry['has_children'],
                'cover': cover,
                # 封面文件自身的 mtime：前端把它当缩略图 URL 的版本号。相册的 `mtime`
                # 是整棵子树里最新文件的 mtime，替换封面（同目录 mtime 不变）时它可能
                # 根本不动，于是 /thumbs 的强缓存让旧封面一直显示。
                'cover_mtime': cover_mtime,
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

        return albums, entries, changed, breakdown

    @staticmethod
    def _same_mtime(cached: Dict, mtime: float) -> bool:
        """缓存条目记的目录 mtime 是否与快照一致。

        精确比较，不用"相差小于 0.5 秒"的容差：删除或移动文件后目录 mtime 只前进
        几毫秒，容差会把"刚变过"当成"没变"，相册树于是沿用旧计数与旧封面；而同一
        目录两次 `stat` 得到的浮点值是相同的，容差没有它要兜的误差。
        """
        try:
            return float(cached.get('mtime', 0)) == float(mtime)
        except (TypeError, ValueError):
            return False
