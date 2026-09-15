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
'''

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)


class SettingsStore:
    """统一的插件设置存储，每个插件一个 JSON 文件，位于 <config_dir>/<plugin>.json

    两个写入要点（历史问题见 docs/code-review.md §4.1-2）：

    - **原子落盘**：tempfile + fsync + os.replace。以前是 open(w) 直接覆写，
      写到一半被杀就会留下损坏文件；而 get() 又把损坏文件静默当成"没有设置"，
      于是用户改过的设置悄悄回落到默认值、界面上没有任何提示。
    - **每个插件一把锁**：update() 是读-改-写，多线程（前端保存 + 插件运行期
      自行写状态）并发时会互相覆盖，后写的把先写的整段覆盖掉。
    """

    def __init__(self, settings_dir: str):
        self.settings_dir = Path(settings_dir)
        self.settings_dir.mkdir(parents=True, exist_ok=True)
        self._locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _file(self, plugin_name: str) -> Path:
        """拼接设置文件路径（<config_dir>/<plugin>.json）。

        plugin_name 用来拼文件名，因此必须是"裸文件名"：一旦允许分隔符或 `..`，
        一次 set()/update() 就能把 JSON 写到配置目录之外的任意可写路径（例如
        Linux 的 ~/.config/autostart/、Windows 的启动目录）。当前所有调用点传进来
        的都是插件文件夹名（不含分隔符），这里做显式校验是为了把这个前提变成
        代码保证，而不是"调用方记得别传坏值"。
        """
        if not isinstance(plugin_name, str):
            raise TypeError(f"插件名必须是字符串，实际是 {type(plugin_name).__name__}")
        name = plugin_name.strip()
        if (
            not name
            or name in ('.', '..')
            or name != Path(name).name
            or '/' in name
            or '\\' in name
            or '\x00' in name
        ):
            raise ValueError(f"非法插件名: {plugin_name!r}")
        return self.settings_dir / f"{name}.json"

    def path_for(self, plugin_name: str) -> Path:
        """该插件设置文件的绝对路径（公开访问器）。

        给需要"保护 / 备份 / 审计这个文件"的调用方用：路径规则只在 `_file()` 里
        定义一次。调用方不要自己拼 `<config>/plugins/<name>.json` —— 拼错不会报错，
        只会静默指向一个不存在的文件，于是"保护"看起来生效了其实什么都没护住。
        """
        return self._file(plugin_name)

    def _lock_for(self, plugin_name: str) -> threading.RLock:
        """取该插件专属的可重入锁（update 内部会再次进入 get/set）。"""
        with self._locks_guard:
            lock = self._locks.get(plugin_name)
            if lock is None:
                lock = threading.RLock()
                self._locks[plugin_name] = lock
            return lock

    def _quarantine(self, file: Path, error: Exception) -> None:
        """损坏的设置文件改名保底 + 显式报错，而不是静默回退默认值。

        静默的后果是"改了没生效、也不报错"，只能靠人猜。改名保留原始内容，
        便于人工恢复或排查；同一个损坏文件只会被隔离一次（之后文件已不存在）。
        """
        backup = file.with_name(f"{file.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(file, backup)
            log.info(f"[SettingsStore] 设置文件损坏，已备份为 {backup.name} 并回退默认值: {error}")
        except OSError as e:
            log.error(f"[SettingsStore] 设置文件损坏且无法备份 {file}: {e}")

    def get(self, plugin_name: str) -> Dict:
        file = self._file(plugin_name)
        with self._lock_for(plugin_name):
            if not file.exists():
                return {}
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                self._quarantine(file, e)
                return {}
            if not isinstance(data, dict):
                self._quarantine(file, ValueError(f"设置文件必须是 JSON 对象，实际是 {type(data).__name__}"))
                return {}
            return data

    def _atomic_write(self, file: Path, values: Dict) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f'.{file.name}.', suffix='.tmp', dir=str(file.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(values, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, file)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def set(self, plugin_name: str, values: Dict):
        if not isinstance(values, dict):
            raise ValueError("设置必须是字典")
        file = self._file(plugin_name)
        with self._lock_for(plugin_name):
            self._atomic_write(file, values)

    def clear(self, plugin_name: str):
        file = self._file(plugin_name)
        with self._lock_for(plugin_name):
            if file.exists():
                file.unlink()

    def update(self, plugin_name: str, values: Dict) -> Dict[str, Any]:
        """读-改-写在**同一把插件锁内**完成，返回合并后的完整设置。

        这是并发安全的写入原语：调用方自己 get() → 改 → set() 会把锁拆成两段，
        中间窗口内另一个写者（插件运行期落状态 / 前端保存设置）的更新会被整段覆盖
        （见 plugin_base.update_setting 与 save_settings 的历史用法）。
        """
        if not isinstance(values, dict):
            raise ValueError("设置必须是字典")
        with self._lock_for(plugin_name):
            current = self.get(plugin_name)
            current.update(values)
            self.set(plugin_name, current)
            return current
