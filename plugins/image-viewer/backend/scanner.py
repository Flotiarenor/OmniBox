"""单个目录的扫描：把"怎么读一个目录"从"扫到的结果怎么用"里分出来。

无状态：允许的扩展名与依赖在构造时确定，扫描本身不读设置、不写缓存，因此可以用一个
临时目录直接验证。

这一层是"裸扫描"：只 stat 一层、挑出子目录与图片、按规则选一个封面。带 mtime 校验的
进程内缓存属于相册索引（`albums.py`），因为它必须和索引条目共用同一份存储 ——
早先这两层在两个文件里互相调用（列表分片给相册分片提供带缓存的扫描，相册分片又给
列表分片提供裸扫描），构成一个方向相反的环。
"""

import logging
from pathlib import Path
from typing import Any, Dict, FrozenSet, List

from shell.backend import media_catalog

log = logging.getLogger(__name__)


class DirScanner:
    """扫描一个目录的直接下级，产出相册索引条目。"""

    def __init__(self, paths: Any, naming: Any, extensions: FrozenSet[str]) -> None:
        self._paths = paths
        self._naming = naming
        self._extensions = extensions

    def scan(self, dir_path: Path, rel_path: str, pixiv: bool) -> Dict:
        """只扫描一个目录的直接图片（一次 `os.scandir`，开销可控）。

        目录名语义（name/depth/parent）按**根内**相对路径计算，所以额外根目录下的
        作者/作品与第一根处在同一层级，两层 Pixiv 布局不会被命名空间顶掉；文件 `rel`
        仍是虚拟路径（`__命名空间/...`），可直接当作缩略图/图片 URL。

        `pixiv` 由调用方给出：封面挑选规则由它决定（作品号最大 vs 自然序第一张），
        而判断它要读设置继承链 —— 那属于调用方（相册索引）的职责，扫描器保持无状态。
        """
        result = media_catalog.scan_directory(dir_path, set(self._extensions))
        # 子目录按名字排序（不是自然序）：这个列表只用于聚合与前端展示的分组顺序，
        # 排序规则与迁移前逐字一致。
        children: List[str] = sorted(item['name'] for item in result['dirs'])
        images: List[Dict] = [
            {
                'rel': (Path(rel_path) / item['name']).as_posix() if rel_path else item['name'],
                'mtime': item['mtime'],
            }
            for item in result['files']
        ]
        # 更新单元数：按作品分组后的组数（同一作品的差分页算一个单元）。时间线要用
        # 组内容、扫描只关心个数，两者共用 `PixivNaming.work_units` 这一份实现，否则
        # 「最近更新」的总数与扫描出来的单元数会各算一套。漫画书的合并不在这里 ——
        # 书的边界由漫画插件（`books.MangaBooks`）决定，在相册聚合时整本记一个单元。
        direct_units = len(self._naming.work_units(
            [item['name'] for item in result['files']]))
        # 封面：Pixiv 树取作品号最大的一张（p0），其余取文件名自然序第一张；
        # 相册新旧仍按最新 mtime 计算
        newest = max((img['mtime'] for img in images), default=0.0)
        cover = self._naming.pick_cover(images, pixiv)
        root, inner = self._paths.split_virtual(rel_path)
        parent = self._paths.virtual_path(root, '/'.join(inner.split('/')[:-1])) \
            if (root is not None and inner) else None
        return {
            'path': rel_path,
            'name': dir_path.name,
            'depth': inner.count('/') + (1 if inner else 0),
            'parent': parent,
            'direct_count': len(images),
            'direct_units': direct_units,
            'direct_cover': cover,
            'direct_mtime': newest,
            'pixiv': pixiv,      # 封面挑选依据，缓存复用时要核对（见 AlbumIndex.scan_dir_cached）
            'has_children': len(children) > 0,
            'children': children,
        }
