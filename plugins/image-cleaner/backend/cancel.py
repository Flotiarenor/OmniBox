"""后台扫描的取消点。

只有入口加载本模块；分组与后台任务共用一个取消点，避免"某个入口忘了检查取消"。
为什么用异常而不是返回值：取消发生在嵌套循环的最内层（相似分组是 O(n²) 的两两比较），
逐层返回 False 会把每个调用点都改成"若取消则返回"，异常让取消点成为唯一出口。
"""

from typing import Any


class ScanCancelled(Exception):
    """扫描被取消（后台 worker 的取消点抛出，由 worker 吞掉并放弃写结果）。"""


def check_cancel(task: Any) -> None:
    """后台任务的取消点：任务已请求取消时抛出 `ScanCancelled`。

    `task` 为 None（同步入口）时什么都不做 —— 同步扫描没有取消语义。
    """
    if task is not None and task.cancelled:
        raise ScanCancelled()
