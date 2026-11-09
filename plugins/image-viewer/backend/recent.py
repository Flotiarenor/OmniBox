"""时间线「按图片」的跨目录最近更新分页（以「作品」为更新单元）。

为什么以单元为粒度：真实图库是 `pixiv/<画师>/<作品号>/<作品号>_p0..pN`，同一作品的
差分页若各占一张卡，时间线会被同一个作品刷屏，也与打开画师目录看到的布局不一致。所以
这里的排序、分页、总数一律以**单元**计：同一目录里同作品号的多页合成一张作品卡（封面
p0、页数角标），其余文件各自一张单图。

漫画 / 本子目录（`本子/1479415/00001.jpg…`）同理，但边界不靠文件名猜：漫画阅读插件把
**漫画根**下的一级子目录当成一部漫画（多章本子的章是它的一级子目录），`books.MangaBooks`
读它的根设置并按同一口径给出整本的页序、封面与标题，这里只负责把一个单元放进时间线。
同时也不会钻进书里把章当成独立单元。漫画插件不在 / 漫画根没配置时整体退化成普通图片。

为什么单独一个组件：它同时要读相册索引（每个目录子树的递归最新 mtime 与单元数）、
文件名分组（与扫描共用 `PixivNaming.work_units`）、缩略图组件（补齐宽高）、漫画书范围
（`MangaBooks`）与路径解析，但不属于其中任何一个的职责。放进 `listing` 会让"列一个目录"
多出一条"按 mtime 跨子树剪枝"的分支，而两条路径的代价与失效原因完全不同（前者只读一层，
后者要读多棵子树）。

算法：`list_albums()` 给出的每目录 mtime 是**该子树内最新图片的 mtime**，因此是子树
上界。按上界从大到小展开目录（优先队列），每展开一个目录就把它的文件名交给
`unit_groups` 分组，每组产出**一个单元**并入容量为 K = page × per_page 的小顶堆（单元
mtime 取组内最新页）；当队列里剩下子树的最高上界已经小于堆顶（第 K 名单元）的 mtime
时，后面的子树只可能产出更旧的单元，立即停止。同 mtime 以单元代表 url（作品封面 /
单图自身）升序作次序键，翻页才不重不漏。

`all_images` 是本页单元按次序展开的全部页（作品按文件名自然序展开、单图一张）：第 i 个
单元的封面在其中的下标 = 前面各单元 image_count / 1 的累加。前端 Justified 布局的
`seqIndex` 按同一规则算，两边必须逐张对齐，否则灯箱会打开到别的作品的页。

mtime 大量重复时（批量下载进同一秒的图库很常见）剪枝判据不再生效：剩下一堆上界相等、
次序只能靠 url 决定的子树，必须展开才知道有没有更靠前的单元。深页因此可能一直展开到
扫描预算，届时返回 `partial`，并把 `total` 收敛为本次能排列出的单元数，由前端说明列表
不完整。

命名空间节点（额外根目录的虚拟顶层）只展开子目录、不扫直接图片：相册树把它算作纯容器，
额外根顶层的直接图片不计入它的递归单元数，只有不扫才与 `total` 一致。

状态自持：无长期状态（索引与尺寸缓存都在各自组件里），构造参数就是全部依赖。
"""

import heapq
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple


class _Candidate:
    """时间线里的一个更新单元（作品卡或单图）。

    比较规则按"谁更该被淘汰"定义：mtime 更小的更差（作品取组内最新页）；mtime 相同时
    代表 url 更大的更差（它在 mtime 倒序 + url 升序里更靠后）。于是定长小顶堆的堆顶恒是
    第 K 名，新单元入堆后弹出堆顶即保留前 K 个。
    """

    __slots__ = ('mtime', 'unit', 'url')

    def __init__(self, unit: Dict[str, Any]) -> None:
        self.unit = unit
        self.mtime = float(unit['mtime'])
        self.url = str(unit['pages'][0]['url'])

    def __lt__(self, other: '_Candidate') -> bool:
        if self.mtime != other.mtime:
            return self.mtime < other.mtime
        return self.url > other.url


class RecentImages:
    """跨目录按单元 mtime 倒序的最近更新分页。"""

    def __init__(self, paths: Any, albums: Any, thumbs: Any,
                 naming: Any, extensions: Any, limits: Any, books: Any = None) -> None:
        self._paths = paths
        self._albums = albums
        self._thumbs = thumbs
        self._naming = naming
        self._extensions = extensions
        self._limits = limits
        self._books = books

    def list_recent_images(self, page: int = 1, per_page: int = 40) -> Dict:
        """最近更新的作品 / 单图，按单元 mtime 倒序、同 mtime 按代表 url 升序分页。

        返回 `{items, all_images, page, per_page, total, reach, image_total, has_next,
        has_prev, scanned_dirs, partial}`。`items` 与 `list_folder_items` 的条目同形：
        作品单元是 `type='album'` 的卡（`cover` 为封面、`cover_mtime` 为封面文件自身
        的 mtime、`image_count` / `total_count` 为页数），单图单元是 `type='image'`，
        二者都不含物理路径。

        `total` 是整库更新单元数，`reach` 是本次预算内真正排列出的单元数，`image_total`
        是整库图片数。`partial` 表示目录扫描预算用尽、还有更早的子树没展开：此时这一页的
        排序依据不完整（未展开的子树里可能有更靠前的单元），但**不等于列表已到边界** ——
        `has_next` 只在这次没把本页填满（`reach` 小于页末）或整库没有更多单元时为假，
        前端按 `has_next` 给"下一页"，按 `reach` 提示"仅列出最近 N 项"。`scanned_dirs`
        是本次真正展开的目录数，供诊断"是否退化成扫全树"。
        """
        try:
            page = max(1, int(page))
            per_page = min(200, max(1, int(per_page)))
        except (TypeError, ValueError):
            page, per_page = 1, 40

        albums = self._albums.list_albums().get('albums') or []
        by_path = {album.get('path') or '': album for album in albums}
        root = by_path.get('')
        image_total = int(root.get('image_count') or 0) if root else 0
        total = int(root.get('unit_count') or 0) if root else 0
        if not total:
            return {
                'items': [], 'all_images': [], 'page': page, 'per_page': per_page,
                'total': 0, 'reach': 0, 'image_total': image_total,
                'has_next': False, 'has_prev': page > 1,
                'scanned_dirs': 0, 'partial': False,
            }

        # 只把"子树里确实有图片"的目录放进队列：没有图片的目录上界恒为 0，
        # 展开它只会白付一次 scandir。
        want = page * per_page
        picked: List[_Candidate] = []
        queue: List[Tuple[float, str]] = []
        for album in albums:
            rel = album.get('path') or ''
            if not album.get('image_count'):
                continue
            if rel == '' or self._paths.is_namespace_node(rel):
                heapq.heappush(queue, (-float(album.get('mtime') or 0.0), rel))

        budget = int(self._limits.max_recent_dirs)
        scanned = 0
        partial = False
        while queue:
            if len(picked) >= want and -queue[0][0] < picked[0].mtime:
                # 剩余子树的最高上界都低于第 K 名单元：再扫也进不了前 want 个
                break
            if scanned >= budget:
                # 预算用尽但仍有子树的更新可能更靠前：结果不完整，交给前端提示
                partial = True
                break
            _bound, rel = heapq.heappop(queue)
            scanned += 1
            for child_bound, child in self._expand(rel, by_path, picked, want):
                heapq.heappush(queue, (-child_bound, child))

        ordered = sorted(picked, key=lambda candidate: (-candidate.mtime, candidate.url))
        # 本次预算内真正排列出的单元数：`total` 是整库单元数，`reach` 是这次能列出的。
        # 预算用尽后 `reach` 可能小于 total —— 再深的分页在同一预算下必然返回空页，
        # 因此 `has_next` 在 partial 时为假，前端据此不再给出"下一页"，也就不会出现
        # "点得到却打不开"的页码。
        reach = len(ordered)
        start = (page - 1) * per_page
        units = [candidate.unit for candidate in ordered[start:start + per_page]]
        rows = [row for unit in units for row in unit['pages']]
        # 本页涉及的全部页都补宽高：灯箱按 all_images 逐张显示，缺尺寸的页会先按占位
        # 尺寸排一次再跳变
        self._thumbs.fill_image_sizes(rows)
        self._thumbs.flush()
        items = [self._public_unit(unit) for unit in units]
        all_images: List[Dict[str, Any]] = [{
            'url': row.get('url') or '',
            'width': row.get('width') or 0,
            'height': row.get('height') or 0,
        } for row in rows]
        end = page * per_page
        return {
            'items': items, 'all_images': all_images,
            'page': page, 'per_page': per_page,
            'total': total, 'reach': reach, 'image_total': image_total,
            # 下一页存在与否只看"这一页有没有被预算截断"：填满了（reach >= end）就还能
            # 往后翻一页，没填满就是列表边界。partial 只表示排序依据不完整，不表示到边界。
            'has_next': end < total and reach >= end, 'has_prev': page > 1,
            'scanned_dirs': scanned, 'partial': partial,
        }

    def _expand(self, rel: str, by_path: Dict[str, Any],
                picked: List[_Candidate], want: int) -> List[Tuple[float, str]]:
        """展开一个目录：产出一个或多个单元，返回可入队的 (上界, 子目录)。

        漫画书（漫画根的一级子目录）整本只产出一个单元，**并且不展开它的章** —— 章属于
        这本书，不是时间线里的独立条目。其余目录只扫**直接**下级：文件按作品分组
        （同作品号的多页合成一张卡），子目录按相册索引给出的递归 mtime 入队
        （`image_count` 为 0 的子目录整棵都没有图片，不进队）。命名空间节点是额外根的
        虚拟顶层，本身没有直接图片，只展开子目录。
        """
        if self._books is not None and by_path.get(rel, {}).get('book'):
            self._push_book(rel, picked, want)
            return []
        target, _ = self._paths.resolve_dir(rel)
        children: List[Tuple[float, str]] = []
        if target is None:
            return children
        namespace = self._paths.is_namespace_node(rel)
        files: List[Dict[str, Any]] = []
        try:
            with os.scandir(target) as entries:
                for item in entries:
                    if item.name.startswith('.') or item.name == '.cache':
                        continue
                    try:
                        if item.is_dir():
                            child = f'{rel}/{item.name}' if rel else item.name
                            album = by_path.get(child)
                            if not album or not album.get('image_count'):
                                continue
                            children.append((float(album.get('mtime') or 0.0), child))
                        elif not namespace and item.is_file() \
                                and Path(item.name).suffix.lower() in self._extensions:
                            stat = item.stat()
                            url = (Path(rel) / item.name).as_posix() if rel else item.name
                            files.append({'path': item.path, 'name': item.name,
                                          'mtime': stat.st_mtime, 'url': url})
                    except OSError:
                        continue
        except OSError:
            return children
        if not files:
            return children
        # 该目录是否启用 Pixiv 排序：作品卡与画师目录里的瓦片用同一个标记
        use_time_name = bool((by_path.get(rel) or {}).get('use_time_name'))
        by_name = {row['name']: row for row in files}
        for group in self._naming.work_units([row['name'] for row in files]):
            pages = [by_name[name] for name in group if name in by_name]
            if not pages:
                continue
            is_work = len(pages) > 1
            unit = {
                # 多页差分作品才是作品卡；单页的 `800_p0` 与普通文件一样按单图显示
                'type': 'album' if is_work else 'image',
                'path': rel,
                'name': self._work_name(pages[0]) if is_work else pages[0]['name'],
                'mtime': max(page['mtime'] for page in pages),
                'use_time_name': use_time_name,
                'pages': pages,
            }
            heapq.heappush(picked, _Candidate(unit))
            if len(picked) > want:
                heapq.heappop(picked)
        return children

    def _push_book(self, rel: str, picked: List[_Candidate], want: int) -> None:
        """把一本漫画作为单元入堆：页序、封面、标题都按漫画插件的口径给。"""
        if self._books is None:
            return
        pages = self._books.book_pages(rel)
        if not pages:
            return
        unit = {
            'type': 'album' if len(pages) > 1 else 'image',
            'path': rel,
            'name': self._books.book_title(rel) if len(pages) > 1 else pages[0]['name'],
            'mtime': max(page['mtime'] for page in pages),
            'use_time_name': False,
            'pages': pages,
        }
        heapq.heappush(picked, _Candidate(unit))
        if len(picked) > want:
            heapq.heappop(picked)

    @staticmethod
    def _work_name(first_page: Dict[str, Any]) -> str:
        """作品名 = 首页文件名去掉扩展名与 `_p<页号>` 后缀（`800_p0.png` → `800`）。

        多页组的成员都匹配 `<作品号>_p<页号>`，`_p` 之前就是作品号本身；卡上带作品名而
        不是一个纯数字，时间线的标题与排序提示才有可读的落点。
        """
        return Path(str(first_page['name'])).stem.split('_p', 1)[0]

    @staticmethod
    def _public_unit(unit: Dict[str, Any]) -> Dict[str, Any]:
        """单元 → 前端条目（与 `list_folder_items` 的 items 同形，不含物理路径）。"""
        pages = unit['pages']
        cover = pages[0]
        if unit['type'] == 'image':
            return {
                'type': 'image',
                'url': cover.get('url') or '',
                'name': cover.get('name') or '',
                'mtime': cover.get('mtime') or 0.0,
                'width': cover.get('width') or 0,
                'height': cover.get('height') or 0,
            }
        return {
            'type': 'album',
            'path': unit['path'],
            'name': unit['name'],
            'cover': cover.get('url') or '',
            # 封面文件自身的 mtime：前端把它当缩略图 URL 的版本号。单元的 `mtime` 是
            # 组内最新页的时间，替换封面（p0 / 书封面）时它未必变，只有这个字段会变。
            'cover_mtime': cover.get('mtime') or 0.0,
            'image_count': len(pages),
            'total_count': len(pages),
            # 作品单元是同一目录里的一组页，本身没有下级目录
            'has_children': False,
            'mtime': unit['mtime'],
            'width': cover.get('width') or 0,
            'height': cover.get('height') or 0,
            'use_time_name': unit['use_time_name'],
            'pending': False,
        }
