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
import logging
import os
import shutil
import sys
import threading
import time
from copy import deepcopy

import requests
import webview
import yaml

from shell.backend.app_logging import setup_logging
from shell.backend.auth import get_or_create_token, get_token_file
from shell.backend.file_server import create_app
from shell.backend.media_catalog import list_subdirectories
from shell.backend.paths import (
    USER_DATA_DIR_ENV,
    get_config_dir,
    get_plugin_search_dirs,
    get_user_data_dir,
    resolve_data_root,
)
from shell.backend.plugin_manager import PluginManager

log = logging.getLogger(__name__)

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


def load_config():
    user_data_dir = get_user_data_dir()
    cfg_path = get_config_dir() / 'app.yaml'

    # 缺失时：尝试从旧 config.yaml 迁移，否则创建默认配置
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        old_path = user_data_dir / 'config.yaml'
        if old_path.exists():
            shutil.copy(old_path, cfg_path)
            log.info(f"[OmniBox] 迁移配置: config.yaml → {cfg_path}")
        else:
            with open(cfg_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(DEFAULT_CONFIG, f, allow_unicode=True, sort_keys=False)
            log.info(f"[OmniBox] 创建默认配置: {cfg_path}")

    with open(cfg_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        log.info("[OmniBox] 配置文件内容无效，已使用默认配置")
        config = {}

    defaults = deepcopy(DEFAULT_CONFIG)

    # 补齐 server 配置
    server = config.setdefault('server', {})
    if not isinstance(server, dict):
        log.error("[OmniBox] 配置项 server 格式错误，已重置为默认值")
        server = defaults['server']
        config['server'] = server
    for key, value in defaults['server'].items():
        server.setdefault(key, value)

    # 补齐 directories 配置
    directories = config.setdefault('directories', {})
    if not isinstance(directories, dict):
        log.error("[OmniBox] 配置项 directories 格式错误，已重置为默认值")
        directories = defaults['directories']
        config['directories'] = directories
    if not isinstance(directories.get('data_root'), str) or not directories['data_root'].strip():
        directories['data_root'] = defaults['directories']['data_root']

    # 相对路径 data_root 统一锚定到用户数据目录
    directories['data_root'] = str(resolve_data_root(directories['data_root']))
    return config

def wait_for_server(host, port, timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"http://{host}:{port}/health", timeout=0.5)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            # 服务还没起来：连接被拒/超时都属正常，继续轮询等它起来。
            # 不能用裸 except —— 那会把 Ctrl+C(KeyboardInterrupt) 一起吞掉，
            # 用户在启动阶段按 Ctrl+C 将无法中断。
            pass
        time.sleep(0.1)
    return False

def _unload_plugins(manager):
    """进程退出前卸载插件：回调每个插件的 on_unload。

    这是插件唯一的收尾钩子（关 SQLite/WAL、停后台线程、落盘最后一次状态），
    以前全仓没有任何调用点。幂等，可安全重复调用。
    """
    try:
        manager.unload_all()
    except Exception as e:
        log.error(f"[OmniBox] 卸载插件时出错: {e}")


def _toggle_fullscreen():
    """切换桌面窗口全屏。窗口可能在调用时已关闭，取不到就静默忽略。"""
    windows = getattr(webview, 'windows', None) or []
    if windows:
        windows[0].toggle_fullscreen()


class ShellAPI:
    """PyWebView 的 js_api 载体占位类：方法在 _run_app 里动态挂载。"""


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
        log.warning(f"[OmniBox] --port 不是整数，按配置端口 {port} 启动: {override!r}")
        return port


def _run_app(config, manager):
    """启动应用（Web-only 或桌面窗口），阻塞到用户退出。"""
    # Web-only 模式：不启动 PyWebView 桌面窗口，只运行 Flask 服务。
    # 适用于通过 nginx/SSH 隧道在浏览器中访问 OmniBox UI。
    if '--web-only' in sys.argv:
        app = create_app(config, manager)
        host = config['server']['host']
        port = _resolve_port(config)
        log.info(f"[OmniBox] Web-only 模式启动: http://{host}:{port}")
        # threaded=True：单线程下大文件媒体流会阻塞所有请求（无法跳转/元数据读取/API），
        # 流媒体（Range 请求）与后台扫描任务都需要并发处理。
        app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
        return

    class ShellAPI:
        """PyWebView 的 js_api 载体：属性即暴露给前端的 system_* 方法。"""

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
        'system_settings_list': manager.get_settings_panels,
        'system_settings_save': manager.save_settings_panel,
        'system_toggle_fullscreen': _toggle_fullscreen,
        # 插件设置弹窗的目录选择器（shell/base.js 的 type:"directory"）用它列盘符/子目录；
        # 桌面模式下 js_api 只拿到这张表，所以必须在这里也注册一次（web 模式在 file_server）
        'system_browse_dir': lambda path='': list_subdirectories(path),
    }
    shell_methods.update(manager.get_api_methods())
    for method_name, method_fn in shell_methods.items():
        setattr(api, method_name, method_fn)

    app = create_app(config, manager)
    host, port = config['server']['host'], _resolve_port(config)
    # threaded=True：媒体流/Range 请求与后台扫描任务需要并发，见 --web-only 分支注释
    threading.Thread(target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True), daemon=True).start()
    if not wait_for_server(host, port):
        log.warning("[OmniBox] Flask 启动超时")
        return

    webview.create_window('OmniBox', f'http://{host}:{port}', js_api=api, width=1400, height=900, text_select=True)
    webview.start(debug=not getattr(sys, 'frozen', False), http_server=True)


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
    log_file = setup_logging(get_config_dir())
    if log_file:
        log.info(f"[OmniBox] 日志文件: {log_file}")

    config = load_config()
    # 数据根覆盖：不传时行为不变；传相对路径仍锚定到本实例的用户数据目录
    # （resolve_data_root 的既有语义），因此夹具可以只给一个空的 OMNIBOX_HOME。
    data_root_override = _argv_value('--data-root')
    if data_root_override:
        config['directories']['data_root'] = str(resolve_data_root(data_root_override))
    os.makedirs(config['directories']['data_root'], exist_ok=True)

    # 状态调试模式（--status-debug）：壳内 /status 视图显示调试面板
    # （健康检查 / API 鉴权 / 标记页演示），正常使用不受影响。
    config.setdefault('debug', {})
    config['debug']['status_debug'] = '--status-debug' in sys.argv

    # 数据路由（/api /file /thumbs）的访问令牌：首次启动生成并持久化。
    # 浏览器页面会自动种下 Cookie；外部脚本可用 X-Omnibox-Token 头携带。
    get_or_create_token(get_config_dir())
    log.info(f"[OmniBox] API 访问令牌: {get_token_file(get_config_dir())}"
          f"（/api /file /thumbs 路由需携带，启动时若缺失将自动生成）")

    plugin_search_dirs = get_plugin_search_dirs()
    for plugin_dir in plugin_search_dirs:
        try:
            plugin_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    log.info(f"[OmniBox] 用户数据目录: {get_user_data_dir()}")
    log.info(f"[OmniBox] 插件搜索目录: {', '.join(str(p) for p in plugin_search_dirs)}")
    manager = PluginManager([str(p) for p in plugin_search_dirs], config=config)
    # atexit 必须在 load_all() 之前注册：插件 on_load() 里抛 SystemExit /
    # KeyboardInterrupt（Ctrl+C 打断启动）会穿透 load_all，此时若还没注册钩子，
    # 已加载插件就永远不会收到 on_unload —— SQLite/WAL 句柄与后台线程全部泄漏。
    atexit.register(_unload_plugins, manager)
    manager.load_all()
    # 退出时必须回调 on_unload（关 SQLite/WAL、停插件线程、落盘最后一次状态）：
    # atexit 兜底 + finally 覆盖所有退出路径（正常关窗、Ctrl+C、异常）。
    try:
        _run_app(config, manager)
    finally:
        _unload_plugins(manager)


if __name__ == '__main__':
    main()