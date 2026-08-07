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

DEFAULT_CONFIG = {
    'server': {'host': '127.0.0.1', 'port': 18080},
    'directories': {'data_root': './data'},
}

def _app_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()

def load_config():
    app_dir = _app_dir()
    cfg_path = app_dir / '.config' / 'app.yaml'

    # 缺失时：尝试从旧 config.yaml 迁移，否则创建默认配置
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        old_path = app_dir / 'config.yaml'
        if old_path.exists():
            shutil.copy(old_path, cfg_path)
            print(f"[OmniBox] 迁移配置: config.yaml → {cfg_path}")
        else:
            with open(cfg_path, 'w', encoding='utf-8') as f:
                yaml.safe_dump(DEFAULT_CONFIG, f, allow_unicode=True, sort_keys=False)
            print(f"[OmniBox] 创建默认配置: {cfg_path}")

    with open(cfg_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

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

    plugins_dir = Path(sys._MEIPASS) / 'plugins' if getattr(sys, 'frozen', False) else Path('plugins')
    manager = PluginManager(plugins_dir=str(plugins_dir), config=config)
    manager.load_all()

    class ShellAPI: pass
    api = ShellAPI()
    setattr(api, 'system_get_plugins', manager.get_frontend_manifests)
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