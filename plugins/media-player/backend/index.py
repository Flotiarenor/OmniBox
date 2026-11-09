"""媒体索引：条目快照、落盘与结构版本校验。

状态自持：条目字典、脏标记、索引文件路径都在这里。

不变量：**每次发布必须是新字典，发布之后内容不得再变化**。桥接 / HTTP 线程会直接
遍历 `items.values()`（search / recent / all_audio …），而刷新 worker 在检查点之间会
持续往工作字典里塞新条目 —— 绝不能把工作字典本身赋给 `items`，否则读取方会撞上
"dictionary changed size during iteration"（前端扫描期间 500ms 轮询会放大暴露）。

索引是单文件：整趟刷新只在结束（`on_pass_end`）与退出时落盘一次，中间用 `dirty`
标记攒改动；每处理一个目录就整体重写会把大库的扫描拖成写盘瓶颈。两次落盘的调用来自
不同线程（校验线程与卸载调用方），因此写入串行化在实例锁上，并且是原子写。
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from shell.backend.json_cache import write_json

log = logging.getLogger(__name__)


class MediaIndex:
    """条目快照与 `media_index.json` 的读写。"""

    def __init__(self, cache_file: Path, version: int, models: Any) -> None:
        self._models = models
        self._version = version
        self.cache_file = cache_file
        self.items: Dict[str, Any] = {}
        self.dirty = False
        #: 从落盘索引读回的时刻（0 = 本次运行还没读过盘）与文件里记录的解析规则版本。
        #: 读回的条目是上一次运行留下的观察结果，文件在两次运行之间可能被删除或移动。
        self.loaded_at = 0.0
        self.loaded_version = 0
        #: 当前快照是否来自落盘文件且本进程还没跑过一趟**完整**遍历。为真时 `get()`
        #: 会核对条目路径是否仍然存在（见 `mark_authoritative`）。
        self.restored = False
        #: 落盘串行化：`on_pass_end`（每趟结束）与 `on_unload`（退出）来自不同线程。
        self._lock = threading.Lock()

    # ===== 读写 =====

    def load(self) -> None:
        """从索引文件重建条目；结构版本不符或解析失败时发布空索引（下次刷新重建）。

        读回成功时记下 `loaded_at` / `loaded_version` 并把 `restored` 置真：这份快照
        只代表上一次运行看到的文件集合，`get()` 因此会核对路径是否还在。
        """
        self.loaded_at = 0.0
        self.loaded_version = 0
        self.restored = False
        if not self.cache_file.exists():
            return
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            version = data.get('version')
            if isinstance(version, int):
                self.loaded_version = version
            if version != self._version:
                # 索引结构升级（如新增曲目号/专辑艺术家字段）时自动重建
                self.publish({})
                return
            # 先在本地字典里建好再一次性发布：边建边写会让读取方看到半成品
            loaded: Dict[str, Any] = {}
            for raw in data.get('items', []):
                item = self._models.MediaItem.from_cache(raw)
                loaded[item.id] = item
            self.publish(loaded)
            self.loaded_at = time.time()
            self.restored = bool(loaded)
        except Exception:
            self.publish({})

    def save(self, updated: float) -> None:
        """把当前条目快照整体写盘（整趟结束 / 退出时各一次）。

        原子写（同目录临时文件 + `flush` + `fsync` + `os.replace`，实现复用
        `shell.backend.json_cache.write_json`）：直接 `open('w')` 覆写时写到一半被杀会留下
        截断的 JSON，下次启动解析失败退化成空索引、整库重建一遍。写入还串行化在实例锁上：
        `on_pass_end`（每趟结束，校验线程）与 `on_unload`（退出，调用方线程）都会走到这里。
        写失败时保留 `dirty`，下一趟收尾再试一次。
        """
        payload = {
            'version': self._version,
            'items': [item.to_dict() for item in self.items.values()],
            'updated': updated,
        }
        with self._lock:
            if not write_json(self.cache_file, payload, indent=2, create_parent=True):
                log.error(f'[MediaPlayer] 保存媒体索引失败: {self.cache_file}')
                return
            self.dirty = False

    def clear_file(self) -> bool:
        """删掉索引文件（派生数据重建用）；返回是否真的删掉了。"""
        if not self.cache_file.exists():
            return False
        self.cache_file.unlink()
        return True

    def bind(self, cache_file: Path) -> None:
        """换根后指向新的索引文件（入口随后调 `load()` 重读）。"""
        self.cache_file = cache_file
        self.publish({})

    # ===== 发布与查询 =====

    def publish(self, items: Dict[str, Any]) -> None:
        """把索引换成一份**新的**快照字典（唯一的写入入口）。

        **不碰 `restored`**：发布只说明"这份条目来自某次遍历"，不说明"整趟遍历走完了"。
        预算用尽的部分遍历只覆盖一部分目录，其余目录的条目仍是上次运行留下的观察结果，
        按 id 取它们时仍要核对路径。整趟走完由 `mark_authoritative()` 宣布。
        """
        self.items = items

    def mark_authoritative(self) -> None:
        """宣布当前快照是本进程一趟**完整**遍历的结果（整趟、非 `partial`、无读取错误）。

        由插件 `freshness_spec` 的 `on_pass_end` 在报告满足条件时调用：此后 `get()` 不再
        逐条核对路径 —— 没走到的目录已在同一趟里被对账掉，剩下的条目都来自本次观察。
        """
        self.restored = False

    def touch(self) -> None:
        """标记"有未落盘的改动"（下一次 `save()` 会写，整趟刷新中途不写）。"""
        self.dirty = True

    @property
    def authoritative(self) -> bool:
        """当前快照是否来自本进程的实际遍历（False = 仍含上次运行读回的条目）。"""
        return not self.restored

    def get(self, item_id: str) -> Optional[Any]:
        """按 id 取条目；来自落盘快照、路径已不存在的条目返回 None。

        `restored` 为真表示这份快照来自上次运行、本进程还没跑完一趟完整遍历：文件可能
        已被删除或移动，这类条目不再回给调用方（否则界面会拿到指向不存在文件的条目）。
        只有 `mark_authoritative()` 能把 `restored` 变假，查询才回到纯内存字典查找。
        """
        item = self.items.get(item_id)
        if item is None or not self.restored:
            return item
        if self.path_exists(item):
            return item
        return None

    @staticmethod
    def path_exists(item: Any) -> bool:
        """条目记录的文件此刻是否还在；空路径按"不在"处理。"""
        path = str(getattr(item, 'path', '') or '')
        if not path:
            return False
        try:
            return Path(path).exists()
        except (OSError, ValueError):
            return False
