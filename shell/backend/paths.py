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

OmniBox 统一路径基准。

- 开发模式：所有相对路径都锚定到项目根目录，不再依赖 os.getcwd()。
- 打包模式：可写数据（.config、plugins、data）放在可执行文件旁边；
  只读资源（shell、内置插件）仍从 PyInstaller 的 _MEIPASS 读取。
  如果可执行文件所在目录不可写，则回退到 %APPDATA%/OmniBox。'''

import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import List


def is_frozen() -> bool:
    return bool(getattr(sys, 'frozen', False))


def get_project_root() -> Path:
    """返回应用根目录。

    开发模式：本仓库根目录（shell/backend/paths.py 向上三级）。
    打包模式：PyInstaller 解包/收集目录（sys._MEIPASS）。
    """
    if is_frozen():
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return Path(meipass).resolve()
    return Path(__file__).resolve().parent.parent.parent


def _can_write_app_dir(candidate: Path) -> bool:
    """通过创建临时文件探测目录是否可写。"""
    try:
        probe_dir = candidate / '.config'
        probe_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix='.omnibox-write-test-', dir=str(probe_dir))
        os.close(fd)
        Path(tmp_name).unlink()
        return True
    except OSError:
        return False


# 实例数据根的环境变量覆盖。多实例与端到端测试用它把每个实例的状态
# （`.config`、`data`、日志、插件设置、身份私钥、缓存）落到各自的目录里。
#
# 为什么必须是环境变量而不是函数参数：`get_user_data_dir()` 是全仓**所有可写路径**
# 的派生根（get_config_dir / get_plugins_config_dir / resolve_data_root），而它在
# 开发模式下恒等于项目根目录 —— 同一台机器上起第二个实例就会与第一个共用 `data/`
# 与 `.config/`（同一份设备私钥、同一份注册表、同一份插件设置），表现是"两个实例
# 其实是同一台设备"；而且会在工作区里留下用户数据（AGENTS.md 明令不许进产物）。
# 环境变量在进程边界上生效，天然按实例隔离；未设置时行为与以前完全一致。
USER_DATA_DIR_ENV = 'OMNIBOX_HOME'


@lru_cache(maxsize=1)
def get_user_data_dir() -> Path:
    """返回可写的用户数据根目录（配置、插件、数据文件都锚定在这里）。

    设了环境变量 `OMNIBOX_HOME`（`USER_DATA_DIR_ENV`）就整体挪到那个目录，供多实例
    与端到端测试使用。注意它是**进程级**覆盖：值在首次调用时读入并缓存，之后改
    环境变量不会生效 —— 覆盖必须发生在任何 `get_config_dir()` 调用之前，入口
    `main()` 把它放在第一行就是这个原因。
    """
    override = os.environ.get(USER_DATA_DIR_ENV, '').strip()
    if override:
        return Path(override).expanduser().resolve()

    if not is_frozen():
        return get_project_root()

    exe_dir = Path(sys.executable).resolve().parent
    if _can_write_app_dir(exe_dir):
        return exe_dir

    # 程序目录只读时（例如安装在 Program Files 后直接运行），回退到用户目录。
    appdata = os.environ.get('APPDATA')
    fallback = Path(appdata) if appdata else Path.home()
    return (fallback / 'OmniBox').resolve()


def get_config_dir() -> Path:
    """主配置目录：<user_data>/.config"""
    return get_user_data_dir() / '.config'


def get_plugins_config_dir() -> Path:
    """插件设置目录：<user_data>/.config/plugins"""
    return get_config_dir() / 'plugins'


def get_plugin_search_dirs() -> List[Path]:
    """返回插件搜索目录列表（排在前面的优先级更高）。

    - 开发模式：<项目根>/plugins
    - 打包模式：<可执行文件旁>/plugins（用户可再放入）→ <_MEIPASS>/plugins（内置）
    """
    dirs: List[Path] = []

    def add(path: Path) -> None:
        try:
            resolved = Path(path).resolve()
        except OSError:
            return
        if resolved not in dirs:
            dirs.append(resolved)

    if is_frozen():
        add(get_user_data_dir() / 'plugins')
        add(get_project_root() / 'plugins')
    else:
        add(get_project_root() / 'plugins')
    return dirs


def resolve_data_root(data_root: str) -> Path:
    """把配置里的 data_root 解析为绝对路径。

    相对路径统一锚定到 get_user_data_dir()，避免依赖启动时的工作目录。
    """
    path = Path(data_root).expanduser()
    if not path.is_absolute():
        path = get_user_data_dir() / path
    return path.resolve()
