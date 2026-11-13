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

import atexit
import importlib.util
import json
import logging
import os
import platform
import re
import sys
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import requests
import webview

from shell.backend import update_check
from shell.backend.app_logging import setup_logging
from shell.backend.auth import get_or_create_token, get_token_file
from shell.backend.file_dialog import pick_path
from shell.backend.file_server import create_app
from shell.backend.json_cache import write_json
from shell.backend.media_catalog import list_subdirectories
from shell.backend.paths import (
    USER_DATA_DIR_ENV,
    get_config_dir,
    get_plugin_search_dirs,
    get_user_data_dir,
    get_webview_storage_dir,
    resolve_data_root,
)
from shell.backend.plugin_manager import PluginManager
from shell.backend.shell_info import ShellInfo, stored_log_level
from shell.backend.shell_prefs import ShellPrefs
from shell.backend.window_state import (
    get_state as window_fullscreen_state,
)
from shell.backend.window_state import (
    register_window as register_desktop_window,
)
from shell.backend.window_state import (
    set_fullscreen as set_window_fullscreen,
)
from shell.backend.window_state import (
    toggle_fullscreen as toggle_window_fullscreen,
)

log = logging.getLogger(__name__)

# ============================================================
# 主配置：JSON 落点、默认值与旧版 YAML 迁移
# ============================================================
# 为什么是 JSON 而不是 YAML：这个文件只有四个叶子键，日常使用不看也不改；而仓库里其余
# 全部持久化（`shell.json`、`.config/plugins/*.json`、各插件状态）都是 JSON。统一一种
# 格式之后读、写都落在标准库上，并且"同一份配置只有一种读法"：旧版落点仍会被读一次并
# 转存，读法见 `_read_legacy_config`。
CONFIG_FILE_NAME = 'app.json'

DEFAULT_CONFIG = {
    'server': {
        'host': '127.0.0.1',
        'port': 18080,
        # 允许访问的 Host 白名单（端口不参与匹配）。默认已放行
        # 127.0.0.1/localhost；把 host 改成 0.0.0.0 供局域网访问时，
        # 本机网卡 IP 会自动放行，NAT/反代等场景可在此显式补充。
        # 留空不是"不校验"——未列入的 Host 一律 400（防 DNS rebinding）。
        'trusted_hosts': [],
    },
    'directories': {'data_root': './data'},
}


def _legacy_config_paths() -> list[Path]:
    """旧版配置的落点，按"从新到旧"排列。

    `<config>/app.yaml` 是上一版的位置；`<user_data>/config.yaml` 更早（那时配置还放在
    用户数据根目录，后来才收进 `.config`）。两者都只用于一次性迁移。
    """
    return [get_config_dir() / 'app.yaml', get_user_data_dir() / 'config.yaml']


#: 旧版 YAML 里一行"键: 值"的形态（值可以为空，表示下面跟着一个块）
_LEGACY_KEY_LINE = re.compile(r'^(?P<key>[A-Za-z_][A-Za-z0-9_.-]*):(?P<rest>.*)$')


def _legacy_scalar(raw: str) -> object:
    """解析旧版 YAML 的一个标量行；返回字符串、列表，或 None 表示"这个键下面是块"。

    只处理四种形态：不带引号、单引号、双引号（去掉引号），以及列表（`[]` 或 `[a, b]`）。
    行尾的 `#` 注释按 YAML 规则只在引号外、且前面是空白时才算注释；引号内的 `#` 属于值。
    """
    quote = ''
    cut = len(raw)
    for index, char in enumerate(raw):
        if quote:
            if char == quote:
                quote = ''
            continue
        if char in ('"', "'"):
            quote = char
        elif char == '#' and (index == 0 or raw[index - 1] in ' \t'):
            cut = index
            break
    value = raw[:cut].strip()
    if not value:
        return None
    if value == '{}':
        return {}
    if value.startswith('[') and value.endswith(']'):
        return [_unquote(part.strip()) for part in value[1:-1].split(',') if part.strip()]
    return _unquote(value)


def _unquote(value: str) -> str:
    """去掉成对的引号（只认同一种引号成对，不做转义处理）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _read_legacy_config(path: Path) -> dict | None:
    """读旧版 `app.yaml` / `config.yaml`，返回嵌套 dict；无法解析时返回 None。

    只为一次性迁移存在：内核不 import PyYAML，而老用户的旧文件里可能有自己改过的
    host / port / trusted_hosts / data_root，直接丢默认值等于把这些设置清掉。
    支持的写法就是旧版默认配置与文档示例用到的全部：

    * 两层映射，子键缩进两个空格；
    * 标量：不带引号 / 单引号 / 双引号，行尾注释；
    * 列表：`key: []`、`key: [a, b]`、以及 `key:` 后跟 `- 值` 的块（PyYAML 默认输出
      的块状列表把 `-` 与键放在同一缩进，因此两空格与四空格都要认）。

    解析失败一律返回 None 而不是"尽量读出来"：迁移读错值比迁移失败更糟——`data_root`
    读成半截字符串会把数据根换到别处，而调用方对 None 的处理是"保留旧文件、本次用默认值"。
    """
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None

    data: dict = {}
    section: str | None = None
    pending_list: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        if '\t' in raw[:indent + 1]:
            return None
        body = raw.strip()

        if indent == 0:
            match = _LEGACY_KEY_LINE.match(body)
            if not match:
                return None
            section = match.group('key')
            pending_list = None
            value = _legacy_scalar(match.group('rest'))
            if value is None:
                data[section] = {}
                pending_list = section       # 紧跟的 `- 值` 属于这个键
            else:
                data[section] = value
            continue

        block = data.get(section) if section else None
        if not isinstance(block, dict):
            return None

        if body.startswith('- '):
            if pending_list is None:
                return None
            item = _legacy_scalar(body[2:])
            if item is None or isinstance(item, list):
                return None
            current = block.get(pending_list)
            if isinstance(current, list):
                current.append(item)
            elif current == {}:
                block[pending_list] = [item]
            else:
                return None
            continue

        if indent != 2:
            return None
        match = _LEGACY_KEY_LINE.match(body)
        if not match:
            return None
        key = match.group('key')
        value = _legacy_scalar(match.group('rest'))
        pending_list = key if value is None else None
        block[key] = {} if value is None else value

    return data


def _retire_legacy_config(path: Path) -> None:
    """把已迁移的旧文件改名保留（不删除），避免同名覆盖掉用户上一次的备份。"""
    for index in range(100):
        suffix = '.migrated' if index == 0 else f'.migrated-{index + 1}'
        target = path.with_name(path.name + suffix)
        if target.exists():
            continue
        try:
            path.rename(target)
        except OSError as error:
            log.warning(f"[Cubby] 旧配置已迁移，但改名失败（可手动删除 {path}）: {error}")
            return
        log.info(f"[Cubby] 旧配置已改名保留: {target}")
        return


def _read_config_file(path: Path) -> dict | None:
    """读 `app.json`；文件不可读、不是 JSON、顶层不是对象时返回 None。"""
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _coerce_port(value: object) -> int | None:
    """把配置里的端口归一成可用的整数；非法值返回 None。

    手改配置时把端口写成字符串（`"port": "18080"`）是最常见的笔误，而下游
    `_resolve_port` 直接 `int()` 取值：不归一的话一个引号就能让启动失败。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
    else:
        return None
    return port if 1 <= port <= 65535 else None


def _coerce_host_list(value: object) -> list[str]:
    """把 `trusted_hosts` 归一成字符串列表（单个字符串按一项处理）。"""
    if value is None:
        return []
    if isinstance(value, str):
        items: list = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        log.warning(f"[Cubby] server.trusted_hosts 不是列表，已忽略: {value!r}")
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def _normalize_config(config: dict) -> dict:
    """补齐默认值并校正字段类型；幂等（同一份输入反复调用结果不变）。

    手改配置时最容易踩的两处是类型：`port` 写成字符串、`trusted_hosts` 写成单个字符串
    （整条白名单会因"不是列表"被静默忽略）。这里就地纠正并留下说明日志，与 `data_root`
    的既有做法一致 —— 宁可带着日志继续启动，也不要因为一个引号退出。

    只在**键存在**时告警：缺键是正常情况（用户删掉某项想用默认值），按默认值补上即可，
    把它报成"无效"只会制造噪音。
    """
    defaults = deepcopy(DEFAULT_CONFIG)

    server = config.get('server')
    if not isinstance(server, dict):
        if server is not None:
            log.error("[Cubby] 配置项 server 格式错误，已重置为默认值")
        server = {}
    config['server'] = server

    if 'host' in server:
        host = server['host']
        if isinstance(host, str) and host.strip():
            server['host'] = host.strip()
        else:
            log.warning(f"[Cubby] server.host 无效，已使用默认值: {host!r}")
            server['host'] = defaults['server']['host']
    if 'port' in server:
        port = _coerce_port(server['port'])
        if port is None:
            log.warning(f"[Cubby] server.port 无效，已使用默认值: {server['port']!r}")
            server['port'] = defaults['server']['port']
        else:
            server['port'] = port
    if 'trusted_hosts' in server:
        server['trusted_hosts'] = _coerce_host_list(server['trusted_hosts'])
    for key, value in defaults['server'].items():
        server.setdefault(key, deepcopy(value))

    directories = config.get('directories')
    if not isinstance(directories, dict):
        if directories is not None:
            log.error("[Cubby] 配置项 directories 格式错误，已重置为默认值")
        directories = {}
    config['directories'] = directories
    if 'data_root' in directories:
        data_root = directories['data_root']
        if isinstance(data_root, str) and data_root.strip():
            directories['data_root'] = data_root.strip()
        else:
            log.warning(f"[Cubby] directories.data_root 无效，已使用默认值: {data_root!r}")
            directories['data_root'] = defaults['directories']['data_root']
    for key, value in defaults['directories'].items():
        directories.setdefault(key, value)
    return config


def _migrate_legacy_config(cfg_path: Path) -> tuple[bool, dict | None]:
    """把旧版 YAML 落点一次性转存为 `app.json`。

    返回 `(是否发现旧文件, 迁移来的配置)`。发现旧文件但读不出来时第二项为 None，调用方
    据此**不写** `app.json` —— 下次启动会再试一次，用户改过的设置不会被一组默认值顶掉。
    转存成功才把旧文件改名（`.migrated`，保留内容）。
    """
    for old_path in _legacy_config_paths():
        if not old_path.exists():
            continue
        legacy = _read_legacy_config(old_path)
        if legacy is None:
            log.error(f"[Cubby] 旧版配置无法解析，本次使用默认值（旧文件保留，下次启动再试）: {old_path}")
            return True, None
        migrated = _normalize_config(legacy)
        if not write_json(cfg_path, migrated, create_parent=True):
            log.error(f"[Cubby] 旧配置迁移写入失败，本次沿用旧配置值: {cfg_path}")
        else:
            log.info(f"[Cubby] 配置已迁移: {old_path} → {cfg_path}")
            _retire_legacy_config(old_path)
        return True, migrated
    return False, None


def load_config() -> dict:
    """读主配置；缺失时建默认值、有旧版落点时迁移，返回值里的 data_root 是绝对路径。"""
    cfg_path = get_config_dir() / CONFIG_FILE_NAME

    if cfg_path.exists():
        config = _read_config_file(cfg_path)
        if config is None:
            # 文件在但读不出来：不覆写用户文件（可能只是改到一半），本次用默认值
            log.error(f"[Cubby] 配置文件无法解析，本次使用默认配置: {cfg_path}")
            config = deepcopy(DEFAULT_CONFIG)
    else:
        found, migrated = _migrate_legacy_config(cfg_path)
        if migrated is not None:
            config = migrated
        else:
            config = deepcopy(DEFAULT_CONFIG)
            # 发现旧文件却读不出来时不落盘：留给下次启动重试（原因已在上面记过）
            if not found and not write_json(cfg_path, config, create_parent=True):
                log.error(f"[Cubby] 默认配置写入失败: {cfg_path}")
            elif not found:
                log.info(f"[Cubby] 创建默认配置: {cfg_path}")

    config = _normalize_config(config)
    # 相对路径 data_root 统一锚定到用户数据目录
    config['directories']['data_root'] = str(resolve_data_root(config['directories']['data_root']))
    return config


def _argv_value(flag: str) -> str | None:
    """取 `--flag value` 形式的参数值；没有该参数、或值缺失/是另一个开关时返回 None。

    刻意不用 argparse：入口既有的两个开关（`--web-only`、`--status-debug`）都是
    `in sys.argv` 判断，混进一个解析器会让"未识别的参数"从静默忽略变成直接退出 ——
    而打包产物是用固定命令行启动的，多一个必填解析器就多一处启动失败面。同时
    `--port` 原先就允许"值非法时回退配置端口"，这里保持同一语义。
    """
    argv = sys.argv
    if flag not in argv:
        return None
    index = argv.index(flag) + 1
    if index >= len(argv):
        return None
    value = argv[index].strip()
    if not value or value.startswith('--'):
        return None
    return value


def wait_for_server(host, port, timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        try:
            # 显式关闭代理：requests 默认读取 HTTP_PROXY/HTTPS_PROXY，环境里存在
            # 代理时探活请求会被交给代理（实测 ProxyError），Flask 已监听也会被判成
            # 启动超时。本请求只访问本机 Flask，不走代理。
            r = requests.get(
                f"http://{host}:{port}/health", timeout=0.5,
                # requests 运行期用 None 表示"禁用该协议的代理"；其类型 stub 只声明
                # str，故加定点 ignore（见 requests 文档的 proxies 用法）。
                proxies={'http': None, 'https': None},  # type: ignore[arg-type]
            )
            if r.status_code == 200:
                return True
        except requests.RequestException:
            # 服务还没起来：连接被拒/超时都属正常，继续轮询等它起来。
            # 不能用裸 except —— 那会把 Ctrl+C(KeyboardInterrupt) 一起吞掉，
            # 用户在启动阶段按 Ctrl+C 将无法中断。
            pass
        time.sleep(0.1)
    return False

def _commit_last_route():
    """把内存里待提交的壳偏好（`last_route`）落盘：安全退出的统一切口。

    调用点只有三处，且都在"用户主动结束"的路径上：

    - 桌面窗口：`webview.start()` **正常返回**之后（关窗退出）。刻意不放在 `finally`：
      异常展开、崩溃与被强杀都不算安全退出，那时应当保留上一次安全退出的值；
    - 浏览器 / `--web-only`：`app.run()` 返回之后（Ctrl+C 或服务被要求退出），
      由 `finally` 兜住。浏览器模式没有"关窗"事件，只能以服务进程的结束为准；
    - `atexit` 兜底（覆盖 `main()` 提前返回等路径）。

    幂等：没有待提交值时不写盘（见 `ShellPrefs.commit_last_route`）。
    """
    try:
        ShellPrefs(str(get_config_dir())).commit_last_route()
    except Exception as e:
        log.error(f"[Cubby] 提交上次页面失败: {e}")


def _unload_plugins(manager):
    """进程退出前卸载插件：回调每个插件的 on_unload。

    这是插件唯一的收尾钩子（关 SQLite/WAL、停后台线程、落盘最后一次状态），
    以前全仓没有任何调用点。幂等，可安全重复调用。
    """
    try:
        manager.unload_all()
    except Exception as e:
        log.error(f"[Cubby] 卸载插件时出错: {e}")


def _toggle_fullscreen():
    """切换桌面窗口全屏（实现见 shell/backend/window_state.py）。"""
    return toggle_window_fullscreen()


def _resolve_port(config) -> int:
    """本次启动真正监听的 HTTP 端口：`--port` 覆盖配置，值非法时回退配置值。

    覆盖的用途：nginx 反代时后端监听内网端口；多实例夹具靠它让每个实例各占一个
    端口（"不同端口"就是它模拟出的"不同网络"）。
    """
    port = int(config['server']['port'])
    override = _argv_value('--port')
    if not override:
        return port
    try:
        return int(override)
    except ValueError:
        log.warning(f"[Cubby] --port 不是整数，按配置端口 {port} 启动: {override!r}")
        return port


#: 品牌图标的源文件（多尺寸 ICO）：Windows 的 winforms/WebView2 只认 ICO，直接用它
_BRAND_ICON_RELATIVE = Path('res') / 'brand' / 'cubby-blue.ico'

#: Linux 上给 GTK 的窗口图标缓存文件名（放在配置目录下）
_LINUX_ICON_CACHE_NAME = 'brand-icon.png'


def _brand_icon_source_path() -> Path:
    """`res/brand/cubby-blue.ico` 的绝对路径；冻结形态在 `_MEIPASS` 下与仓库同构。"""
    root = Path(getattr(sys, '_MEIPASS', '')) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    return root / _BRAND_ICON_RELATIVE


def _write_largest_ico_frame_as_png(source: Path, target: Path) -> None:
    """把 ICO 里像素最多的一帧写成 PNG，原子替换到 `target`。

    ICO 是多尺寸容器，窗口图标要最大的那份才清晰；Pillow 的 ICO 插件把各帧列在
    `ico.sizes()` 里、按帧取图用 `ico.getimage()`（这两个属性不在 Pillow 的公开
    类型声明里，故按 Any 访问；取不到就退回 Pillow 默认打开的那一帧）。
    先写同目录的 `.tmp` 再 `os.replace`：中途失败不会留下半截 PNG 被下次当成缓存。
    """
    from PIL import Image  # 延迟导入：Windows 不需要转换，没必要付这个启动成本

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + '.tmp')
    try:
        with Image.open(source) as ico:
            plugin: Any = getattr(ico, 'ico', None)
            # 这两个属性不在 Pillow 的公开类型声明里（只在 IcoImageFile 实例上），
            # 因此先按签名 cast，取不到（非 ICO 或有其他加载器）就退回默认帧。
            sizes = cast(Callable[[], set[tuple[int, int]]] | None,
                         getattr(plugin, 'sizes', None))
            get_image = cast(Callable[[tuple[int, int]], Any] | None,
                             getattr(plugin, 'getimage', None))
            frame: Any = ico
            if sizes is not None and get_image is not None:
                largest = max(sizes(), key=lambda size: size[0] * size[1])
                if tuple(largest) != tuple(ico.size):
                    frame = get_image(largest)
            try:
                frame.save(tmp, format='PNG')
            finally:
                if frame is not ico:
                    frame.close()
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _linux_window_icon(source: Path, target: Path) -> str | None:
    """给 GTK 用的窗口图标：把 ICO 的最大帧转成 PNG 并缓存；失败返回 None。

    为什么不能直接把 ICO 交给 GTK：仓库里的 `.ico` 是 PNG 压缩的多尺寸图标，
    Windows 的 `System.Drawing.Icon` 能读，而 Debian 12 的 gdk-pixbuf ICO loader
    不支持压缩帧 —— `set_icon_from_file` 会在 GUI 线程里抛 GError，pywebview 吞掉
    异常后窗口不显示，进程一直挂到 CI 的超时。PNG 则一定读得出来。

    为什么失败只给 None 不抛：图标是装饰，窗口必须能开。`create_window(icon=None)`
    会跳过设置图标，其余行为不变。
    """
    try:
        if (target.is_file() and target.stat().st_size > 0
                and target.stat().st_mtime >= source.stat().st_mtime):
            return str(target)
    except OSError:
        pass      # 缓存状态读不出来（或源文件不存在）就重新生成，生成失败再兜底
    try:
        _write_largest_ico_frame_as_png(source, target)
    except Exception as error:
        log.warning(f'[Cubby] 窗口图标转换失败，本次不设窗口图标: {error}')
        return None
    log.info(f'[Cubby] Linux 窗口图标: {target}')
    return str(target)


def _window_icon_path(platform_name: str | None = None) -> str | None:
    """窗口图标路径：Windows / macOS 沿用 ICO，Linux（GTK）给转好的 PNG。

    为什么必须显式传图标：不传时 winforms 后端用 `ExtractIconW` 从 `sys.executable`
    取图标 —— 打包后那是 Cubby.exe（构建期已带图标），开发态却是 python.exe，窗口
    会顶着 Python 的图标。Windows 上仍必须是 ICO：winforms 用
    `System.Drawing.Icon(path)` 读取，该类型只认 ICO。

    `--web-only` 不建窗，这条路径不会被调用。平台名可注入，因此 Windows 上也能
    单测 Linux 分支（转换结果、失败兜底都不依赖显示器）。
    """
    source = _brand_icon_source_path()
    name = platform.system() if platform_name is None else platform_name
    if name in ('Linux', 'OpenBSD'):
        return _linux_window_icon(source, Path(get_config_dir()) / _LINUX_ICON_CACHE_NAME)
    return str(source)


def _webview_storage_dir() -> str:
    """WebView2 的持久化 UserDataFolder；不可用时返回空串（退回 pywebview 默认）。

    背景：`pywebview.start()` 的 `private_mode` 默认为 True，此时 winforms 后端把
    cache_dir 指向一个 `tempfile.TemporaryDirectory().name`，**退出即删** —— 浏览器
    profile 与 HTTP 缓存都不落盘，图片每次启动都要重新取。传
    `private_mode=False` + `storage_path` 才会得到磁盘缓存。

    为什么要"试写"再决定：打包产物可能被放在只读位置（例如 Program Files），
    而 WebView2 无法在只读目录里建 profile —— 直接传进去会让窗口起不来。
    这里试不出可写目录时返回空串，交回 pywebview 的默认行为：宁可这一轮没有
    持久缓存，也不能因为缓存目录而启动失败。
    """
    target = get_webview_storage_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / '.write-probe'
        probe.write_text('', encoding='utf-8')
        probe.unlink()
    except OSError as e:
        log.warning(f'[Cubby] WebView2 缓存目录不可用（{target}: {e}），'
                    f'本次启动不启用持久缓存')
        return ''
    log.info(f'[Cubby] WebView2 缓存目录: {target}')
    return str(target)


# ============================================================
# 桌面窗口后端探测与 --web-only 降级
# ============================================================
# 为什么要在建窗前主动探测：pywebview 在 Windows 缺 WebView2 Runtime 时会**静默**
# 退回 mshtml（IE 内核），窗口能开、界面却错乱；在 Linux 缺 GTK/WebKitGTK 时则在
# import 阶段抛异常，用户只看到一段 traceback。探测一次、把"缺什么、怎么装"写进
# 日志、自动退回浏览器访问，比让人对着 traceback 猜依赖可行。

#: WebView2 Runtime 在 EdgeUpdate 客户端里的产品 GUID（Evergreen 发行版）
_WEBVIEW2_CLIENT_GUID = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'

#: WebView2 版本的注册表落点。64 位系统上 32 位产品写在 WOW6432Node 下，64 位安装
#: 也可能直接写在 SOFTWARE 下；两处都要查，HKCU（按用户安装）与 HKLM 都要查。
_WEBVIEW2_REGISTRY_PATHS = (
    rf'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_GUID}',
    rf'SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT_GUID}',
)

#: WebView2 Runtime 的官方下载页（安装提示里给这个，而不是任何镜像）
_WEBVIEW2_DOWNLOAD_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/'

#: 冒烟钩子：正整数时，窗口建好后 N 毫秒自动关窗。CI 用它把"窗口真的能建起来"
#: 变成一个可自动判定的退出码，而不是靠人盯着看。
WINDOW_SMOKE_ENV = 'CUBBY_WINDOW_SMOKE_MS'

#: 后端探测通过、日志里报的后端名。真名以 pywebview 的 renderer 为准，这里只是
#: GUI 循环还没起来时的推断值。
_WINDOW_BACKEND_NAMES = {
    'Windows': 'edgechromium',
    'Linux': 'gtk',
    'OpenBSD': 'gtk',
    'Darwin': 'cocoa',
}


def _linux_window_backend_probe() -> tuple[bool, str]:
    """Linux：gi（PyGObject）在、且 WebKit2 4.1/4.0 的类型库能加载，才算可用。

    pywebview 的 gtk 后端同时需要 Gtk 3.0 与 WebKit2（4.1 优先、4.0 兜底）。这里只
    判最常缺的两样：gi 本身、WebKit2 typelib —— 缺任一个，建窗都会在 import 阶段
    抛异常。先 `find_spec` 再 import，是为了把"根本没装 PyGObject"和"装了但导入失败"
    区分成两条可读原因。
    """
    if importlib.util.find_spec('gi') is None:
        return False, '缺少 PyGObject（gi 模块）'
    try:
        gi = importlib.import_module('gi')
    except ImportError as error:
        return False, f'无法导入 gi: {error}'

    for version in ('4.1', '4.0'):
        try:
            gi.require_version('WebKit2', version)
        except ValueError:
            continue
        break
    else:
        return False, '缺少 WebKit2 4.1/4.0 的 GObject 类型库（webkit2gtk）'

    try:
        from gi.repository import WebKit2  # noqa: F401  # type: ignore  # 只验证类型库能加载
    except (ImportError, ValueError) as error:
        return False, f'无法加载 WebKit2 类型库: {error}'
    return True, ''


def _webview2_registry_version() -> str:
    """从 EdgeUpdate 客户端注册表读 WebView2 Runtime 版本；读不到返回空串。

    `ImportError` 是正常分支：这段代码在非 Windows 上也会被直接调用（用例验证分支），
    而 `winreg` 只有 Windows 才有。
    """
    try:
        winreg = importlib.import_module('winreg')
    except ImportError:
        return ''
    for root_name in ('HKEY_CURRENT_USER', 'HKEY_LOCAL_MACHINE'):
        root = getattr(winreg, root_name)
        for path in _WEBVIEW2_REGISTRY_PATHS:
            try:
                with winreg.OpenKey(root, path) as key:
                    version, _ = winreg.QueryValueEx(key, 'pv')
            except OSError:
                continue
            text = str(version).strip()
            if text:
                return text
    return ''


def _webview2_install_roots() -> list[str]:
    """WebView2 安装根目录候选：32 位程序目录优先，再 64 位（运行时可能在任一）。"""
    roots = []
    for name in ('ProgramFiles(x86)', 'ProgramFiles'):
        value = os.environ.get(name)
        if value:
            roots.append(value)
    return roots


def _webview2_browser_executable() -> Path | None:
    """注册表读不到时的兜底：找 `<root>\\Microsoft\\EdgeWebView\\Application\\*\\msedgewebview2.exe`。"""
    for root in _webview2_install_roots():
        application = Path(root) / 'Microsoft' / 'EdgeWebView' / 'Application'
        try:
            candidates = sorted(application.glob('*/msedgewebview2.exe'))
        except OSError:
            continue
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def _windows_window_backend_probe() -> tuple[bool, str]:
    """Windows：装了 WebView2 Runtime 才算可用。

    不能等建窗失败再报错 —— 没有运行时时 pywebview 会悄悄退回 mshtml，窗口照开，
    界面却是 IE 内核渲染的坏样子。注册表是权威来源，安装目录是"注册表被清理过但
    文件还在"时的兜底。
    """
    if _webview2_registry_version():
        return True, ''
    if _webview2_browser_executable() is not None:
        return True, ''
    return False, ('未检测到 WebView2 Runtime（缺少它时 pywebview 会退回 mshtml/IE，'
                   '界面无法正常显示）')


def _probe_window_backend(platform_name: str | None = None) -> tuple[bool, str]:
    """探测桌面窗口后端是否可用，返回 `(是否可用, 不可用原因)`。

    平台判定与各平台探测实现分开、平台名可传入，因此 Windows 上也能单测 Linux 分支。
    其余平台（含 macOS）按可用处理：那里没有"静默退回旧内核"的问题，真缺依赖时
    pywebview 自己会报错，不需要预判。
    """
    name = platform.system() if platform_name is None else platform_name
    if name in ('Linux', 'OpenBSD'):
        return _linux_window_backend_probe()
    if name == 'Windows':
        return _windows_window_backend_probe()
    return True, ''


def _resolve_window_route(web_only: bool, probe: tuple[bool, str]) -> tuple[bool, str]:
    """纯决策：返回 `(是否走 web-only, 降级原因)`。

    显式 `--web-only` 是用户的选择、不是降级，原因留空；窗口后端可用时同理。
    只有"没要 web-only、后端又不可用"才产生降级原因。
    """
    if web_only:
        return True, ''
    available, reason = probe
    if available:
        return False, ''
    return True, reason


def _resolve_startup_route() -> tuple[bool, str]:
    """按命令行决定本次启动路线；显式 `--web-only` 时不做平台探测。"""
    if '--web-only' in sys.argv:
        return True, ''
    return _resolve_window_route(False, _probe_window_backend())


def _log_window_backend_install_hint(platform_name: str) -> None:
    """把"缺什么、执行哪条命令"直接写进日志：用户看的是日志，不是文档。"""
    if platform_name in ('Linux', 'OpenBSD'):
        log.warning('[Cubby] 安装桌面窗口依赖: Debian/Ubuntu 执行 '
                    '`sudo apt install gir1.2-gtk-3.0 gir1.2-webkit2-4.1`；Fedora 执行 '
                    '`sudo dnf install gtk3 webkit2gtk4.1`；Arch 执行 '
                    '`sudo pacman -S webkit2gtk-4.1`')
        return
    if platform_name == 'Windows':
        log.warning(f'[Cubby] 安装 WebView2 Runtime: {_WEBVIEW2_DOWNLOAD_URL}')


def _window_backend_label(platform_name: str) -> str:
    """日志里的后端名：pywebview 已初始化过就取它的 renderer，否则按平台推断。"""
    renderer = getattr(webview, 'renderer', None)
    if isinstance(renderer, str) and renderer:
        return renderer
    return _WINDOW_BACKEND_NAMES.get(platform_name, platform_name.lower())


def _window_smoke_delay_ms() -> int | None:
    """解析 `CUBBY_WINDOW_SMOKE_MS`：未设置返回 None，非正整数记日志后返回 None。"""
    raw = os.environ.get(WINDOW_SMOKE_ENV, '').strip()
    if not raw:
        return None
    try:
        delay_ms = int(raw)
    except ValueError:
        log.warning(f'[Cubby] {WINDOW_SMOKE_ENV} 不是整数，忽略: {raw!r}')
        return None
    if delay_ms <= 0:
        log.warning(f'[Cubby] {WINDOW_SMOKE_ENV} 必须是正整数，忽略: {raw!r}')
        return None
    return delay_ms


#: 冒烟钩子等窗口显示的秒数：超时说明窗口根本没起来（GTK 建窗异常会被 pywebview
#: 吞掉），此时必须让 CI 快速失败，而不是让整条流水线挂到超时
WINDOW_SHOW_TIMEOUT_SECONDS = 10


def _exit_process(code: int) -> None:
    """立即结束整个进程，不走解释器收尾（`atexit` 与插件卸载都不会执行）。

    为什么必须这么退：冒烟钩子跑在 `webview.start(func=...)` 起的工作线程里，而 GUI
    循环阻塞着主线程。工作线程里 `raise` / `sys.exit()` 只结束这个线程，GUI 循环照旧
    挂着，CI 只能等超时 —— `os._exit` 是唯一能立刻把整个进程（含 GUI 线程）结束掉
    的方式。冒烟是 CI 的一次性进程，图标缓存这类收尾本来也不需要。
    """
    os._exit(code)


def _window_smoke_closer(window: webview.Window | None, delay_ms: int) -> Callable[[], None]:
    """构造 `webview.start(func=...)` 的冒烟回调：窗口显示后延迟 N 毫秒关窗。

    先等 shown 事件再计时：pywebview 的窗口对象要等 GUI 层把它建出来才有效，启动
    回调与建窗之间存在这一段竞态，过早 destroy 是空操作。`window` 允许为 None：
    `webview.create_window` 在窗口初始化失败时会返回 None。

    窗口在 `WINDOW_SHOW_TIMEOUT_SECONDS` 秒内没显示（例如 GTK 建窗抛错被 pywebview
    吞掉）时记错误并以非 0 退出：此时 destroy 是空操作，继续等下去只会把 CI 拖到
    超时，早失败才能留下可读原因。
    """
    def close_after_delay() -> None:
        if window is None:
            log.error('[Cubby] 桌面窗口未能创建（create_window 返回空）')
            _exit_process(1)
            return
        try:
            shown = window.events.shown.wait(WINDOW_SHOW_TIMEOUT_SECONDS)
        except Exception as error:
            log.debug(f'[Cubby] 冒烟钩子等待窗口显示失败: {error}')
            shown = False
        if not shown:
            log.error(f'[Cubby] 桌面窗口未能在 {WINDOW_SHOW_TIMEOUT_SECONDS} 秒内显示')
            _exit_process(1)
            return

        # 先绑成不可选的局部名字：pyright 不会把外层的 None 收窄带进嵌套函数。
        target = window

        def destroy_window() -> None:
            try:
                target.destroy()
            except Exception as error:
                log.debug(f'[Cubby] 冒烟钩子关闭窗口失败: {error}')

        timer = threading.Timer(delay_ms / 1000, destroy_window)
        timer.daemon = True
        timer.start()
        log.info(f'[Cubby] 冒烟钩子: {delay_ms} ms 后关闭桌面窗口')

    return close_after_delay


def _run_web_only(config, manager, host: str, port: int) -> None:
    """只起 HTTP 服务：显式 `--web-only` 与"窗口后端不可用"的降级共用这一份实现。

    降级后必须与显式 web-only 完全一样可用（浏览器打开同一个地址），差异只在日志
    里那条"已退回"的说明。
    """
    app = create_app(config, manager)
    log.info(f"[Cubby] Web-only 模式启动: http://{host}:{port}")
    try:
        # threaded=True：单线程下大文件媒体流会阻塞所有请求（无法跳转/元数据读取/API），
        # 流媒体（Range 请求）与后台扫描任务都需要并发处理。
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
    finally:
        # 浏览器访问模式没有"关窗"事件：调用方停掉这个服务（Ctrl+C）就是安全退出。
        # 放在 `finally` 是因为 `app.run()` 在收到停止信号时是抛异常退出的 ——
        # 这一条与桌面模式的取舍不同，理由见 `_commit_last_route` 的说明。
        _commit_last_route()


def _run_app(config, manager, web_only: bool | None = None, fallback_reason: str = ''):
    """启动应用（Web-only 或桌面窗口），阻塞到用户退出。

    `web_only` / `fallback_reason` 由 `main()` 用 `_resolve_startup_route()` 算好传进来；
    为 None 时按 `--web-only` 自行判定（保持既有调用方式，也让单测可以直接调本函数
    而不触发平台探测）。
    """
    if web_only is None:
        web_only = '--web-only' in sys.argv
    host, port = config['server']['host'], _resolve_port(config)
    # Web-only 模式：不启动 PyWebView 桌面窗口，只运行 Flask 服务。
    # 适用于通过 nginx/SSH 隧道在浏览器中访问 Cubby UI，也用于窗口后端不可用时的降级。
    if web_only:
        if fallback_reason:
            log.warning(f"[Cubby] 桌面窗口不可用: {fallback_reason}；"
                        f"已退回 --web-only: http://{host}:{port}")
            _log_window_backend_install_hint(platform.system())
        _run_web_only(config, manager, host, port)
        return

    class ShellAPI:
        """PyWebView 的 js_api 载体：属性即暴露给前端的 system_* 方法。"""

    shell_info = ShellInfo(config, str(get_config_dir()))
    # 壳偏好：与 file_server 里那个实例指向同一份 `<config>/shell.json`，
    # 共用 SettingsStore 的同一把锁（见 shell/backend/shell_prefs.py）。
    shell_prefs = ShellPrefs(str(get_config_dir()))

    # PyWebView 的 js_api 载体：属性名即暴露给前端的方法名。
    # 这里刻意用 setattr 动态挂载（而不是逐个 `api.xxx = ...`）：方法集合包含
    # 由插件注册的动态名（<插件>__<方法>），静态属性声明无法覆盖；集中成一张
    # 表也让"暴露了什么"一眼可读、可日志化。
    # 标注为 object：方法集合含插件注册的动态名，静态类型无法枚举；
    # pywebview 只按属性名反射取值，object 注解不影响运行期行为。
    api: object = ShellAPI()
    shell_methods: dict = {
        'system_get_plugins': manager.get_frontend_manifests,
        'system_get_plugin_extensions': manager.get_plugin_extensions,
        'system_get_plugin_status': manager.get_plugin_status,
        'system_get_config': lambda: config,
        'system_toggle_fullscreen': _toggle_fullscreen,
        # 显式设置 / 读取全屏态：前端不再用"切换"来实现「启动后进入全屏」
        # （切换是相对量，窗口已经是全屏时会把用户切出去）
        'system_set_window_fullscreen': set_window_fullscreen,
        'system_get_window_fullscreen': window_fullscreen_state,
        # 插件设置弹窗的目录选择器（shell/base.js 的 type:"directory"）用它列盘符/子目录；
        # 桌面模式下 js_api 只拿到这张表，所以必须在这里也注册一次（web 模式在 file_server）
        'system_browse_dir': lambda path='': list_subdirectories(path),
        # 系统文件对话框（背景图选图 / 选目录）：与 HTTP 路径注册同一个实现
        'system_pick_path': pick_path,
        # 集中设置页「数据与缓存 / 诊断 / 关于」段（web 模式在 file_server 同一份实现）
        'system_get_shell_info': shell_info.get_info,
        # 背景图库：导入 / 删除 / 枚举（web 模式在 file_server 是同一份实现；图片本身
        # 走 /file 无 plugin 的壳级路径，不经过 js_api）
        'system_import_background_image': shell_info.import_background_image,
        'system_delete_background_image': shell_info.delete_background_image,
        'system_list_background_images': shell_info.list_background_images,
        'system_set_log_level': shell_info.set_log_level,
        'system_clear_thumb_caches': shell_info.clear_thumb_caches,
        'system_open_log_dir': shell_info.open_log_dir,
        # 壳偏好（启动与窗口 / 外观 / 背景 / 玻璃）：与 file_server 同一份实现
        'system_get_shell_prefs': shell_prefs.read,
        'system_set_shell_prefs': shell_prefs.apply_patch,
    }
    shell_methods.update(manager.freshness_api_methods())
    # 应用内检查更新：与 HTTP 通道同一张方法表（唯一来源 update_check.api_methods()）
    shell_methods.update(update_check.api_methods())
    shell_methods.update(manager.get_api_methods())
    # 桌面模式**不**套 `guard_admin_methods`，理由必须写在代码里，否则下一个人会
    # 把它当成"漏了一处"补上、然后所有管理员端点在桌面模式全部拒绝：
    #   * js_api 由 pywebview 反射调用，不经过 Flask 的 before_request，执行上下文里
    #     没有主体（CURRENT_PRINCIPAL 只在 file_server._require_token 里设置）；
    #   * 而"这次调用是谁"在本机没有第二个答案 —— 窗口页面本身就是 owner 令牌的持有者，
    #     插件 iframe 与它同源，同样能经 Bridge.callSystem 沿 parent 链取到
    #     pywebview.api 说话。**角色判定分不开同源的插件前端与壳**。
    # 能分开的只有 origin 隔离（插件前端独立端口/子域 + postMessage 桥），属于后续方案。
    # HTTP 路径继续走同一份判定（file_server.guard_admin_methods），
    # 那是能携带非 owner 令牌、判定真正生效的通道。
    for method_name, method_fn in shell_methods.items():
        setattr(api, method_name, method_fn)

    _run_desktop_window(config, manager, api, host, port)


def _run_desktop_window(config, manager, api, host, port):
    """起本地 Flask + 桌面窗口，阻塞到用户关窗。

    单独成函数是为了让"关窗退出"这条路径可测：`tests/test_main_startup.py` 把
    `webview` 的建窗与消息循环替身掉，断言「上次离开的页面」的提交发生在
    `webview.start()` **返回之后**，而不是异常路径上。
    """
    app = create_app(config, manager)
    # threaded=True：媒体流/Range 请求与后台扫描任务需要并发，见 --web-only 分支注释
    threading.Thread(target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True), daemon=True).start()
    if not wait_for_server(host, port):
        log.warning("[Cubby] Flask 启动超时")
        return

    log.info(f"[Cubby] 窗口后端: {_window_backend_label(platform.system())}")
    window = webview.create_window('格子铺', f'http://{host}:{port}', js_api=api, width=1400, height=900, text_select=True)
    # 窗口已建：此后 system_*_window_fullscreen 才有对象可操作（--web-only 不会走到这里，
    # 那条通道因此继续给出"没有桌面窗口"的明确拒绝）
    register_desktop_window()
    log.info("[Cubby] 桌面窗口已创建")
    # CI 冒烟钩子：CUBBY_WINDOW_SMOKE_MS 为正整数时，窗口显示后自动关窗。
    # 回调交给 webview.start(func=...) 在 GUI 循环起来之后执行，未设置时为 None，
    # 与原有行为完全一致（pywebview 对 func=None 就是什么都不做）。
    smoke_ms = _window_smoke_delay_ms()
    on_window_started = _window_smoke_closer(window, smoke_ms) if smoke_ms is not None else None
    webview.start(on_window_started, debug=not getattr(sys, 'frozen', False), http_server=True,
                  private_mode=False, storage_path=str(_webview_storage_dir()),
                  icon=_window_icon_path())
    # 这一行必须留在 `webview.start()` **之后**：它返回即"用户正常关窗退出"。
    # 异常展开（启动失败 / 崩溃）会跳过这里，进程被强杀同样不会执行 —— 「上次离开的
    # 页面」只记安全退出的值，正是要靠这个位置来保证。浏览器与
    # `--web-only` 没有关窗事件，由 `app.run()` 返回处与空闲兜底提交覆盖。
    _commit_last_route()


def main():
    # 实例数据根覆盖必须**最先**处理：`setup_logging` 与 `load_config` 都要去读
    # `<config_dir>`，而 `get_user_data_dir()` 是进程级缓存 —— 晚一步设置，这个实例
    # 仍然会往仓库/程序目录的 `data/`、`.config/` 里写（端到端夹具正是靠这一行做到
    # "每个实例一份空白状态"）。
    user_data_dir = _argv_value('--user-data-dir')
    if user_data_dir:
        os.environ[USER_DATA_DIR_ENV] = user_data_dir

    # 日志必须最先初始化：发行版打包 console=False 时没有控制台，
    # 所有诊断信息只能靠文件通道（shell/backend/app_logging.py）。
    # 级别取集中设置页保存过的那一档（未设置/非法时回退 INFO）。
    log_file = setup_logging(get_config_dir(), level=stored_log_level(str(get_config_dir())))
    if log_file:
        log.info(f"[Cubby] 日志文件: {log_file}")

    config = load_config()
    # 数据根覆盖：不传时行为不变；传相对路径仍锚定到本实例的用户数据目录
    # （resolve_data_root 的既有语义），因此夹具可以只给一个空的 CUBBY_HOME。
    data_root_override = _argv_value('--data-root')
    if data_root_override:
        config['directories']['data_root'] = str(resolve_data_root(data_root_override))
    os.makedirs(config['directories']['data_root'], exist_ok=True)

    # 状态调试模式（--status-debug）：壳内 /status 视图显示调试面板
    # （健康检查 / API 鉴权 / 标记页演示），正常使用不受影响。
    config.setdefault('debug', {})
    config['debug']['status_debug'] = '--status-debug' in sys.argv

    # 数据路由（/api /file /thumbs）的访问令牌：首次启动生成并持久化。
    # 浏览器页面会自动种下 Cookie；外部脚本可用 X-Cubby-Token 头携带。
    get_or_create_token(get_config_dir())
    log.info(f"[Cubby] API 访问令牌: {get_token_file(get_config_dir())}"
          f"（/api /file /thumbs 路由需携带，启动时若缺失将自动生成）")

    plugin_search_dirs = get_plugin_search_dirs()
    for plugin_dir in plugin_search_dirs:
        try:
            plugin_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    log.info(f"[Cubby] 用户数据目录: {get_user_data_dir()}")
    log.info(f"[Cubby] 插件搜索目录: {', '.join(str(p) for p in plugin_search_dirs)}")
    manager = PluginManager([str(p) for p in plugin_search_dirs], config=config)
    # atexit 必须在 load_all() 之前注册：插件 on_load() 里抛 SystemExit /
    # KeyboardInterrupt（Ctrl+C 打断启动）会穿透 load_all，此时若还没注册钩子，
    # 已加载插件就永远不会收到 on_unload —— SQLite/WAL 句柄与后台线程全部泄漏。
    atexit.register(_unload_plugins, manager)
    # 「上次离开的页面」的退出兜底（浏览器访问与 --web-only 没有关窗事件）
    atexit.register(_commit_last_route)
    manager.load_all()
    # 启动路线在这里定：显式 --web-only 不动，否则先探测窗口后端，不可用就降级。
    # 探测不放进 `_run_app` 里，是因为那两段启动路径（`_run_app` 的桌面分支、
    # `_run_desktop_window`）已经被既有用例直接调用做替身测试；把探测放在入口，
    # 单测替换掉建窗逻辑时不会被本机的窗口依赖左右。
    web_only, fallback_reason = _resolve_startup_route()
    # 退出时必须回调 on_unload（关 SQLite/WAL、停插件线程、落盘最后一次状态）：
    # atexit 兜底 + finally 覆盖所有退出路径（正常关窗、Ctrl+C、异常）。
    try:
        _run_app(config, manager, web_only=web_only, fallback_reason=fallback_reason)
    finally:
        _unload_plugins(manager)


if __name__ == '__main__':
    main()
