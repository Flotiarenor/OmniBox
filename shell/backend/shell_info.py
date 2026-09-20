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

壳自身的信息与运维入口：本机路径、日志级别、缩略图缓存占用与清理。

这里**不放插件设置**：插件的设置项由插件自己的设置弹窗维护
（`/api/<插件>__get_settings` / `__save_settings`），壳只提供展示与清理所需的
系统级事实（`/api/system_get_shell_info` 等）。

为什么日志级别要落盘：它必须在下一次启动时、界面出现之前生效，所以只能存在后端
读得到的地方 —— `<config_dir>/shell.json`（复用 SettingsStore 的原子写入）。
唯一的写者就是本模块（插件写各自的 `<插件名>.json`），因此不存在"两个
SettingsStore 实例各持一把锁写同一个文件"的时间窗。
'''

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from shell.backend.app_logging import get_log_dir
from shell.backend.paths import get_config_dir
from shell.backend.settings_store import SettingsStore
from shell.backend.thumb_cache import cache_stats, clear_all_caches

log = logging.getLogger(__name__)

# 壳自身设置的文件名（SettingsStore 会拼成 <config_dir>/shell.json）
SHELL_SETTINGS_NAME = 'shell'
LOG_FILE_NAME = 'omnibox.log'
# 只留四档：DEBUG 会为每张缩略图记一行，量级与 INFO 差一个数量级，够排查就停。
LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR')


def _store(config_dir: Optional[str] = None) -> SettingsStore:
    return SettingsStore(str(config_dir or get_config_dir()))


def stored_log_level(config_dir: Optional[str] = None) -> int:
    """启动时该用的日志级别：读已保存的值，缺失或非法时回退 INFO。"""
    name = _store(config_dir).get(SHELL_SETTINGS_NAME).get('log_level')
    if isinstance(name, str) and name.upper() in LOG_LEVELS:
        return getattr(logging, name.upper())
    return logging.INFO


class ShellInfo:
    """壳自身信息 / 运维动作的实现（端点注册见 file_server 与 main.py）。"""

    def __init__(self, config: Optional[dict] = None, config_dir: Optional[str] = None):
        self._config = config or {}
        self._config_dir = Path(config_dir) if config_dir else get_config_dir()
        self._store = _store(str(self._config_dir))

    def get_info(self) -> dict:
        """集中设置页要展示的系统事实：路径 + 日志级别 + 缓存占用。"""
        log_dir = get_log_dir(self._config_dir)
        caches = cache_stats()
        return {
            'data_root': str(self._config.get('directories', {}).get('data_root', '')),
            'config_dir': str(self._config_dir),
            'log_dir': str(log_dir),
            'log_file': str(log_dir / LOG_FILE_NAME),
            'log_level': logging.getLevelName(logging.getLogger().level),
            'caches': caches,
            'cache_total': {
                'count': sum(item['count'] for item in caches),
                'bytes': sum(item['bytes'] for item in caches),
            },
        }

    def set_log_level(self, level) -> dict:
        """改日志级别：先落盘再生效，重启后仍是这一档。"""
        name = str(level or '').strip().upper()
        if name not in LOG_LEVELS:
            return {'success': False, 'error': f'不支持的日志级别: {level!r}'}
        self._store.update(SHELL_SETTINGS_NAME, {'log_level': name})
        logging.getLogger().setLevel(getattr(logging, name))
        log.info(f'[ShellInfo] 日志级别已改为 {name}')
        return {'success': True, 'level': name}

    def clear_thumb_caches(self) -> dict:
        """清空所有插件的缩略图缓存（下次浏览时按需重生成）。"""
        result = clear_all_caches()
        log.info(f"[ShellInfo] 已清空 {result['caches']} 个缩略图缓存，释放 {result['freed_bytes']} 字节")
        return {'success': True, **result}

    def open_log_dir(self) -> dict:
        """在文件管理器里打开日志目录。

        路径固定为 `<config_dir>/logs`，不接受调用方传入 —— 一旦能打开任意路径，
        这个端点就成了"用受信任的壳去启动任意程序 / 目录"的入口。
        """
        path = get_log_dir(self._config_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            opener = getattr(os, 'startfile', None)  # 仅 Windows 存在
            if opener is not None:
                opener(str(path))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except Exception as e:
            log.error(f'[ShellInfo] 打开日志目录失败: {e}')
            return {'success': False, 'error': str(e)}
        return {'success': True}
