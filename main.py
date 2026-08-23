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

import os, sys, time, yaml, webview, threading, shutil
from pathlib import Path
from shell.backend.file_server import create_app
from shell.backend.plugin_manager import PluginManager
from copy import deepcopy
from shell.backend.paths import (
    get_config_dir,
    get_plugin_search_dirs,
    get_user_data_dir,
    resolve_data_root,
)

DEFAULT_CONFIG = {
    'server': {'host': '127.0.0.1', 'port': 18080},
    'directories': {'data_root': './data'},
}






def load_config():
    user_data_dir = get_user_data_dir()
    cfg_path = get_config_dir() / 'app.yaml'

    # 缺失时：尝试从旧 config.yaml 迁移，否则创建默认配置
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        old_path = user_data_dir / 'config.yaml'
        if old_path.exists():
            shutil.copy(old_path, cfg_path)
            print(f"[OmniBox] 迁移配置: config.yaml → {cfg_path}")
        else:
            with open(cfg_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(DEFAULT_CONFIG, f, allow_unicode=True, sort_keys=False)
            print(f"[OmniBox] 创建默认配置: {cfg_path}")

    with open(cfg_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        print("[OmniBox] 配置文件内容无效，已使用默认配置")
        config = {}

    defaults = deepcopy(DEFAULT_CONFIG)

    # 补齐 server 配置
    server = config.setdefault('server', {})
    if not isinstance(server, dict):
        print("[OmniBox] 配置项 server 格式错误，已重置为默认值")
        server = defaults['server']
        config['server'] = server
    for key, value in defaults['server'].items():
        server.setdefault(key, value)

    # 补齐 directories 配置
    directories = config.setdefault('directories', {})
    if not isinstance(directories, dict):
        print("[OmniBox] 配置项 directories 格式错误，已重置为默认值")
        directories = defaults['directories']
        config['directories'] = directories
    if not isinstance(directories.get('data_root'), str) or not directories['data_root'].strip():
        directories['data_root'] = defaults['directories']['data_root']

    # 相对路径 data_root 统一锚定到用户数据目录
    directories['data_root'] = str(resolve_data_root(directories['data_root']))
    return config

def wait_for_server(host, port, timeout=5):
    import requests
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"http://{host}:{port}/health", timeout=0.5)
            if r.status_code == 200: return True
        except: pass
        time.sleep(0.1)
    return False

def main():
    config = load_config()
    os.makedirs(config['directories']['data_root'], exist_ok=True)

    plugin_search_dirs = get_plugin_search_dirs()
    for plugin_dir in plugin_search_dirs:
        try:
            plugin_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    print(f"[OmniBox] 用户数据目录: {get_user_data_dir()}")
    print(f"[OmniBox] 插件搜索目录: {', '.join(str(p) for p in plugin_search_dirs)}")
    manager = PluginManager([str(p) for p in plugin_search_dirs], config=config)
    manager.load_all()

    # Web-only 模式：不启动 PyWebView 桌面窗口，只运行 Flask 服务。
    # 适用于通过 nginx/SSH 隧道在浏览器中访问 OmniBox UI。
    if '--web-only' in sys.argv:
        app = create_app(config, manager)
        host, port = config['server']['host'], config['server']['port']
        print(f"[OmniBox] Web-only 模式启动: http://{host}:{port}")
        app.run(host=host, port=port, debug=False, use_reloader=False)
        return

    class ShellAPI: pass
    api = ShellAPI()
    setattr(api, 'system_get_plugins', manager.get_frontend_manifests)
    setattr(api, 'system_get_plugin_extensions', manager.get_plugin_extensions)
    setattr(api, 'system_get_config', lambda: config)
    setattr(api, 'system_settings_list', manager.get_settings_panels)
    setattr(api, 'system_settings_save', manager.save_settings_panel)
    setattr(api, 'system_toggle_fullscreen', lambda: webview.windows[0].toggle_fullscreen())
    for method_name, method_fn in manager.get_api_methods().items():
        setattr(api, method_name, method_fn)

    app = create_app(config, manager)
    host, port = config['server']['host'], config['server']['port']
    threading.Thread(target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False), daemon=True).start()
    if not wait_for_server(host, port):
        print("[OmniBox] Flask 启动超时"); return

    webview.create_window('OmniBox', f'http://{host}:{port}', js_api=api, width=1400, height=900, text_select=True)
    webview.start(debug=not getattr(sys, 'frozen', False), http_server=True) 

if __name__ == '__main__':
    main()