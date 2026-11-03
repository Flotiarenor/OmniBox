"""接入壳的统一刷新基建（同步 / 校验）。

壳负责去抖、单飞、增量短路、幽灵清理、进度与状态展示，这里只回答三个问题 —— 管哪些
文件、怎么算指纹、哪些条目需要重活。两个用户可见的动作为：

- **同步**：被动、增量。目录 mtime + 纳入索引的名字个数没变就整目录跳过；变了才失效
  该目录的列表缓存与相册索引（相册的派生数据本来就是**惰性**重算的，所以"重活"在这里
  就是"丢掉旧结果"，下一帧按需重算）。
- **校验**：手动、全量。逐项 stat 校对指纹、清掉消失条目的缩略图与尺寸缓存，并按有效
  键集合对账缩略图库的孤儿行。

为什么值得接：旧实现里「刷新」只清几个内存缓存，既不清缩略图孤儿、也没有"删除/移动后
失效相册缓存"这条，而"改规则必须手动 +1"的约定一旦忘记就是静默失效。接入后这些判据
由壳统一托管。

状态自持：无（缓存都在相册索引、列表缓存与缩略图库里）。`engine_provider` 是入口给的
闭包：壳把刷新引擎挂在插件实例上，组件不持有宿主对象，只按需问"引擎在哪"。
"""

import logging
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)


class FreshnessProvider:
    """向壳声明新鲜度规格，并实现它的三个钩子。"""

    def __init__(self, paths: Any, thumbs: Any, albums: Any, entries: Any,
                 listing: Any, extensions: Any, limits: Any,
                 engine_provider: Callable[[], Optional[Any]]) -> None:
        self._paths = paths
        self._thumbs = thumbs
        self._albums = albums
        self._entries = entries
        self._listing = listing
        self._extensions = extensions
        self._limits = limits
        self._engine = engine_provider

    # ===== 规格 =====

    def spec(self) -> Dict[str, Any]:
        """声明本插件的新鲜度规格（字段语义见 shell/backend/freshness.py）。"""
        return {
            # 可调用：额外图片目录能在运行时增删，引擎是长期缓存的对象
            'roots': self._paths.roots,
            # 前缀也必须跟着现算：命名空间 token 会因重名加序号，写死会与实际虚拟路径
            # 不一致（缩略图库的键就全对不上了）
            'prefixes': lambda: tuple(self._paths.virtual_path(root, '')
                                      for root in self._paths.roots()),
            'include': tuple(sorted(self._extensions)),
            'derive': self.derive,
            'prune': self.prune,
            'on_verified': self.audit,
            'on_pass_end': self.pass_end,
            'rebuild': self.rebuild,
            # 封面挑选规则变更时的整体失效信号：替代原先"记得手动 +1"的约定
            'content_version': self._limits.album_cache_version,
            # 逐条 stat 会把"进一次相册页"变成"给整库每个文件 stat 一次"：实测 6000 张
            # 的整树被动同步从 0.11s 涨到 0.29s（真实磁盘冷缓存下差距更大），而它换来的
            # 只是"原地改写文件也能被同步发现" —— 这件事由**校验**负责，插件自己在读图
            # 时也按 mtime 校验尺寸元数据。目录 mtime + 纳入索引的名字个数足以发现新增/
            # 删除/改名。
            'stat_entries': False,
            'unit': '张',
            'min_sync_interval': 5.0,
            'max_dirs_per_sync': 400,
        }

    # ===== 钩子（全部由壳在它自己的线程里调用，只做缓存失效，不做重活） =====

    def derive(self, items) -> Dict[str, Any]:
        """条目新增/变化：作废受影响的相册节点，让下一帧按需重算。

        相册的派生数据（图片列表、封面、递归计数）本来就是**惰性**重算的，所以这里不
        需要真的去算 —— 把旧结果丢掉，读盘时自然是最新的。

        **只丢"变化目录 + 各级父目录"**，其余节点继续按 mtime 复用：实测 6000 张 /
        1400 目录上，整表作废后重建 2.74s，精细作废后 1.40s —— 差的 1.34s 全在把 1400
        个目录重扫一遍，而其中 1399 个根本没变（全树枚举 1.40s 是两者都要付的底价）。

        列表缓存（≤200 条）仍整表清空：它便宜，且键在两种列表里形状不同，清子目录会
        留下父目录的陈旧连续浏览序列。
        """
        self._entries.drop_nodes(items)
        self._listing.clear_cache()
        self._albums.invalidate()
        return {'count': len(items)}

    def prune(self, keys) -> int:
        """条目消失：删缩略图行、丢尺寸元数据、作废所属目录的相册节点。"""
        pruned = 0
        dirs = []
        for rel in keys:
            try:
                self._thumbs.delete_key(rel)
                abs_path, _ = self._paths.resolve_path(rel)
                self._thumbs.drop(str(abs_path or (self._paths.root / rel)))
                dirs.append({'dir': str(rel).rpartition('/')[0]})
                pruned += 1
            except Exception as exc:
                log.warning('[ImageViewer] 清理失效条目失败 %s: %s', rel, exc)
        if dirs:
            self._entries.drop_nodes(dirs)
            self._albums.invalidate()
        if pruned:
            self._thumbs.mark_dirty()
            self._thumbs.flush()
        return pruned

    def pass_end(self, report, verified: bool) -> Dict[str, Any]:
        """整趟结束：**在任务里**把相册索引重建好，"完成"就等于下一帧直接可读。

        为什么放在这里：索引重建（全树枚举 + 变化目录重扫）实测在 6000 张 / 1400 目录
        上要 1.4-2.7s，而它是惰性重算的 —— 放在任务外，用户看到的就是"进度条已经满了、
        界面还要卡几秒再出内容"。宁可让"校验中"多显示这几秒：完成态必须意味着"现在读
        就是最新的"，而不是"现在读会触发几秒的重算"。
        """
        if not verified or report.get('partial') or report.get('errors'):
            return {}
        engine = self._engine()
        if engine is not None:
            engine.progress('重建相册索引')
        try:
            result = self._albums.list_albums()
        except Exception as exc:
            log.error('[ImageViewer] 校验后重建相册索引失败: %s', exc)
            return {'album_error': str(exc)}
        return {'albums': len(result.get('albums') or []),
                'albums_rescanned': int(result.get('changed') or 0)}

    def audit(self, keys) -> Dict[str, Any]:
        """全量校验后的对账：按"当前有效图片键集合"清缩略图库里的孤儿行。

        这是旧实现缺的那一环：只有插件内的删除会删缩略图行，在资源管理器 / pixiv-sync
        里删掉的图片会永久留在缩略图库里，而"刷新"不 prune、"全量重建"只能把整个库
        清空重来。
        """
        valid = {str(key) for key in keys}
        removed_rows = self._thumbs.prune_keys(valid)
        return {'thumb_rows_removed': removed_rows, 'valid_entries': len(valid)}

    def refresh(self) -> Dict:
        """兼容入口（API 名 `refresh`）：转发到壳的统一刷新「同步」，并清本地派生缓存。

        老前端与"保存设置后刷新"仍会调它。引擎不可用（规格非法）时退化成原来的行为，
        不让刷新整个失灵 —— 那时至少把本地缓存清掉。
        """
        report: Dict[str, Any] = {}
        engine = self._engine()
        if engine is not None:
            report = engine.sync('', force=True)
        self._listing.clear_cache()
        self._albums.reset_index()
        self._albums.invalidate()
        try:
            if self._paths.album_cache_file.exists():
                self._paths.album_cache_file.unlink()
        except OSError:
            pass
        return {'success': True, **report}

    def rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃派生缓存（缩略图库 + 尺寸元数据 + 相册索引）。

        **不预生成**缩略图：`/thumbs` 会按需生成（预生成上万次随机小文件 I/O 得不偿
        失）。需要"一次生成整库"的场景仍走 `rebuild_all()`。
        """
        cleared: Dict[str, Any] = {'kind': kind}
        try:
            self._thumbs.cache.clear()
            cleared['thumbs'] = True
        except Exception as exc:
            cleared['thumbs_error'] = str(exc)
        self._thumbs.clear_meta()
        self._albums.reset_index()
        self._listing.clear_cache()
        self._albums.invalidate()
        try:
            if self._paths.album_cache_file.exists():
                self._paths.album_cache_file.unlink()
        except OSError as exc:
            cleared['album_error'] = str(exc)
        return cleared
