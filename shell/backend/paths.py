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

Cubby 统一路径基准。

- 开发模式：所有相对路径都锚定到项目根目录，不再依赖 os.getcwd()。
- 打包模式：可写数据（.config、data、logs、用户插件）落在**用户数据根**，与安装目录
  分离；只读资源（前端产物、内置插件）仍从 PyInstaller 的 _MEIPASS 读取。因此升级
  可以整目录替换安装位置，用户数据不动。

为什么必须分开：把数据放在可执行文件旁边时，"换一个版本的解压目录"就等于"换一个
数据目录"，用户每升级一次都要手工搬 `.config/` 与 `data/`；而按"安装目录是否可写"
决定落点，还让同一份程序在不同目录下有两种行为。2.0 起落点只由用户数据根本身决定，
便携是显式声明（见 `PORTABLE_MARKER`），不再靠探测。

`.config/webview` 是 WebView2 的 UserDataFolder（浏览器 profile 与 HTTP 缓存）。
它与 `.config` 下的其它内容同等对待：属于用户数据，必须落在
`get_user_data_dir()` 之下，不能进发行产物（`tools/check_build_tree.py` 的
`FORBIDDEN_RELATIVE` 已禁止产物出现首层 `.config`）。'''

import logging
import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import List

log = logging.getLogger(__name__)


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


#: 便携模式的标记文件：放在可执行文件旁边，表示"数据跟随程序目录"。
#:
#: 为什么是显式标记而不是"目录可写就算便携"：1.x 用可写探测决定数据落点，于是同一份
#: 程序解压到 D:\Cubby 是便携、放进 Program Files 就变成用户目录，行为随存放位置变化；
#: 用户升级时看到的是"新版本换了目录，历史记录留在旧文件夹里"。2.0 改成显式声明：
#: 只有用户自己放了标记文件才便携，其余一律落在用户数据根。
PORTABLE_MARKER = 'portable.txt'

# 实例数据根的环境变量覆盖。多实例与端到端测试用它把每个实例的状态
# （`.config`、`data`、日志、插件设置、身份私钥、缓存）落到各自的目录里。
#
# 为什么必须是环境变量而不是函数参数：`get_user_data_dir()` 是全仓**所有可写路径**
# 的派生根（get_config_dir / get_plugins_config_dir / resolve_data_root），而它在
# 开发模式下恒等于项目根目录 —— 同一台机器上起第二个实例就会与第一个共用 `data/`
# 与 `.config/`（同一份设备私钥、同一份注册表、同一份插件设置），表现是"两个实例
# 其实是同一台设备"；而且会在工作区里留下用户数据（AGENTS.md 明令不许进产物）。
# 环境变量在进程边界上生效，天然按实例隔离；它优先于下面所有判断。
USER_DATA_DIR_ENV = 'CUBBY_HOME'


def get_install_dir() -> Path:
    """程序所在的目录：只读资源与"这台机器上装的是什么"的判定基准。

    - 冻结产物：`sys.executable` 所在目录（便携标记、安装器的 `unins000.exe` 都在这
      里面判定）；
    - 开发模式：项目根目录。刻意不返回 `sys.executable` 的目录 —— 那会是 venv 里的
      Python 目录，形态判定读到的就成了解释器自己的文件，与"这份程序装在哪"无关。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return get_project_root()


def is_portable() -> bool:
    """是否便携模式：可执行文件旁存在 `portable.txt`。

    开发模式恒为 False（数据在项目根目录）；标记文件只对冻结产物起作用。
    """
    return is_frozen() and (get_install_dir() / PORTABLE_MARKER).is_file()


def _can_write_dir(candidate: Path) -> bool:
    """通过创建并删除临时文件探测目录是否可写。

    只服务于便携模式：用户显式要求数据跟随程序目录，而该目录不可写时（例如把便携版
    解压进了 Program Files）不能把数据写丢，也不能默默按用户数据根处理 —— 调用方
    退回用户数据根并记一条告警。
    """
    try:
        fd, tmp_name = tempfile.mkstemp(prefix='.cubby-write-test-', dir=str(candidate))
        os.close(fd)
        Path(tmp_name).unlink()
        return True
    except OSError:
        return False


def default_user_data_dir() -> Path:
    """与安装目录无关的用户数据根。

    - Windows：`%LOCALAPPDATA%/Cubby`（缺 `LOCALAPPDATA` 时退 `%APPDATA%/Cubby`）。
      用 Local 而不是 Roaming：`data/` 下有缩略图与索引，域环境的漫游配置文件会把
      它们整份同步到服务器。
    - 其他平台：`$XDG_DATA_HOME/cubby`，未设置时 `~/.local/share/cubby`。
    """
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
        if base:
            return Path(base) / 'Cubby'
        return Path.home() / 'AppData' / 'Local' / 'Cubby'
    xdg = os.environ.get('XDG_DATA_HOME', '').strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / '.local' / 'share'
    return base / 'cubby'


@lru_cache(maxsize=1)
def get_user_data_dir() -> Path:
    """返回可写的用户数据根目录（配置、插件、数据文件都锚定在这里）。

    解析顺序（先命中先赢）：

    1. 环境变量 `CUBBY_HOME`（`USER_DATA_DIR_ENV`）：多实例与端到端测试用它把每个
       实例的状态落到各自的目录里。注意它是**进程级**覆盖：值在首次调用时读入并缓存，
       之后改环境变量不会生效 —— 覆盖必须发生在任何 `get_config_dir()` 调用之前，入口
       `main()` 把它放在第一行就是这个原因。
    2. 开发模式（未冻结）：项目根目录，与 1.x 一致 —— 从源码运行时数据就在仓库里。
    3. 便携模式（`is_portable()`）：安装目录本身，数据跟随程序目录。
    4. 默认数据根（`default_user_data_dir()`）：与安装目录无关，换版本、换安装位置
       都不影响它。
    """
    override = os.environ.get(USER_DATA_DIR_ENV, '').strip()
    if override:
        return Path(override).expanduser().resolve()

    if not is_frozen():
        return get_project_root()

    if is_portable():
        exe_dir = get_install_dir()
        if _can_write_dir(exe_dir):
            return exe_dir
        # 便携标记写在了不可写目录里（例如 Program Files）：退回用户数据根，既不让
        # 数据落到一个写不进去的地方，也不把"显式才便携"改成"可写就便携"。
        log.warning('[paths] %s 不可写，便携模式退回 %s', exe_dir, default_user_data_dir())

    return default_user_data_dir()


def get_config_dir() -> Path:
    """主配置目录：<user_data>/.config"""
    return get_user_data_dir() / '.config'


def get_plugins_config_dir() -> Path:
    """插件设置目录：<user_data>/.config/plugins"""
    return get_config_dir() / 'plugins'


def get_webview_storage_dir() -> Path:
    """WebView2 的 UserDataFolder：<user_data>/.config/webview。

    为什么必须有它：pywebview 的 `private_mode` 默认为 True（见
    `venv/.../webview/__init__.py` 的 `private_mode: bool = True`），而 winforms
    后端在该模式下把 cache_dir 设成 `tempfile.TemporaryDirectory().name` ——
    **退出即删**。默认启动因此没有持久化的浏览器 profile：HTTP 缓存不落盘，
    每次启动都要把用过的图片重新取一遍；实测那张 6000×3000 / 13.66 MB 的原图
    在 WebView2 的任务管理器里显示为 `[InPrivate]`，即这一模式的表象。

    锚在 `.config` 之下而不是用户数据根目录：便携模式下根目录就是可执行文件所在
    目录，新增一个几十 MB、含 Cookie 与缓存碎片的目录会让"运行过程序后不可直接压缩
    发布"这件事多一处容易漏掉的入口；而 `.config` 已被
    `tools/check_build_tree.py` 列入产物禁止项，放在它下面天然受同一条门禁约束。

    开发模式与冻结产物走同一份解析（`get_user_data_dir()` 已按 `is_frozen()`
    分派），因此两种启动方式都得到"进程可写、且不随退出消失"的目录。
    """
    return get_config_dir() / 'webview'


def get_plugin_search_dirs() -> List[Path]:
    """返回插件搜索目录列表（排在前面的优先级更高）。

    - 开发模式：<项目根>/plugins
    - 打包模式：<用户数据根>/plugins（用户可再放入，跨版本保留）→ <_MEIPASS>/plugins（内置）
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
