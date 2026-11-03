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

    # ===== 读写 =====

    def load(self) -> None:
        """从索引文件重建条目；结构版本不符或解析失败时发布空索引（下次刷新重建）。"""
        if not self.cache_file.exists():
            return
        try:
            with open(self.cache_file, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            if data.get('version') != self._version:
                # 索引结构升级（如新增曲目号/专辑艺术家字段）时自动重建
                self.publish({})
                return
            # 先在本地字典里建好再一次性发布：边建边写会让读取方看到半成品
            loaded: Dict[str, Any] = {}
            for raw in data.get('items', []):
                item = self._models.MediaItem.from_cache(raw)
                loaded[item.id] = item
            self.publish(loaded)
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
        """把索引换成一份**新的**快照字典（唯一的写入入口）。"""
        self.items = items

    def touch(self) -> None:
        """标记"有未落盘的改动"（下一次 `save()` 会写，整趟刷新中途不写）。"""
        self.dirty = True

    def get(self, item_id: str) -> Optional[Any]:
        return self.items.get(item_id)
