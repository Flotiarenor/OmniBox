"""与漫画阅读插件（manga-library）对齐的「书」范围与整本页面。

为什么需要它：漫画插件把**漫画根**下的一级子目录当成一部漫画（多章本子的章是它的一级
子目录，页面在章里），页面命名由下载器决定 —— `00001.jpg`、`第 1 話` 这类名字都不能靠
猜测来判断「这一堆图是一本书」。时间线要按同一口径把整本合并成一张卡，就必须知道漫画根
在哪；它属于另一个插件的设置，所以这里只通过注入的 `root_provider` 读，读不到就整体退化
（不合并，按普通图片逐张列），绝不用文件名兜底猜。

页面顺序、封面与标题都照漫画插件的口径：

- 章按名字自然序，章内页面按名字自然序；单章本子的图片直接在书本目录里；
- 封面：优先名字含 `cover` / `封面` 的图，其次数字命名的第一页，最后自然序第一张
  （先找自然序第一个章，再找书本目录）；
- 标题：`album_info.json` 的 `title`，缺失时用目录名。

状态自持：漫画根解析结果（物理根 + 图片根内的虚拟相对路径）与时间戳都在这里；
`invalidate` 由入口在设置变更 / 刷新时调用，另有 TTL 兜住另一个插件改根的情况。
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shell.backend.media_catalog import natural_sort_key

log = logging.getLogger(__name__)

#: 章节目录里不参与阅读的目录名（与漫画插件的 `visible_subdirs` 同一口径）。
_SKIP_SUBDIR = 'ai'

#: 书名里表示「封面」的片段（与漫画插件的 `find_cover` 同一口径）。
_COVER_HINTS = ('cover', '封面')

#: 漫画根是另一个插件的设置：解析结果复用一段时间，改根后最多这么久生效。
_ROOT_TTL = 30.0


class MangaBooks:
    """漫画根下的书 / 章判定，以及一本的整页序列。"""

    def __init__(self, paths: Any, root_provider: Callable[[], Optional[str]],
                 extensions: Any, ttl: float = _ROOT_TTL) -> None:
        self._paths = paths
        self._root_provider = root_provider
        self._extensions = extensions
        self._ttl = ttl
        self._resolved = False
        self._resolved_at = 0.0
        self._root_abs: Optional[Path] = None
        self._root_rel = ''

    # ===== 漫画根 =====

    def invalidate(self) -> None:
        """丢掉解析结果（换根、刷新时由入口调用）。"""
        self._resolved = False
        self._root_abs = None
        self._root_rel = ''

    def _resolve(self) -> Optional[Path]:
        now = time.time()
        if self._resolved and (now - self._resolved_at) < self._ttl:
            return self._root_abs
        self._resolved = True
        self._resolved_at = now
        self._root_abs = None
        self._root_rel = ''
        raw: Optional[str] = None
        try:
            raw = self._root_provider()
        except Exception as exc:                       # 另一个插件不可用时只退化，不影响浏览
            log.info('[ImageViewer] 读取漫画根失败，按普通图片处理: %s', exc)
            raw = None
        if not raw:
            return None
        target = Path(str(raw))
        for root in self._paths.roots():
            try:
                inner = target.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            self._root_abs = target
            self._root_rel = self._paths.virtual_path(root, '' if inner == '.' else inner)
            return self._root_abs
        # 漫画根不在图片根里：这本书的页面本来就不会出现在相册树里，无需合并
        return None

    def root_rel(self) -> str:
        """漫画根在图片根内的虚拟相对路径（未配置或不在图片根内时为空串）。"""
        self._resolve()
        return self._root_rel

    def kind(self, rel_path: str) -> str:
        """`'book'`（漫画根的一级子目录）、`'chapter'`（书的一级子目录）或空串。"""
        if self._resolve() is None:
            return ''
        rel = (rel_path or '').replace('\\', '/').strip('/')
        parent, _, name = rel.rpartition('/')
        if not name:
            return ''
        root_rel = self._root_rel
        if parent == root_rel:
            return 'book'
        grandparent, _, parent_name = parent.rpartition('/')
        if parent_name and grandparent == root_rel:
            return 'chapter'
        return ''

    def is_book(self, rel_path: str) -> bool:
        return self.kind(rel_path) == 'book'

    def book_flags(self, paths: List[str]) -> set:
        """一批相册路径里哪些是「书」（相册索引聚合单元数时用）。"""
        if self._resolve() is None:
            return set()
        return {rel for rel in paths if self.kind(rel) == 'book'}

    # ===== 一本的页面 =====

    def book_pages(self, rel_path: str) -> List[Dict[str, Any]]:
        """整本的页：章（一级子目录）按自然序、章内按自然序，最后是书本目录自己的图。

        返回条目带物理 `path`（供尺寸读取）与虚拟 `url`（供缩略图 / 灯箱），封面恒在首位
        —— 前端按"第 i 个单元的张数累加"定位灯箱起点，封面不在首位就会打开到别的页。
        """
        target, _ = self._paths.resolve_dir(rel_path)
        if target is None:
            return []
        chapters: List[Path] = []
        try:
            with os.scandir(target) as entries:
                for item in entries:
                    if item.name.startswith('.') or item.name == _SKIP_SUBDIR:
                        continue
                    try:
                        if item.is_dir():
                            chapters.append(Path(item.path))
                    except OSError:
                        continue
        except OSError:
            chapters = []
        chapters.sort(key=lambda path: natural_sort_key(path.name))

        pages: List[Dict[str, Any]] = []
        first_chapter: List[Dict[str, Any]] = []
        for index, chapter in enumerate(chapters):
            group = self._images(chapter, f'{rel_path}/{chapter.name}' if rel_path
                                 else chapter.name)
            if index == 0:
                first_chapter = group
            pages.extend(group)
        own = self._images(target, rel_path)
        pages.extend(own)

        cover = self._pick_cover(first_chapter, own)
        if cover is None:
            return pages
        return [cover] + [page for page in pages if page['url'] != cover['url']]

    def _images(self, directory: Path, rel_path: str) -> List[Dict[str, Any]]:
        """一个目录的直接图片，按名字自然序（与漫画插件的页序规则一致）。"""
        images: List[Dict[str, Any]] = []
        try:
            with os.scandir(directory) as entries:
                for item in entries:
                    if item.name.startswith('.'):
                        continue
                    try:
                        if not item.is_file():
                            continue
                        if Path(item.name).suffix.lower() not in self._extensions:
                            continue
                        stat = item.stat()
                    except OSError:
                        continue
                    url = f'{rel_path}/{item.name}' if rel_path else item.name
                    images.append({'path': item.path, 'name': item.name,
                                   'mtime': stat.st_mtime, 'url': url})
        except OSError:
            return []
        images.sort(key=lambda row: natural_sort_key(row['name']))
        return images

    def _pick_cover(self, first_chapter: List[Dict[str, Any]],
                    own: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """封面：先找自然序第一个章，再找书本目录（与漫画插件的 find_cover 一致）。"""
        for group in (first_chapter, own):
            if not group:
                continue
            hinted = [row for row in group
                      if any(hint in Path(row['name']).stem.lower() for hint in _COVER_HINTS)]
            if hinted:
                return self._natural_first(hinted)
            numeric = [row for row in group if Path(row['name']).stem.isdigit()]
            if numeric:
                return self._natural_first(numeric)
            return self._natural_first(group)
        return None

    @staticmethod
    def _natural_first(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return min(rows, key=lambda row: natural_sort_key(row['name']))

    def book_title(self, rel_path: str) -> str:
        """书名：`album_info.json` 的 `title`，缺省用目录名（与书架取名一致）。"""
        target, _ = self._paths.resolve_dir(rel_path)
        if target is not None:
            info_path = target / 'album_info.json'
            try:
                data = json.loads(info_path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                title = data.get('title')
                if isinstance(title, str) and title.strip():
                    return title.strip()
        return Path(rel_path).name or rel_path
