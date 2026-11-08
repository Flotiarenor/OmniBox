"""统一刷新基建接入（同步 / 校验）。

把媒体库接进壳的「同步 / 校验」（`shell/backend/freshness.py`）。本组件回答三个问题：

- **管哪些文件**：全部扫描根下的音频/视频，外加目录封面图（`folder.jpg` 等）——
  封面不改索引条目，却决定 `has_cover`，所以必须进指纹（`include_names`）；
- **怎么算指纹**：条目键 = 绝对路径的 md5 前 16 位（`item_id`，索引与缩略图库
  都用它）；解析规则版本 = `scanner.index_version`，升级后由基建整体作废派生数据；
- **哪些条目需要重活**：`build_item()`（读标签 / 探时长）只对新增或指纹变化的
  文件调用，这是全流程唯一昂贵的一步。

修掉的两个旧问题：

1. **幽灵条目**：旧增量扫描只做 `merged[id] = item`，从磁盘删掉的媒体永久留在
   索引里（实测：删掉 b.mp3 后索引仍有两条），只有「深度扫描」能清。现在由
   `on_verified` 按有效键集合做差集删除 —— 校验是唯一入口，不必再"重读全库"。
2. **根级断点**：旧实现每个根目录完成才落一次检查点（`scan_task.json`），中断后
   以根为粒度续跑。现在指纹精确到目录，同步天然跳过未变化的目录，那套状态机
   （任务文件 + `completed_roots` + paused 恢复）整体删除。

状态自持：无。条目快照在 `index`，封面在 `covers`，根与路径在 `paths`；`engine_provider`
是入口给的闭包 —— 壳把刷新引擎挂在插件实例上，组件不持有宿主对象，只按需问"引擎在哪"。
"""

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

#: 封面文件在指纹库里的条目键前缀：`cover:<目录绝对路径>`。
#: 为什么要给封面单独一类键：它是"影响派生数据但不进索引"的文件。键里带上目录，
#: 于是"某目录的封面被加入/替换/删除"都能定位到要重算 `has_cover` 的那批条目。
COVER_KEY_PREFIX = 'cover:'


class FreshnessProvider:
    """向壳声明新鲜度规格，并实现它的钩子与兼容刷新入口。"""

    def __init__(self, paths: Any, index: Any, covers: Any, scanner: Any,
                 engine_provider: Callable[[], Optional[Any]]) -> None:
        self._paths = paths
        self._index = index
        self._covers = covers
        self._scanner = scanner
        self._engine = engine_provider

    # ===== 规格 =====

    def spec(self) -> Dict[str, Any]:
        """声明本插件的新鲜度规格（字段语义见 shell/backend/freshness.py）。"""
        return {
            # 可调用：媒体文件夹能在运行时增删（第一行是数据根，其余是额外扫描根）
            'roots': lambda: list(self._paths.scan_roots),
            'include': tuple(sorted(self._scanner.media_exts)),
            # 目录封面图纳入指纹：后补/替换一张 folder.jpg 不改任何媒体文件的
            # mtime，只能靠它把"这个目录变了"报出来（否则 has_cover 永远不刷新）
            'include_names': tuple(sorted(self._scanner.cover_names)),
            'key_of': self.key,
            'derive': self.derive,
            'prune': self.prune,
            'on_verified': self.audit,
            'on_pass_end': self.pass_end,
            'rebuild': self.rebuild,
            'content_version': self._scanner.index_version,
            'unit': '首',
            'min_sync_interval': 5.0,
            'max_dirs_per_sync': 400,
        }

    # ===== 键 =====

    def key(self, abs_path: str) -> str:
        """媒体文件 → item id；目录封面 → `cover:<目录>`（见 COVER_KEY_PREFIX）。"""
        path = Path(abs_path)
        if path.name.lower() in self._scanner.cover_names:
            return f'{COVER_KEY_PREFIX}{path.parent}'
        return self._scanner.item_id(abs_path)

    # ===== 钩子 =====

    def derive(self, items) -> Dict[str, Any]:
        """新增/指纹变化：构造或刷新条目；封面变化只刷新受影响目录的 `has_cover`。"""
        updated: Dict[str, Any] = {}
        errors: List[str] = []
        cover_dirs: set = set()
        for entry in items:
            key = str(entry.get('key') or '')
            if key.startswith(COVER_KEY_PREFIX):
                cover_dirs.add(key[len(COVER_KEY_PREFIX):])
                continue
            path = Path(entry['path'])
            namespace = self._paths.namespace_for(path)
            try:
                stat = path.stat()
            except OSError as error:
                errors.append(f'{path}: {error}')
                continue
            item = self._scanner.build_item(path, self._paths.root_for(path), namespace,
                                            self._paths.rel_dir_for(path), stat,
                                            self._scanner.find_cover(path.parent))
            if item is None:
                continue
            updated[item.id] = item

        merged = dict(self._index.items)
        merged.update(updated)
        if cover_dirs:
            merged = self._refresh_covers(merged, cover_dirs)
        if updated or cover_dirs:
            self._index.publish(merged)
            self._index.touch()
        return {'count': len(updated), 'errors': errors[:20]}

    def prune(self, keys) -> int:
        """条目消失：移出索引并删封面缓存（封面键 → 重算该目录的 has_cover）。"""
        dropped = 0
        cover_dirs: set = set()
        merged = dict(self._index.items)
        for key in keys:
            key = str(key)
            if key.startswith(COVER_KEY_PREFIX):
                cover_dirs.add(key[len(COVER_KEY_PREFIX):])
                continue
            if merged.pop(key, None) is not None:
                dropped += 1
                try:
                    self._covers.delete(key)
                except Exception as error:
                    log.warning(f'[MediaPlayer] 清理封面缓存失败 {key}: {error}')
        if cover_dirs:
            merged = self._refresh_covers(merged, cover_dirs)
        if dropped or cover_dirs:
            self._index.publish(merged)
            self._index.touch()
        return dropped

    def audit(self, keys) -> Dict[str, Any]:
        """全量校验后的对账：按有效键集合删幽灵条目 + 清孤儿封面。

        这一步就是"删除的媒体不再永久留在库里"的落点：旧增量扫描只增不减，
        现在只要跑过一次校验，索引与磁盘就一致。
        """
        valid = {str(key) for key in keys}
        media_keys = {key for key in valid if not key.startswith(COVER_KEY_PREFIX)}
        dropped = sorted(set(self._index.items) - media_keys)
        if dropped:
            merged = {key: value for key, value in self._index.items.items()
                      if key in media_keys}
            self._index.publish(merged)
            self._index.touch()
        removed_rows = 0
        try:
            removed_rows = self._covers.prune(media_keys)
        except Exception as error:
            log.warning(f'[MediaPlayer] 清理孤儿封面失败: {error}')
        return {'dropped_items': len(dropped), 'thumb_rows_removed': removed_rows}

    def pass_end(self, report, verified: bool) -> Dict[str, Any]:
        """整趟结束：落盘一次索引 + 更新进度计数（索引是单文件，不能每目录重写）。"""
        if self._index.dirty:
            self._index.save(time.time())
        audio = sum(1 for item in self._index.items.values() if item.kind == 'audio')
        video = sum(1 for item in self._index.items.values() if item.kind == 'video')
        return {'audio': audio, 'video': video, 'total': len(self._index.items),
                'verified': bool(verified)}

    def rebuild(self, kind: str = 'derived') -> Dict[str, Any]:
        """逃生门：丢弃派生数据（索引 + 封面库）后重算。

        只清派生数据，**不动**收藏 / 最近播放 / 歌单 / 播放进度（由 `state` 持有）。
        """
        cleared: Dict[str, Any] = {'kind': kind}
        try:
            self._index.publish({})
            self._index.clear_file()
            cleared['index'] = True
        except OSError as error:
            cleared['index_error'] = str(error)
        try:
            self._covers.clear()
            cleared['thumbs'] = True
        except Exception as error:
            cleared['thumbs_error'] = str(error)
        return cleared

    # ===== 壳的兼容入口 / 状态查询（API 表直接绑这几个） =====

    def scan(self, force: bool = False) -> Dict:
        """启动媒体库刷新（兼容入口 `media_scan`）。

        `force=False` 走「同步」（被动增量，目录级短路），`force=True` 走「校验」
        （全量逐项比对 + 幽灵清理）。旧实现的根级断点状态机已删除 —— 指纹精确到
        目录，续跑不再需要单独的任务文件。新代码请直接用统一刷新控件。
        """
        engine = self._engine()
        if engine is None:
            return {'success': False, 'error': '刷新基建未接入'}
        if force:
            started = engine.start_verify()
            return {'success': bool(started.get('started')), **started}
        report = engine.sync('', force=True)
        return {'success': True, 'started': True, 'report': report, **self.status()}

    def status(self) -> Dict:
        """扫描状态（前端轮询 `media_scan_status`）。

        契约与旧实现保持一致：`state` ∈ `none/done/cancelled/running`，另带
        `processed/total/current/extra`（audio/video）。

        - 校验在跑（或刚跑完）→ 直接投影 `BackgroundTask.status()`；
        - 没有任务时 → 若索引已经同步过，返回 `done` + 最近一趟的计数。同步是
          **同步调用**（无任务），把"没有任务"报成 `none` 会让轮询方以为"什么都没
          发生"，与旧的"扫描完成"契约不符。
        """
        engine = self._engine()
        if engine is None:
            return {'state': 'none'}
        state = engine.state()
        task = state.get('task')
        if task:
            return task
        report = state.get('last_report') or {}
        if not report:
            return {'state': 'none'}
        end = report.get('pass_end') or {}
        return {
            'state': 'done',
            'processed': report.get('dirs', 0),
            'total': report.get('dirs', 0),
            'current': '',
            'extra': {key: end.get(key) for key in ('audio', 'video', 'total') if key in end},
        }

    def cancel(self) -> Dict:
        """请求取消正在跑的全量校验（已写入的指纹与派生数据保留，可续跑）。"""
        engine = self._engine()
        if engine is None:
            return {'success': False, 'error': '刷新基建未接入'}
        return {'success': engine.request_cancel()}

    def shutdown(self) -> None:
        """进程退出收尾：取消在跑的校验并落盘索引。"""
        engine = self._engine()
        if engine is not None:
            engine.request_cancel()
        if not self._index.dirty:
            return
        try:
            self._index.save(time.time())
        except Exception as error:
            log.error(f'[MediaPlayer] 退出前保存索引失败: {error}')

    # ===== 内部工具 =====

    def _refresh_covers(self, items: Dict[str, Any], dirs: set) -> Dict[str, Any]:
        """重算指定目录下条目的 `has_cover` 与 `cover_mtime`（封面图加入/替换/删除后）。

        判据重新走一遍"目录封面图 **或** 内嵌封面"，而不是沿用旧值：沿用的话
        "删掉 folder.jpg" 之后标记仍然是 True，前端会一直请求一个不存在的封面。
        `cover_mtime` 一起重算同样是为了让替换封面后 URL 上的版本号前进：封面文件的
        指纹变了才会走到这里，此时不更新版本号就等于继续命中浏览器缓存。
        只对**受影响的目录**做（封面变化是低频事件），不做全库重查。
        """
        if not dirs:
            return items
        wanted = {str(directory) for directory in dirs}
        out: Dict[str, Any] = {}
        folder_cover: Dict[str, Optional[Path]] = {}
        for key, item in items.items():
            parent = str(Path(item.path).parent)
            if parent not in wanted:
                out[key] = item
                continue
            if parent not in folder_cover:
                folder_cover[parent] = self._scanner.find_cover(Path(parent))
            cover = folder_cover[parent]
            if item.kind == 'audio':
                has_cover = bool(cover)
                if not has_cover:
                    try:
                        has_cover = self._scanner.has_embedded_cover(Path(item.path))
                    except Exception:
                        has_cover = False
                if has_cover != item.has_cover:
                    item.has_cover = has_cover
            item.cover_mtime = self._scanner.cover_mtime(cover, item.mtime)
            out[key] = item
        return out
