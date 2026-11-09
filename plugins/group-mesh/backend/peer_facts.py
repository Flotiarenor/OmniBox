"""对端的运行期事实：连不上的端点、各对端报的名单版本、最近一次采纳提示。

状态自持：三张表都在这里。写入方是 Discovery（探测与推送周期），读取方是 Peers
（`list_peers` 的 `errors` 与 `roster_versions`）与 StatusReport（首屏状态）——
两边都只经由本类的方法读写，因此谁改了什么不需要在两个组件之间对齐。

放在底层的原因：Discovery 与 Peers 互相都要用这份数据（一个写、一个读），
把它放在任何一侧都会让那一侧变成另一侧的下层，形成环。

名单版本的读写沿用装配期注入的那把 `lock`：与注册表合并、连接池共锁是**迁移前就有的
语义**，拆成独立的锁会改变并发行为。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

Endpoint = Tuple[str, int]


class PeerFacts:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        # 上一次刷新里连不上的端点 -> 失败原因（手动登记的地址失败时要回报给用户）
        self.unreachable: Dict[Endpoint, str] = {}
        # 对端名单版本记录（端点 -> {group, version, hash}）：界面据此做
        # "本机 v3 / 对方 v2" 的对照（宽限提示与分发可见性）。
        self.roster_notes: Dict[Endpoint, Dict[str, Any]] = {}
        # 最近一次自动采纳的新名单（界面提示"你的名单已从对方升级"）。
        self.notice: Optional[Dict[str, Any]] = None

    # ── 探测失败表 ────────────────────────────────────────────────────────

    def reset_unreachable(self) -> None:
        """新一轮探测开始：整表作废（探测线程随后往新表里写）。"""
        self.unreachable = {}

    def mark_unreachable(self, endpoint: Endpoint, reason: str) -> None:
        self.unreachable[endpoint] = reason

    def unreachable_list(self) -> List[Dict[str, str]]:
        """给界面看的"连不上的端点"列表（按端点排序，形状是 `endpoint` + `error`）。

        不能直接把 `unreachable` 交给界面：它的键是 `(host, port)` 元组，JSON 化时
        键必须是字符串，序列化会直接失败。
        """
        return [{'endpoint': f'{host}:{port}', 'error': reason}
                for (host, port), reason in sorted(self.unreachable.items())]

    # ── 名单版本表 ────────────────────────────────────────────────────────

    def note_roster(self, endpoint: Endpoint, note: Dict[str, Any]) -> None:
        with self._lock:
            self.roster_notes[endpoint] = note

    def notes_snapshot(self) -> Dict[Endpoint, Dict[str, Any]]:
        with self._lock:
            return dict(self.roster_notes)

    def version_list(self) -> List[Dict[str, Any]]:
        """给界面看的版本对照列表（按端点排序，形状是 `endpoint` + 记录字段）。"""
        return [{'endpoint': f'{host}:{port}', **note}
                for (host, port), note in sorted(self.roster_notes.items())]

    # ── 采纳提示 ──────────────────────────────────────────────────────────

    def set_notice(self, notice: Optional[Dict[str, Any]]) -> None:
        self.notice = notice
