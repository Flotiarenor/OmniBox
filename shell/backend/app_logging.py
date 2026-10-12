'''
Copyright 2026 flotiarenor

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

统一日志配置：文件（滚动）+ 控制台双通道。

为什么必须有文件通道：发行版打包为 console=False，
此时 CPython 的 sys.stdout / sys.stderr 是 None，`print` 会**静默丢弃**所有
诊断信息 —— 发布版等于闭眼运行。日志落盘后，插件加载失败、扫描异常这类
问题才有据可查。

约定：
    - 内核与插件统一用 `log = logging.getLogger(__name__)`，不要用 print；
    - 只有面向终端的工具/脚本（tools/、tests/ 下的命令行脚本）才继续 print，
      因为它们的 stdout 就是产品本身；
    - 没有控制台时不注册 StreamHandler，更不会因为写 None 而报错。'''

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_ROOT = 'omnibox'
_LOG_FORMAT = '%(asctime)s %(levelname)-7s [%(name)s] %(message)s'
_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

_configured = False


class SafeRotatingFileHandler(RotatingFileHandler):
    """滚动日志：**不重命名当前文件**，因此不会因文件被占用而报错。

    标准库的 `RotatingFileHandler.doRollover()` 走的是
    `os.rename(omnibox.log, omnibox.log.1)`。Windows 上这一步会因为"文件被别的
    进程持有句柄"而抛 PermissionError（WinError 32）—— 本仓库里这是常态：应用在跑，
    同时又开着 `--web-only` 实例、e2e 用例或某个正在读这份日志的编辑器/杀软实时扫描。
    logging 把它当 handler 异常打到 stderr（就是用户看到的 "--- Logging error ---"
    加一整段调用栈），而且**那条日志本身也丢了**：看起来像程序崩了，实际只是滚动没做成。

    这里的做法是"先备份、再原子替换"：
      1. 空文件写进 `<log>.tmp`；
      2. 备份链整体后移（`.2` → `.3`、`.1` → `.2`）；
      3. 当前文件 → `.1`，再把 `.tmp` 换到 `<log>`。
    每一步都是 `os.replace`（同目录内原子）。当前文件只作为**源**被替换，不再被改名；
    仍然被占用时只抛 OSError，这里吞掉 —— 表现为**滚动延后到下一次写满**，写入不断、
    日志不丢、stderr 干净。

    备份链与标准库同名同义（`.1` 起，`backupCount` 个）。"""

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        try:
            self._rotate_via_replace()
        except OSError:
            # 文件被占着：这次不滚，继续写当前文件，下次写满再试
            pass
        if not self.delay:
            self.stream = self._open()

    def _rotate_via_replace(self) -> None:
        for index in range(self.backupCount - 1, 0, -1):
            source = Path(f'{self.baseFilename}.{index}')
            if source.exists():
                os.replace(source, f'{self.baseFilename}.{index + 1}')
        current = Path(self.baseFilename)
        if current.exists():
            # 先把当前内容挪进 .1，再用空文件顶掉它。顺序不能反：先建空文件的话
            # 旧内容会被这次替换直接丢掉（滚动是归档，不是清理）。
            os.replace(current, f'{self.baseFilename}.1')
        tmp = Path(f'{self.baseFilename}.tmp')
        # 与当前文件同目录：os.replace 才是原子的（跨卷会退化成复制 + 删除）
        tmp.touch()
        os.replace(tmp, self.baseFilename)

    def handleError(self, record: logging.LogRecord) -> None:
        """滚动/写入出问题时不要把 traceback 打到控制台。

        logging 默认走 `raiseExceptions` → stderr，用户看到的是整段
        "--- Logging error ---"+ 调用栈，像是程序崩了，其实只是那一条日志没落盘。
        这里连 stderr 都不写：启动路径上本来就有 `setup_logging` 的失败提示。
        """


def get_log_dir(config_dir) -> Path:
    """日志目录：<配置目录>/logs。"""
    return Path(config_dir) / 'logs'


def setup_logging(config_dir, level: int = logging.INFO, console: bool = True) -> Path | None:
    """配置根日志：滚动文件 + （有控制台时的）stderr。幂等，可重复调用。

    返回日志文件路径；文件通道建立失败时返回 None（此时日志只在控制台）。
    """
    global _configured
    root = logging.getLogger()
    if _configured:
        return getattr(root, '_omnibox_log_file', None)

    root.setLevel(level)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    log_file = None
    try:
        log_dir = get_log_dir(config_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'omnibox.log'
        file_handler = SafeRotatingFileHandler(
            log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:
        # 目录只读/磁盘满等：不能让"记日志"把启动搞挂
        log_file = None
        if getattr(sys, 'stderr', None) is not None:
            print(f'[logging] 无法创建日志文件，仅输出到控制台: {e}', file=sys.stderr)

    # 发行版 console=False 时 sys.stderr 为 None：注册 StreamHandler 会在写日志时炸
    stream = getattr(sys, 'stderr', None)
    if console and stream is not None:
        stream_handler = logging.StreamHandler(stream)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    if not root.handlers:
        root.addHandler(logging.NullHandler())

    root._omnibox_log_file = log_file  # type: ignore[attr-defined]
    _configured = True
    return log_file


def is_configured() -> bool:
    """是否已调用过 setup_logging（测试与诊断用）。"""
    return _configured


def reset_for_tests() -> None:
    """清空根日志的 handler 与状态（仅供测试）。"""
    global _configured
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    if hasattr(root, '_omnibox_log_file'):
        delattr(root, '_omnibox_log_file')
    _configured = False
