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

为什么必须有文件通道（docs/code-review.md §4.2）：发行版打包为 console=False，
此时 CPython 的 sys.stdout / sys.stderr 是 None，`print` 会**静默丢弃**所有
诊断信息 —— 发布版等于闭眼运行。日志落盘后，插件加载失败、扫描异常这类
问题才有据可查。

约定：
    - 内核与插件统一用 `log = logging.getLogger(__name__)`，不要用 print；
    - 只有面向终端的工具/脚本（tools/、tests/ 下的命令行脚本）才继续 print，
      因为它们的 stdout 就是产品本身；
    - 没有控制台时不注册 StreamHandler，更不会因为写 None 而报错。'''

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_ROOT = 'omnibox'
_LOG_FORMAT = '%(asctime)s %(levelname)-7s [%(name)s] %(message)s'
_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3

_configured = False


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
        file_handler = RotatingFileHandler(
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
