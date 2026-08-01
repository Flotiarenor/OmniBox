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

from flask import Flask, request, send_from_directory, abort
from pathlib import Path

def create_app(config, plugin_manager):
    app = Flask(__name__)
    frontend_dist = Path(__file__).parent.parent / 'frontend' / 'dist'

    @app.route('/health')
    def health(): return 'OK'

    @app.route('/')
    @app.route('/<path:filename>')
    def serve_shell(filename='index.html'):
        if not (frontend_dist / filename).exists() and not filename.startswith('assets'):
            return send_from_directory(frontend_dist, 'index.html')
        return send_from_directory(frontend_dist, filename)
    @app.route('/files/<path:filepath>')
    def serve_file(filepath):
        # 获取插件名（从查询参数）
        plugin_name = request.args.get('plugin', '')
        
        # 确定根目录
        if plugin_name and plugin_name in plugin_manager._instances:
            data_root = plugin_manager._instances[plugin_name].get_data_root()
        else:
            # 回退到全局根目录
            data_root = Path(config['directories']['data_root']).resolve()
        
        # 安全检查
        try:
            full_path = (data_root / filepath).resolve()
            if not str(full_path).startswith(str(data_root)):
                abort(403)
        except Exception:
            abort(400)
        
        if not full_path.exists():
            abort(404)
        
        return send_from_directory(data_root, filepath)
    @app.route('/thumbs/<path:filepath>')
    def serve_thumb(filepath):
        plugin_name = request.args.get('plugin', '')
        if plugin_name and plugin_name in plugin_manager._instances:
            thumb_dir = plugin_manager._instances[plugin_name].thumb_dir
        else:
            # 回退到全局缩略图目录（通常不存在）
            data_root = Path(config['directories']['data_root']).resolve()
            thumb_dir = data_root / '.cache' / 'thumbs'
        
        # 安全检查
        try:
            full_path = (thumb_dir / filepath).resolve()
            if not str(full_path).startswith(str(thumb_dir)):
                abort(403)
        except Exception:
            abort(400)
        
        if not full_path.exists():
            abort(404)
        
        return send_from_directory(thumb_dir, filepath)
    @app.route('/shell/<path:filename>')
    def serve_shell_assets(filename):
        shell_dir = Path(__file__).parent.parent / 'frontend' / 'dist' / 'shell'
        return send_from_directory(shell_dir, filename)

    @app.route('/plugins/<plugin_name>/frontend/<path:filename>')
    def serve_plugin_frontend(plugin_name, filename):
        plugin_dir = plugin_manager.plugins_dir / plugin_name / 'frontend'
        if not plugin_dir.exists():
            abort(404)
        if filename == 'index.html':
            html_path = plugin_dir / 'index.html'
            if html_path.exists():
                with open(html_path, 'r', encoding='utf-8') as f:
                    html = f.read()
                SCRIPT_TPL = (
                    '<link rel="stylesheet" href="/shell/variables.css">'
                    '<link rel="stylesheet" href="/shell/base.css">'
                    '<script src="/shell/base.js"></script>'
                    '<script>'
                    "Bridge.setPrefix('PLACEHOLDER_NAME');"
                    '(function(){'
                    'var pd = parent.document.documentElement;'
                    "var t = pd.getAttribute('data-theme') || 'light';"
                    "document.documentElement.setAttribute('data-theme', t);"
                    'new MutationObserver(function(){'
                    "var nt = pd.getAttribute('data-theme') || 'light';"
                    "document.documentElement.setAttribute('data-theme', nt);"
                    '}).observe(pd, {attributes:true,attributeFilter:["data-theme"]});'
                    'var cc = pd.getAttribute("data-custom-colors");'
                    'if (cc) { try {'
                    'var map = JSON.parse(cc);'
                    'Object.keys(map).forEach(function(k){'
                    "document.documentElement.style.setProperty(k, map[k]); });"
                    '} catch(e) {} }'
                    'new MutationObserver(function(){'
                    'var ncc = pd.getAttribute("data-custom-colors");'
                    'if (ncc) { try {'
                    'var nmap = JSON.parse(ncc);'
                    'Object.keys(nmap).forEach(function(k){'
                    "document.documentElement.style.setProperty(k, nmap[k]); });"
                    '} catch(e) {} }'
                    '}).observe(pd, {attributes:true,attributeFilter:["data-custom-colors"]});'
                    '})();'
                    '</script>'
                )
                inject = SCRIPT_TPL.replace('PLACEHOLDER_NAME', plugin_name)
                html = html.replace('</head>', inject + '</head>')
                return html
        return send_from_directory(plugin_dir, filename)

    return app
