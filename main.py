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

import os, time, yaml, webview, threading
from shell.backend.file_server import create_app
from shell.backend.plugin_manager import PluginManager

def load_config():
    with open('config.yaml', 'r', encoding='utf-8') as f:
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

    manager = PluginManager(plugins_dir='plugins', config=config)
    manager.load_all()

    class ShellAPI: pass
    api = ShellAPI()
    setattr(api, 'system_get_plugins', manager.get_frontend_manifests)
    setattr(api, 'system_get_config', lambda: config)
    setattr(api, 'system_settings_list', manager.get_settings_panels)
    setattr(api, 'system_settings_save', manager.save_settings_panel)
    for method_name, method_fn in manager.get_api_methods().items():
        setattr(api, method_name, method_fn)

    app = create_app(config, manager)
    host, port = config['server']['host'], config['server']['port']
    threading.Thread(target=lambda: app.run(host=host, port=port, debug=False, use_reloader=False), daemon=True).start()
    if not wait_for_server(host, port):
        print("[OmniBox] Flask 启动超时"); return

    webview.create_window('OmniBox', f'http://{host}:{port}', js_api=api, width=1400, height=900, text_select=True)
    webview.start(debug=True, http_server=True) 

if __name__ == '__main__':
    main()