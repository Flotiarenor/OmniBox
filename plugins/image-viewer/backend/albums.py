"""相册树、封面挑选与相册缓存（ImageViewerPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 ImageViewerPlugin.__init__ 持有 ——
分片只把方法挂到同一个类上，因此方法与调用点都没有变。
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'image_viewer')
NAMESPACE_MARKER = _common.NAMESPACE_MARKER
ALLOWED_EXTENSIONS = _common.ALLOWED_EXTENSIONS
_pick_cover = _common.pick_cover
_cover_rank = _common.cover_rank
_same_path = _common.same_path

class AlbumMixin:
    """相册树、封面挑选与相册缓存。"""

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
