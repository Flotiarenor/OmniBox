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

import io
import mimetypes
import sys
from flask import Flask, request, send_from_directory, send_file, abort
from pathlib import Path
from collections.abc import Iterable
from shell.backend.plugin_manager import PluginManager

# Windows 上 Python 的 mimetypes 可能从注册表把 .js 识别成 text/plain，
# 导致 ES module 被浏览器拒绝加载。这里强制修正常见前端资源类型。
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/json', '.json')
mimetypes.add_type('image/svg+xml', '.svg')
mimetypes.add_type('application/wasm', '.wasm')
mimetypes.add_type('font/woff2', '.woff2')
mimetypes.add_type('font/woff', '.woff')

def _get_shell_dir() -> Path:
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return Path(meipass) / 'shell'
    return Path(__file__).resolve().parent.parent

_SHELL_DIR = _get_shell_dir()

def _is_safe_path(full_path: Path, root: Path) -> bool:
    """路径包含检查：必须位于 root 内，避免字符串前缀误判。"""
    try:
        return full_path.is_relative_to(root)
    except ValueError:
        return False

def create_app(config: dict, plugin_manager: PluginManager) -> Flask:
    app = Flask(__name__)
    frontend_dist = _SHELL_DIR / 'frontend' / 'dist'

    @app.route('/health')
    def health(): return 'OK'

    @app.route('/api/<path:method>', methods=['POST'])
    def api_proxy(method):
        """普通浏览器模式：把前端 window.pywebview.api 调用映射为 HTTP POST。"""
        api_methods = dict(plugin_manager.get_api_methods())
        api_methods.update({
            'system_get_plugins': plugin_manager.get_frontend_manifests,
            'system_get_plugin_extensions': plugin_manager.get_plugin_extensions,
            'system_settings_list': plugin_manager.get_settings_panels,
            'system_settings_save': plugin_manager.save_settings_panel,
            'system_get_config': lambda: config,
            'system_toggle_fullscreen': lambda: None,
        })

        fn = api_methods.get(method)
        if fn is None:
            abort(404)

        try:
            payload = request.get_json(silent=True) or {}
            args = payload.get('args', []) if isinstance(payload.get('args'), list) else []
            kwargs = payload.get('kwargs', {}) if isinstance(payload.get('kwargs'), dict) else {}
            result = fn(*args, **kwargs)
            return {'result': result}
        except Exception as e:
            return {'error': str(e)}, 500

    @app.route('/')
    @app.route('/<path:filename>')
    def serve_shell(filename='index.html'):
        if not (frontend_dist / filename).exists() and not filename.startswith('assets'):
            return send_from_directory(frontend_dist, 'index.html')
        return send_from_directory(frontend_dist, filename)
    def serve_media_file(filepath, plugin_name):
        """媒体/文件访问：支持相对路径和绝对路径，并做越权目录校验。"""
        instance = plugin_manager.get_plugin_instance(plugin_name) if plugin_name else None
        if plugin_name and instance is None:
            print(f"[FileServer] 找不到插件 {plugin_name} 的实例")
            abort(404)
        # 确定允许访问的根目录（支持插件跨多个媒体目录）
        if plugin_name and plugin_name in plugin_manager._instances:
            assert instance is not None  # 上面已确认 plugin_name 有对应实例
            getter = getattr(instance, 'get_file_roots', None)
            if callable(getter):
                result = getter()
                if isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
                    roots = list(result)
                else:
                    roots = []
            else:
                roots = []
            if not roots:
                roots = [instance.get_data_root()]
        else:
            # 回退到全局根目录
            roots = [Path(config['directories']['data_root']).resolve()]
        try:
            roots = [Path(root).resolve() for root in roots if root]
        except Exception:
            abort(400)

        # 安全检查
        try:
            decoded_path = Path(filepath)
            if decoded_path.is_absolute():
                # 绝对路径：逐根目录校验（media-player 等跨根插件使用）
                full_path = decoded_path.resolve()
                if not any(_is_safe_path(full_path, root) for root in roots):
                    abort(403)
                if not full_path.is_file():
                    abort(404)
                return send_file(full_path, conditional=True)
            else:
                # 相对路径：沿用「插件数据根目录」语义
                data_root = roots[0]
                full_path = (data_root / filepath).resolve()
                if not _is_safe_path(full_path, data_root):
                    abort(403)
                if not full_path.exists():
                    abort(404)
                return send_from_directory(data_root, filepath)
        except Exception as e:
            code = getattr(e, 'code', None)
            if code in (400, 403, 404):
                abort(code)
            abort(400)

    @app.route('/files/<path:filepath>')
    def serve_file(filepath):
        # 兼容旧路径：/files/<path>
        return serve_media_file(filepath, request.args.get('plugin', ''))

    @app.route('/file')
    def serve_file_query():
        # 新路径：/file?path=<urlencoded>&plugin=<name>
        # 使用 query 参数避免绝对路径中的 / 被 Flask 路由吃掉。
        filepath = request.args.get('path', '')
        plugin_name = request.args.get('plugin', '')
        return serve_media_file(filepath, plugin_name)
    @app.route('/thumbs/<path:filepath>')
    def serve_thumb(filepath):
        plugin_name = request.args.get('plugin', '')
        instance = plugin_manager.get_plugin_instance(plugin_name) if plugin_name else None
        if plugin_name and instance is None:
            print(f"[File_Server-Thumbs] 找不到插件 {plugin_name} 的实例")
            abort(404)

        # 新路径：插件可直接返回 SQLite 缩略图字节，避免散文件随机 I/O。
        get_thumb_data = getattr(instance, 'get_thumb_data', None) if instance is not None else None
        if callable(get_thumb_data):
            try:
                result = get_thumb_data(filepath)
                if result:
                    data, mime = result
                    resp = send_file(io.BytesIO(data), mimetype=mime, conditional=True)
                    resp.headers['Cache-Control'] = 'private, max-age=86400'
                    return resp
            except Exception:
                pass
            abort(404)

        if plugin_name and plugin_name in plugin_manager._instances:
            assert instance is not None  # 上面已确认 plugin_name 有对应实例
            thumb_dir = getattr(instance, 'thumb_dir', None)
            if thumb_dir is None:
                thumb_dir = instance.get_data_root() / '.cache' / 'thumbs'
        else:
            # 回退到全局缩略图目录（通常不存在）
            data_root = Path(config['directories']['data_root']).resolve()
            thumb_dir = data_root / '.cache' / 'thumbs'
        thumb_dir = Path(thumb_dir).resolve()

        # 按需生成缩略图（如 image-viewer）：文件不存在时交给插件现场生成
        ensure = getattr(instance, 'ensure_thumb', None) if instance is not None else None
        if callable(ensure):
            try:
                ensure(filepath)
            except Exception:
                pass

        # 安全检查
        try:
            full_path = (thumb_dir / filepath).resolve()
            if not _is_safe_path(full_path, thumb_dir):
                abort(403)
        except Exception:
            abort(400)
        
        if not full_path.exists():
            abort(404)
        
        return send_from_directory(thumb_dir, filepath)
    @app.route('/shell/<path:filename>')
    def serve_shell_assets(filename):
        dist_shell = _SHELL_DIR / 'frontend' / 'dist' / 'shell'
        if (dist_shell / filename).exists():
            return send_from_directory(dist_shell, filename)
        # 开发期未构建进 dist 的新共享组件直接从 public/shell 提供。
        public_shell = _SHELL_DIR / 'frontend' / 'public' / 'shell'
        return send_from_directory(public_shell, filename)

    @app.route('/plugins/<plugin_name>/frontend/<path:filename>')
    def serve_plugin_frontend(plugin_name, filename):
        plugin_root = plugin_manager.get_plugin_dir(plugin_name)
        if not plugin_root:
            abort(404)
        plugin_dir = plugin_root / 'frontend'
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
                    '<link rel="stylesheet" href="/shell/effects.css">'
                    '<script src="/shell/base.js"></script>'
                    '<script src="/shell/motion.js"></script>'
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
