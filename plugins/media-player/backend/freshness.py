"""统一刷新基建接入（MediaPlayerPlugin 的一个 mixin 分片）。

把媒体库接进壳的「同步 / 校验」（`shell/backend/freshness.py`，契约见
`docs/plugin-guide.md` §3.4）。本插件回答三个问题：

- **管哪些文件**：全部扫描根下的音频/视频，外加目录封面图（`folder.jpg` 等）——
  封面不改索引条目，却决定 `has_cover`，所以必须进指纹（`include_names`）；
- **怎么算指纹**：条目键 = 绝对路径的 md5 前 16 位（`item_id`，索引与缩略图库
  都用它）；解析规则版本 = `INDEX_VERSION`，升级后由基建整体作废派生数据；
- **哪些条目需要重活**：`build_item()`（读标签 / 探时长）只对新增或指纹变化的
  文件调用，这是全流程唯一昂贵的一步。

修掉的两个旧问题：

1. **幽灵条目**：旧增量扫描只做 `merged[id] = item`，从磁盘删掉的媒体永久留在
   索引里（实测：删掉 b.mp3 后索引仍有两条），只有「深度扫描」能清。现在由
   `on_verified` 按有效键集合做差集删除 —— 校验是唯一入口，不必再"重读全库"。
2. **根级断点**：旧实现每个根目录完成才落一次检查点（`scan_task.json`），中断后
   以根为粒度续跑。现在指纹精确到目录，同步天然跳过未变化的目录，那套状态机
   （任务文件 + `completed_roots` + paused 恢复）整体删除。
"""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_scanner = load_sibling(__file__, 'scanner', 'media_player')
_metadata = load_sibling(__file__, 'metadata', 'media_player')
build_item = _scanner.build_item
find_cover = _scanner.find_cover
item_id = _scanner.item_id
MetadataReader = _metadata.MetadataReader
AUDIO_EXTS = _scanner.AUDIO_EXTS
VIDEO_EXTS = _scanner.VIDEO_EXTS
COVER_NAMES = _scanner.COVER_NAMES
MEDIA_EXTS = _scanner.MEDIA_EXTS
INDEX_VERSION = _scanner.INDEX_VERSION

# 封面文件在指纹库里的条目键前缀：`cover:<目录绝对路径>`。
# 为什么要给封面单独一类键：它是"影响派生数据但不进索引"的文件。键里带上目录，
# 于是"某目录的封面被加入/替换/删除"都能定位到要重算 `has_cover` 的那批条目。
COVER_KEY_PREFIX = 'cover:'


class FreshnessMixin:
    """与壳的统一刷新基建对接（声明 + 四个钩子）。"""

    def freshness_spec(self) -> Dict[str, Any]:
        return {
            # 可调用：媒体文件夹能在运行时增删（第一行是数据根，其余是额外扫描根）
            'roots': lambda: list(self._scan_roots),
            'include': tuple(sorted(MEDIA_EXTS)),
            # 目录封面图纳入指纹：后补/替换一张 folder.jpg 不改任何媒体文件的
            # mtime，只能靠它把"这个目录变了"报出来（否则 has_cover 永远不刷新）
            'include_names': tuple(sorted(COVER_NAMES)),
            'key_of': self._freshness_key,
            'derive': self._freshness_derive,
            'prune': self._freshness_prune,
            'on_verified': self._freshness_audit,
            'on_pass_end': self._freshness_pass_end,
            'rebuild': self._freshness_rebuild,
            'content_version': INDEX_VERSION,
            'unit': '首',
            'min_sync_interval': 5.0,
            'max_dirs_per_sync': 400,
        }

    # ===== 键 =====

    def _freshness_key(self, abs_path: str) -> str:
        """媒体文件 → item id；目录封面 → `cover:<目录>`（见 COVER_KEY_PREFIX）。"""
        path = Path(abs_path)
        if path.name.lower() in COVER_NAMES:
            return f'{COVER_KEY_PREFIX}{path.parent}'
        return item_id(abs_path)

    # ===== 钩子 =====

    def _freshness_derive(self, items) -> Dict[str, Any]:
        """新增/指纹变化：构造或刷新条目；封面变化只刷新受影响目录的 `has_cover`。"""
        updated: Dict[str, Any] = {}
        errors: List[str] = []
        cover_dirs: set = set()
        for it in items:
            key = str(it.get('key') or '')
            if key.startswith(COVER_KEY_PREFIX):
                cover_dirs.add(key[len(COVER_KEY_PREFIX):])
                continue
            path = Path(it['path'])
            namespace = self._namespace_for(path)
            try:
                stat = path.stat()
            except OSError as e:
                errors.append(f'{path}: {e}')
                continue
            item = build_item(path, self._root_for(path), namespace,
                              self._rel_dir_for(path), stat, find_cover(path.parent))
            if item is None:
                continue
            updated[item.id] = item

        merged = dict(self._items)
        merged.update(updated)
        if cover_dirs:
            merged = self._refresh_covers(merged, cover_dirs)
        if updated or cover_dirs:
            self._publish_items(merged)
            self._index_dirty = True
        return {'count': len(updated), 'errors': errors[:20]}

    def _freshness_prune(self, keys) -> int:
        """条目消失：移出索引并删封面缓存（封面键 → 重算该目录的 has_cover）。"""
        dropped = 0
        cover_dirs: set = set()
        merged = dict(self._items)
        for key in keys:
            key = str(key)
            if key.startswith(COVER_KEY_PREFIX):
                cover_dirs.add(key[len(COVER_KEY_PREFIX):])
                continue
            if merged.pop(key, None) is not None:
                dropped += 1
                try:
                    self._thumb_cache.delete(key)
                except Exception as e:
                    log.warning(f'[MediaPlayer] 清理封面缓存失败 {key}: {e}')
        if cover_dirs:
            merged = self._refresh_covers(merged, cover_dirs)
        if dropped or cover_dirs:
            self._publish_items(merged)
            self._index_dirty = True
        return dropped

    def _freshness_audit(self, keys) -> Dict[str, Any]:
        """全量校验后的对账：按有效键集合删幽灵条目 + 清孤儿封面。

        这一步就是"删除的媒体不再永久留在库里"的落点：旧增量扫描只增不减，
        现在只要跑过一次校验，索引与磁盘就一致。
        """
        valid = {str(k) for k in keys}
        media_keys = {k for k in valid if not k.startswith(COVER_KEY_PREFIX)}
        dropped = sorted(set(self._items) - media_keys)
        if dropped:
            merged = {k: v for k, v in self._items.items() if k in media_keys}
            self._publish_items(merged)
            self._index_dirty = True
        removed_rows = 0
        try:
            removed_rows = self._thumb_cache.prune(media_keys)
        except Exception as e:
            log.warning(f'[MediaPlayer] 清理孤儿封面失败: {e}')
        return {'dropped_items': len(dropped), 'thumb_rows_removed': removed_rows}

    def _freshness_pass_end(self, report, verified: bool) -> Dict[str, Any]:
        """整趟结束：落盘一次索引 + 更新进度计数（索引是单文件，不能每目录重写）。"""
        if self._index_dirty:
            self._save_index({'items': [i.to_dict() for i in self._items.values()],
                              'updated': self._now()})
            self._index_dirty = False
        audio = sum(1 for i in self._items.values() if i.kind == 'audio')
        video = sum(1 for i in self._items.values() if i.kind == 'video')
        return {'audio': audio, 'video': video, 'total': len(self._items),
                'verified': bool(verified)}

    def _freshness_rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃派生数据（索引 + 封面库）后重算。

        只清派生数据，**不动**收藏 / 最近播放 / 歌单 / 播放进度（`media_state.json`）。
        """
        cleared: Dict[str, Any] = {'kind': kind}
        try:
            self._publish_items({})
            if self._cache_file.exists():
                self._cache_file.unlink()
            cleared['index'] = True
        except OSError as e:
            cleared['index_error'] = str(e)
        try:
            self._thumb_cache.clear()
            cleared['thumbs'] = True
        except Exception as e:
            cleared['thumbs_error'] = str(e)
        return cleared

    # ===== 内部工具 =====

    def _refresh_covers(self, items: Dict[str, Any], dirs: set) -> Dict[str, Any]:
        """重算指定目录下条目的 `has_cover`（封面图加入/替换/删除后）。

        判据重新走一遍"目录封面图 **或** 内嵌封面"，而不是沿用旧值：沿用的话
        "删掉 folder.jpg" 之后标记仍然是 True，前端会一直请求一个不存在的封面。
        只对**受影响的目录**做（封面变化是低频事件），不做全库重查。
        """
        if not dirs:
            return items
        wanted = {str(d) for d in dirs}
        out: Dict[str, Any] = {}
        folder_cache: Dict[str, bool] = {}
        for key, item in items.items():
            parent = str(Path(item.path).parent)
            if parent not in wanted or item.kind != 'audio':
                out[key] = item
                continue
            if parent not in folder_cache:
                folder_cache[parent] = bool(find_cover(Path(parent)))
            has_cover = folder_cache[parent]
            if not has_cover:
                try:
                    has_cover = MetadataReader.has_embedded_cover(Path(item.path))
                except Exception:
                    has_cover = False
            if has_cover != item.has_cover:
                item.has_cover = has_cover
            out[key] = item
        return out

    def _namespace_for(self, path: Path) -> str:
        root = self._root_for(path)
        return root.name or str(root)

    def _root_for(self, path: Path) -> Path:
        """该文件属于哪个扫描根（多根时按最长前缀匹配；找不到时退回第一根）。"""
        best: Optional[Path] = None
        for root in self._scan_roots:
            try:
                if path.is_relative_to(root) and (best is None or len(str(root)) > len(str(best))):
                    best = root
            except (OSError, ValueError):
                continue
        return best or (self._scan_roots[0] if self._scan_roots else path.parent)

    def _rel_dir_for(self, path: Path) -> str:
        root = self._root_for(path)
        try:
            rel = path.parent.relative_to(root).as_posix()
        except ValueError:
            return ''
        return '' if rel == '.' else rel

    @staticmethod
    def _now() -> float:
        return time.time()
