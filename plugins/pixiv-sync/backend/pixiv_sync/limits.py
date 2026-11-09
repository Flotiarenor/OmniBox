"""设置项里的数量上限与并发数。

这些值在任务线程里被反复读取（每个画师、每个作品都可能取一次），集中在这里是为了
"越界值怎么兜底"只有一个答案：非法输入回落到默认值，而不是在调用点各写一份 try。
"""

from shell.backend.ports import SettingsPort


class SettingsLimits:
    def __init__(self, settings: SettingsPort) -> None:
        self._settings = settings

    def max_download(self) -> int:
        try:
            return max(0, int(self._settings.setting("max_download", 100)))
        except (TypeError, ValueError):
            return 100

    def max_artists(self) -> int:
        try:
            return max(0, int(self._settings.setting("max_artists", 30)))
        except (TypeError, ValueError):
            return 30

    def scan_workers(self) -> int:
        """刷新名单的并行拉取画师数（滑动窗口）。"""
        try:
            return max(1, min(8, int(self._settings.setting("scan_workers", 4))))
        except (TypeError, ValueError):
            return 4

    def workers(self) -> int:
        try:
            return max(1, min(8, int(self._settings.setting("workers", 4))))
        except (TypeError, ValueError):
            return 4
