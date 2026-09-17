"""多实例用例的共享小工具（不是被测代码，也不被 unittest 直接收集）。"""

from __future__ import annotations

import os
import shutil
import socket
import time
from pathlib import Path
from typing import Dict


def same_path(left, right) -> bool:
    """两个路径是否指向同一个位置（Windows 大小写 + 8.3 短名都要归一）。

    必要性：`tempfile` 给的根目录来自 `TEMP` 环境变量，在 Windows 上是 **8.3 短名**
    （`...\\ADMINI~1\\...`），而应用内部算出来的路径是长名。两者 string 不等却指同一
    目录 —— 直接比较字符串会得到一条与行为无关的失败。
    """
    return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(os.path.realpath(str(right)))


def cleanup_tree(path: Path, attempts: int = 6) -> None:
    """删除临时目录；Windows 上子进程刚退出时文件句柄可能还没释放，重试几次。

    实测现象：`PermissionError: [WinError 32] …\\.config\\logs\\omnibox.log`。它有两种
    来源，都真实踩到过：① 实例进程没被杀干净（见 `AppInstance._terminate_tree`），
    握着日志与 SQLite；② 杀毒/索引服务对刚写完的文件短暂占用。重试覆盖第二种，
    第一种由停机用例单独守（`tests/test_multi_instance_fixture.py`）。
    """
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.25 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)


def write_shared(shared: Path, files: Dict[str, bytes]) -> None:
    """把 `{相对路径: 字节}` 铺成一个共享目录（父目录按需创建）。"""
    for rel, payload in files.items():
        target = shared / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def cache_root_of(consumer, peer_id: str, share_id: str) -> Path:
    """物化根：`<消费者数据根>/group-mesh/.cache/remote/<真实设备ID>/<共享标识>`。"""
    return (Path(consumer.data_root) / 'group-mesh' / '.cache'
            / 'remote' / peer_id / share_id)


def port_is_free(port: int) -> bool:
    """端口现在能不能重新绑定 —— 孤儿实例还占着它时绑不上。

    **刻意不设 `SO_REUSEADDR`**：Windows 上它会允许绑到一个正在被监听的端口
    （实测：孤儿在 11515 上监听时，带 REUSEADDR 的探测照样成功），判据直接失效。
    """
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False
