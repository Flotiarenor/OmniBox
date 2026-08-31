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

import html
import io
import mimetypes
import sys
from flask import Flask, request, send_from_directory, send_file, abort
from pathlib import Path
from collections.abc import Iterable
from shell.backend.auth import (
    TOKEN_COOKIE,
    TOKEN_HEADER,
    get_or_create_token,
    token_matches,
)
from shell.backend.paths import get_config_dir
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

# 无需令牌即可访问的路由（页面与静态资源本身不含用户数据）。
# 数据路由（/api、/file、/files、/thumbs）默认全部要求令牌，新增路由默认受保护。
_OPEN_ENDPOINTS = {'health', 'serve_shell', 'serve_shell_assets', 'serve_plugin_frontend'}

# ===== 状态标记页 =====
# 401/403/404 等错误在浏览器中返回可读的标记页（/api 前缀返回 JSON，
# 便于前端 fetch 解析）；避免 SPA fallback 把错误路径吞成空白 index.html。
# 页面直接复用 OmniBox 本体样式（/shell/variables.css + base.css），
# 并读取主应用 localStorage 的主题/自定义颜色，视觉与本体完全一致。
_STATUS_PAGE_TPL = '''<!DOCTYPE html>
<html lang="zh" data-theme="light" data-status-page="{code}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{code} {title} - OmniBox</title>
<script>
try {{
  var t = localStorage.getItem('omni-theme');
  if (t === 'dark') {{ document.documentElement.setAttribute('data-theme', 'dark'); }}
  var cc = localStorage.getItem('omni-custom-colors');
  if (cc) {{
    var m = JSON.parse(cc);
    Object.keys(m).forEach(function (k) {{ document.documentElement.style.setProperty(k, m[k]); }});
  }}
}} catch (e) {{}}
try {{
  // 顶层打开（浏览器直接访问）时跳转到壳内 /status 视图统一展示错误；
  // 嵌套在插件 iframe 内时不跳转（由 Vue 壳检测 data-status-page 后接管）。
  // location.replace 整页导航会顺带种下令牌 Cookie，壳随后可正常加载。
  if (window.top === window.self) {{
    var code = document.documentElement.getAttribute('data-status-page') || '';
    location.replace(location.origin + '/status?code=' + encodeURIComponent(code));
  }}
}} catch (e) {{}}
</script>
<link rel="stylesheet" href="/shell/variables.css">
<link rel="stylesheet" href="/shell/base.css">
<style>
  html, body {{ height: 100%; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  }}
  .status-view {{
    flex: 1; display: flex; align-items: center; justify-content: center;
    padding: 24px; background: var(--bg-app);
  }}
  .status-card {{
    background: var(--bg-surface); border: 1px solid var(--border);
    border-radius: var(--radius-lg); box-shadow: var(--shadow-md);
    padding: 40px 52px; text-align: center; max-width: 480px;
  }}
  .status-code {{ font-size: 64px; font-weight: 700; color: var(--danger); line-height: 1.1; }}
  .status-title {{ font-size: 18px; font-weight: 600; margin: 12px 0 8px; color: var(--text-primary); }}
  .status-detail {{ font-size: 13px; color: var(--text-secondary); line-height: 1.7; margin-bottom: 24px; }}
  a.home {{ text-decoration: none; }}
</style>
</head>
<body class="view-body">
  <div class="view-toolbar">
    <span style="font-weight:600;font-size:14px;">OmniBox</span>
    <span style="color:var(--text-secondary);font-size:13px;">&nbsp;·&nbsp;{code} {title}</span>
  </div>
  <div class="status-view">
    <div class="status-card">
      <div class="status-code">{code}</div>
      <div class="status-title">{title}</div>
      <div class="status-detail">{detail}</div>
      <a class="btn btn-primary home" href="/">返回首页</a>
    </div>
  </div>
</body>
</html>'''


def _status_page(code: int, title: str, detail: str) -> str:
    """渲染状态标记页 HTML（所有文案先转义，杜绝注入）。"""
    return _STATUS_PAGE_TPL.format(
        code=code,
        title=html.escape(title),
        detail=html.escape(detail),
    )


def _status_response(code: int, title: str, detail: str):
    """统一错误响应：/api 命名空间返回 JSON，浏览器路径返回 HTML 标记页。"""
    if request.path == '/api' or request.path.startswith('/api/'):
        return {'error': title, 'detail': detail}, code
    return _status_page(code, title, detail), code


def create_app(config: dict, plugin_manager: PluginManager) -> Flask:
    app = Flask(__name__)
    frontend_dist = _SHELL_DIR / 'frontend' / 'dist'
    _token = get_or_create_token(get_config_dir())

    @app.before_request
    def _require_token():
        """数据路由鉴权：Cookie 或 X-Omnibox-Token 头二选一。"""
        if request.endpoint in _OPEN_ENDPOINTS:
            return None
        supplied = request.cookies.get(TOKEN_COOKIE, '') or request.headers.get(TOKEN_HEADER, '')
        if token_matches(supplied, _token):
            return None
        abort(401)

    @app.after_request
    def _attach_token_cookie(resp):
        """页面响应时种下 HttpOnly 令牌 Cookie，同源请求（含 <img>）自动携带。"""
        if request.endpoint == 'serve_shell':
            resp.set_cookie(TOKEN_COOKIE, _token, httponly=True, samesite='Lax')
        return resp

    @app.errorhandler(401)
    def _err_401(e):
        return _status_response(401, '未授权',
                                '访问该资源需要访问令牌（页面 Cookie 或 '
                                'X-Omnibox-Token 请求头）。请通过首页进入应用。')

    @app.errorhandler(403)
    def _err_403(e):
        return _status_response(403, '禁止访问',
                                '请求的路径超出了允许访问的目录范围。')

    @app.errorhandler(404)
    def _err_404(e):
        return _status_response(404, '页面不存在',
                                '请求的资源不存在或已被移动。')

    @app.route('/health')
    def health():
        """健康检查：200 + JSON（wait_for_server 与 nginx 探活均兼容）。"""
        return {'status': 'ok'}

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
        # /api 命名空间不属于 SPA 前端路由：GET 到不存在的 API 路径应返回
        # 明确的 404，而不是被 fallback 吞成空白 index.html。
        if filename.startswith('api/'):
            abort(404)
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
