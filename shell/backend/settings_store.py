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
from typing import Any, Callable, Dict

log = logging.getLogger(__name__)

#: `os.replace` 的重试次数与单次等待。取值针对"外部扫描器短暂占用临时文件"这个已知成因：
#: Windows 上 mkstemp 出来的 `.tmp` 可能被实时扫描/索引在改名的那一瞬间抓住，此时
#: `os.replace` 报 WinError 5（拒绝访问）；实测该占用是瞬时的，等几十毫秒再试即可成功。
#: 等待总计 `RETRY_DELAY * (1+2+…)` ≈ 150ms（3 次重试），代价远低于"用户改动没落盘"。
_REPLACE_ATTEMPTS = 3
_REPLACE_DELAY = 0.05


class SettingsStore:
    """统一的插件设置存储，每个插件一个 JSON 文件，位于 <config_dir>/<plugin>.json

    两个写入要点：

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

    def _replace_with_retry(self, tmp_path: Path, file: Path) -> None:
        """`os.replace(tmp, file)`，对**瞬时**拒绝访问重试若干次。

        为什么需要：这一步是原子写的成败点，而它在 Windows 上会**偶发**失败 ——
        实测（全量测试期间）同一个临时目录下 `.shell.json.<随机>.tmp` 改名报
        `PermissionError: [WinError 5] 拒绝访问`，约 13 秒内连续出现 6 次，随后同样的
        请求立刻成功。成因是外部实时扫描/索引在改名瞬间持有该临时文件；本进程这边
        既没有别的写者（每文件一把锁），目标文件内容也完好。

        为什么只重试这一处：`mkstemp` / `json.dump` / `fsync` 的失败都是真实错误，
        重试只会把它们拖成"改了没生效、也没报错"。只有改名这一步的失败是"换个时间
        点就能成"的，因此重试范围收在这里。

        只对 `PermissionError` 重试：`FileNotFoundError`（临时目录被删）与
        `OSError` 的其它分支重试也不会变好，立刻抛出反而让调用方看到真实原因。

        次数用尽后仍抛原始异常：宁可让这次写入失败（前端会重试整次调用），也不要把
        文件留在一个被静默吞掉的状态里。
        """
        for attempt in range(_REPLACE_ATTEMPTS + 1):
            try:
                os.replace(tmp_path, file)
                if attempt:
                    log.warning(f'[SettingsStore] 改名 {file.name} 第 {attempt + 1} 次成功'
                                f'（前 {attempt} 次被占用）')
                return
            except PermissionError:
                # 最后一次不再等待：重试循环之外没有别的补救手段
                if attempt == _REPLACE_ATTEMPTS:
                    log.error(f'[SettingsStore] 改名 {file.name} 连续 '
                              f'{_REPLACE_ATTEMPTS + 1} 次被拒绝访问，放弃本次写入')
                    raise
                time.sleep(_REPLACE_DELAY * (attempt + 1))

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
            self._replace_with_retry(tmp_path, file)
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

    def transform(self, plugin_name: str, mutate: Callable[[Dict], Dict]) -> Dict:
        """在**同一把锁内**读-改-写，由调用方给出整份新内容，返回写入后的内容。

        与 `update()` 的区别是合并逻辑由调用方决定：`update()` 只做顶层浅合并，
        而壳偏好（`shell/backend/shell_prefs.py`）需要在写回前把读到的内容按键
        归一化，并且要能"拒绝这次写入"（`kind='import'` 在后端已有数据时原样返回）。
        两者共用同一把插件名锁，因此并发写不会互相丢更新。

        `mutate` 必须返回 dict：它拿到的是 `get()` 的结果，返回别的形状会写出一份
        非法设置文件 —— 这里显式拒绝，而不是让它落盘后再由下一次读去隔离。
        """
        if not callable(mutate):
            raise TypeError('mutate 必须是可调用对象')
        with self._lock_for(plugin_name):
            mutated = mutate(self.get(plugin_name))
            if not isinstance(mutated, dict):
                raise ValueError(
                    f'mutate 必须返回 dict，实际是 {type(mutated).__name__}')
            self.set(plugin_name, mutated)
            return mutated

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
