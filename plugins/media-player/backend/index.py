"""媒体索引：条目快照、落盘与结构版本校验。

状态自持：条目字典、脏标记、索引文件路径都在这里。

不变量：**每次发布必须是新字典，发布之后内容不得再变化**。桥接 / HTTP 线程会直接
遍历 `items.values()`（search / recent / all_audio …），而刷新 worker 在检查点之间会
持续往工作字典里塞新条目 —— 绝不能把工作字典本身赋给 `items`，否则读取方会撞上
"dictionary changed size during iteration"（前端扫描期间 500ms 轮询会放大暴露）。

索引是单文件：整趟刷新只在结束（`on_pass_end`）与退出时落盘一次，中间用 `dirty`
标记攒改动；每处理一个目录就整体重写会把大库的扫描拖成写盘瓶颈。
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

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
        #: 当前快照是否来自落盘文件且尚未被本次运行的遍历覆盖。为真时 `get()`
        #: 会核对条目路径是否仍然存在；同步/校验发布新快照后置假。
        self.restored = False

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
        """把当前条目快照整体写盘（整趟结束 / 退出时各一次）。"""
        payload = {
            'version': self._version,
            'items': [item.to_dict() for item in self.items.values()],
            'updated': updated,
        }
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_file, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
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

        发布的条目来自刚刚完成的一次遍历，因此同时清掉 `restored`：之后的查询直接
        返回内存条目，不再逐条核对文件是否存在。
        """
        self.items = items
        self.restored = False

    def touch(self) -> None:
        """标记"有未落盘的改动"（下一次 `save()` 会写，整趟刷新中途不写）。"""
        self.dirty = True

    @property
    def authoritative(self) -> bool:
        """当前快照是否来自本次运行的实际遍历（False = 只是上次运行读回的）。"""
        return not self.restored

    def get(self, item_id: str) -> Optional[Any]:
        """按 id 取条目；来自落盘快照、路径已不存在的条目返回 None。

        `restored` 为真表示这份快照是上次运行读回来的，而本次运行还没同步过：文件可能
        已被删除或移动，这类条目不再回给调用方（否则界面会拿到指向不存在文件的条目）。
        同步/校验发布新快照后 `restored` 变假，查询回到纯内存字典查找。
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
