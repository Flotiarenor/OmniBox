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
import logging
import mimetypes
import socket
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import List

from flask import Flask, abort, request, send_file, send_from_directory

from shell.backend.auth import (
    TOKEN_COOKIE,
    TOKEN_HEADER,
    get_or_create_token,
    get_token_file,
    token_matches,
)
from shell.backend.media_catalog import list_subdirectories
from shell.backend.paths import get_config_dir
from shell.backend.plugin_manager import PluginManager

log = logging.getLogger(__name__)

# Windows 上 Python 的 mimetypes 可能从注册表把 .js 识别成 text/plain，
# 导致 ES module 被浏览器拒绝加载。这里强制修正常见前端资源类型。
mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')
mimetypes.add_type('application/json', '.json')
mimetypes.add_type('image/svg+xml', '.svg')
mimetypes.add_type('application/wasm', '.wasm')
mimetypes.add_type('font/woff2', '.woff2')
mimetypes.add_type('font/woff', '.woff')
mimetypes.add_type('image/webp', '.webp')

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


# ===== 安全响应头 =====
#
# 统一在 after_request 施加，而不是逐路由设置：/api、/file、/thumbs、壳静态资源、
# 插件前端都走同一个响应出口，漏掉任何一条路由就是一处缺口。
#
# 这里刻意分两档。强执行档只放"一旦页面被注入就会扩大战果、而正常页面从不使用"
# 的指令，因此**不可能打坏现有页面**：不设 default-src / script-src / style-src，
# 脚本与样式的加载行为与加头之前完全一致（7 个插件大量使用内联 <script> 与
# style="..." 属性，桌面模式还有 pywebview 注入的桥接脚本，收紧脚本来源必须
# 先在真实浏览器里验证过）。
_SECURITY_HEADERS = {
    # 阻止浏览器把响应体"猜"成另一种类型执行（例如把用户文件当 HTML/脚本执行）
    'X-Content-Type-Options': 'nosniff',
    # 外部站点不得把本应用嵌进 iframe（点击劫持/UI 伪装）；插件 iframe 与壳同源，不受影响
    'X-Frame-Options': 'SAMEORIGIN',
    # 不把本应用的 URL（含 /file?path= 里的本机路径）当 Referer 发给远程站点。
    # 插件前端要加载远程封面/图片，这是唯一会外发的请求头。
    'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': (
        # 关掉 <object>/<embed> 这条历史脚本执行面
        "object-src 'none'; "
        # 阻断 XSS 之后插入 <base href="//evil/"> 把相对资源全部改指向攻击者：
        # 这是"已经失守之后"最省事的提权手法，而全仓没有一处使用 <base>
        "base-uri 'self'; "
        # 与 X-Frame-Options 同义（现代浏览器以本指令为准）
        "frame-ancestors 'self'"
    ),
    # 目标档先只上报、不拦截。等下面这份策略在 Report-Only 下确认无违规上报，
    # 再把需要的来源合并进上面的强执行档。frame-ancestors 在 report-only 里
    # 被规范定义为忽略，所以这里不重复列它。
    'Content-Security-Policy-Report-Only': (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        # img-src/media-src 必须放行远程：插件播放的网易云流与漫画页图都是外链；
        # data: 供抽帧结果的 canvas.toDataURL 使用
        "img-src 'self' data: blob: https: http:; "
        "media-src 'self' blob: https: http:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-src 'self' about: https: http:; "
        "object-src 'none'; base-uri 'self'"
    ),
}


def _resolve_thumb_dir(instance) -> Path | None:
    """解析插件的缩略图目录，形状不对时返回 None（调用方回退到全局根）。

    曾经这里用 getattr 探针取 thumb_dir 并做真值判断：插件把 thumb_dir 写成**方法**时，
    探针拿到的是 bound method（真值），于是跳过回退分支，最后在 Path(bound_method) 上
    抛 TypeError → /thumbs 对所有插件返回 500。契约现在定义在 PluginBase.thumb_dir
    （只读 property），这里把方法形态显式归一化，真正无法解析的形状只记一条 warning
    并回退，不再把整条路由带下水。
    """
    if instance is None:
        return None
    try:
        thumb_dir = instance.thumb_dir
    except Exception as exc:
        log.warning(f'[File_Server-Thumbs] 读取 thumb_dir 失败，回退到全局缩略图目录: {exc}')
        return None
    if isinstance(thumb_dir, (str, Path)):
        return Path(thumb_dir)
    if callable(thumb_dir):
        # 旧插件把 thumb_dir 定义成方法的形态：归一化，而不是让 Path() 抛异常
        try:
            normalized = thumb_dir()
        except Exception as exc:
            log.warning(f'[File_Server-Thumbs] 调用 thumb_dir() 失败，回退到全局缩略图目录: {exc}')
            return None
        if isinstance(normalized, (str, Path)):
            return Path(normalized)
    log.warning(
        f'[File_Server-Thumbs] thumb_dir 必须是路径（PluginBase 只读 property），'
        f'实际为 {type(thumb_dir).__name__}，回退到全局缩略图目录'
    )
    return None


def _local_ipv4_addresses() -> set:
    """枚举本机各网卡的 IPv4 地址（失败时返回空集合）。"""
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        return {ip for ip in ips if ip and not ip.startswith('127.')}
    except OSError:
        return set()


def _build_trusted_hosts(config: dict) -> list:
    """计算允许的 Host 白名单（端口不参与匹配）。

    存在的意义是阻断 DNS rebinding：攻击者把 evil.tld 的 DNS 先指向自己、
    再翻成 127.0.0.1，浏览器便会认为 evil.tld:18080 与 OmniBox 同源，
    自动带上 HttpOnly 令牌 Cookie。此时 Host 头是攻击者域名 → 直接拒绝。

    白名单来源：
      - 环回地址永远放行（本机桌面窗口与浏览器访问都走这里）；
      - config.server.host 是具体地址时放行该地址；
      - host 为通配地址（0.0.0.0 / ::，即用户就是要从局域网访问）时，
        放行本机各网卡 IP —— 仍然只放行 IP，不放行任意域名；
      - config.server.trusted_hosts 可显式补充（如固定内网 IP、反代域名）。
    """
    server = config.get('server') or {}
    if not isinstance(server, dict):
        server = {}
    trusted = {'127.0.0.1', 'localhost'}
    host = str(server.get('host') or '').strip()
    if host and host not in ('0.0.0.0', '::'):
        trusted.add(host)
    else:
        trusted |= _local_ipv4_addresses()
    extra = server.get('trusted_hosts') or []
    if isinstance(extra, (list, tuple)):
        trusted |= {str(item).strip() for item in extra if str(item).strip()}
    return sorted(trusted)

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
  // 已经站在 /status 上时必须就地渲染这张标记页：壳自己的请求也会命中同一套
  // errorhandler（例如 Host 白名单 400），再跳一次就是 /status → 400 → /status
  // 的无限重定向刷新，页面永远打不开。
  if (window.top === window.self && location.pathname !== '/status') {{
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

    def _protected_paths() -> List[Path]:
        """当前必须拒绝返回的路径：壳自己的凭据 + 插件申报的受保护路径。

        每次请求现算而不是建 app 时快照：插件支持运行期卸载与重新加载，快照会让
        新加载插件申报的路径静默失效 —— 静默失效的防护比没有防护更危险。

        比较用解析后的 `Path` 而不是字符串：Windows 上 `PureWindowsPath` 的
        `__eq__`/`__hash__` 都走 normcase，因此 `AUTH_TOKEN.TXT` 与
        `auth_token.txt` 是同一个路径。直接比字符串会被绕过
        （tests/test_file_server_paths.py 的用例锁住这条）。
        """
        paths: List[Path] = []
        try:
            paths.append(get_token_file(get_config_dir()).resolve())
        except OSError:
            pass
        for declared in plugin_manager.get_protected_paths():
            try:
                paths.append(Path(declared).resolve())
            except OSError:
                continue
        return paths

    def _reject_protected_file(target: Path) -> None:
        """受保护路径一律 403 —— 优先于「是否在允许根之内」的判定。

        为什么必须由壳兜住：文件路由的放行依据是插件自己给出的根（`get_file_roots()`
        / `thumb_dir`），而根可以由插件设置改写（审计 §1.1 的改根链路）。开发模式下
        `<data_root>`（默认 ./data）与 `<config_dir>`（./.config）是兄弟目录，于是
        `<data_root>/../.config/auth_token.txt` 完全落在 /file 的允许范围内。
        auth_token.txt 是长期进程外凭据 —— 读走它，等于把一次前端 XSS 或一次局域网
        泄露自举成持久令牌。插件的设置文件同理（pixiv-sync 的 refresh_token 就存在
        `<config>/plugins/pixiv-sync.json`）。

        受保护清单 = 壳自己的凭据（硬编码，不可协商）+ 插件申报的路径
        （`PluginBase.get_protected_paths`，边界由 PluginManager 校验）。Shell
        **不**维护系统路径黑名单：把整个盘符当媒体根（一整块媒体盘）是正当用法。
        """
        for protected in _protected_paths():
            if target == protected or target.is_relative_to(protected):
                log.warning(f'[File_Server] 拒绝返回受保护的凭据文件: {target}')
                abort(403)

    # Host 头白名单：不加这行，攻击者域名（DNS rebinding 指向 127.0.0.1）
    # 就能与 OmniBox 同源、自动带上令牌 Cookie 读走全部数据。
    app.config['TRUSTED_HOSTS'] = _build_trusted_hosts(config)

    @app.before_request
    def _enforce_trusted_host():
        """Host 白名单（防 DNS rebinding），判定必须早于鉴权。

        Flask 的 TRUSTED_HOSTS 在路由解析阶段生效，而 before_request 早于路由
        解析执行，于是会出现"无令牌 401、有令牌才 400"的语义混淆；这里显式
        再判一次，让未列入白名单的 Host 一律 400，开放路由（/health）同样受约束。
        """
        try:
            host = request.host
        except Exception:
            # Werkzeug 的 Request.host 会按 trusted_hosts 校验并抛 SecurityError
            abort(400)
        if host.partition(':')[0] not in app.config['TRUSTED_HOSTS']:
            abort(400)

    @app.before_request
    def _require_token():
        """数据路由鉴权：Cookie 或 X-Omnibox-Token 头二选一。"""
        if request.endpoint in _OPEN_ENDPOINTS:
            return
        supplied = request.cookies.get(TOKEN_COOKIE, '') or request.headers.get(TOKEN_HEADER, '')
        if token_matches(supplied, _token):
            return
        abort(401)

    @app.after_request
    def _attach_token_cookie(resp):
        """页面响应时种下 HttpOnly 令牌 Cookie，同源请求（含 <img>）自动携带。"""
        if request.endpoint == 'serve_shell':
            resp.set_cookie(TOKEN_COOKIE, _token, httponly=True, samesite='Lax')
        return resp

    @app.after_request
    def _attach_security_headers(resp):
        """给**所有**响应统一加安全头（常量定义与取舍理由见 _SECURITY_HEADERS）。

        用 setdefault 而不是直接赋值：将来某个路由需要更严或更松的策略时可以
        自己先设好，这里的兜底值不会把它覆盖掉。
        """
        for name, value in _SECURITY_HEADERS.items():
            resp.headers.setdefault(name, value)
        return resp

    @app.errorhandler(400)
    def _err_400(e):
        # Host 校验失败（werkzeug SecurityError）走这里；不注册的话会掉到
        # Werkzeug 的英文默认页，用户看不懂也拿不到统一外观。
        return _status_response(400, '请求无效',
                                '请求的 Host 不在允许列表中，或请求格式不正确。'
                                '若通过局域网 / 反向代理访问，请在 app.yaml 的 '
                                'server.trusted_hosts 中补充你的访问地址。')

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
            'system_get_plugin_status': plugin_manager.get_plugin_status,
            'system_settings_list': plugin_manager.get_settings_panels,
            'system_settings_save': plugin_manager.save_settings_panel,
            'system_get_config': lambda: config,
            'system_toggle_fullscreen': lambda: None,
            # 集中设置面板的 folder 类型字段：浏览本机绝对路径，走共享基建
            # （与 image-viewer 的 browse_dir 同一套实现；空路径/哨兵 = 盘符层）。
            'system_browse_dir': lambda path='': list_subdirectories(path),
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
            log.info(f"[FileServer] 找不到插件 {plugin_name} 的实例")
            abort(404)
        # 确定允许访问的根目录（支持插件跨多个媒体目录）：get_file_roots() 是
        # PluginBase 的正式成员，默认 [get_data_root()]，不再做 getattr 探针。
        if instance is not None:
            result = instance.get_file_roots()
            roots = list(result) if isinstance(result, Iterable) and not isinstance(result, (str, bytes)) else []
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
                # 凭据文件优先于「是否在根内」判定：插件可以把根设成包含 .config 的目录
                _reject_protected_file(full_path)
                if not any(_is_safe_path(full_path, root) for root in roots):
                    abort(403)
                if not full_path.is_file():
                    abort(404)
                return send_file(full_path, conditional=True)
            # 相对路径：沿用「插件数据根目录」语义
            data_root = roots[0]
            full_path = (data_root / filepath).resolve()
            _reject_protected_file(full_path)
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
            log.info(f"[File_Server-Thumbs] 找不到插件 {plugin_name} 的实例")
            abort(404)

        # 新路径：插件可直接返回 SQLite 缩略图字节，避免散文件随机 I/O。
        # get_thumb_data() 是 PluginBase 的正式成员，默认返回 None（= 本插件不提供字节）。
        # 优先级（docs/core-contract-fixes.md §2.4.c）：本方法命中优先，返回 None
        # 或形状不对时才使用 thumb_dir 散文件布局 —— 否则默认实现会把所有插件的
        # /thumbs 变成 404。
        if instance is not None:
            try:
                result = instance.get_thumb_data(filepath)
                # 插件返回值不可信（插件实现是自由代码）：必须校验形状再解包，
                # 否则返回单值/三元组时会抛 ValueError → 500。
                if isinstance(result, tuple) and len(result) == 2:
                    data, mime = result
                    resp = send_file(io.BytesIO(data), mimetype=mime, conditional=True)
                    resp.headers['Cache-Control'] = 'private, max-age=86400'
                    return resp
            except Exception:
                pass

        return _send_thumb_file(plugin_name, instance, filepath)

    def _send_thumb_file(plugin_name: str, instance, filepath: str):
        """thumb_dir 散文件布局：默认 数据根/.cache/thumbs，找不到时按需生成再读。"""
        global_thumb_dir = Path(config['directories']['data_root']).resolve() / '.cache' / 'thumbs'
        if plugin_name and instance is not None:
            thumb_dir = _resolve_thumb_dir(instance) or global_thumb_dir
        else:
            # 无插件上下文：回退到全局缩略图目录（通常不存在）
            thumb_dir = global_thumb_dir
        thumb_dir = thumb_dir.resolve()

        # 按需生成缩略图（如 image-viewer）：文件不存在时交给插件现场生成
        if instance is not None:
            try:
                instance.ensure_thumb(filepath)
            except Exception:
                pass

        # 安全检查
        try:
            full_path = (thumb_dir / filepath).resolve()
            # thumb_dir 本身可以由插件指定（PluginBase.thumb_dir 可写）：
            # 它若落在 .config 上，"auth_token.txt" 就是一条合法相对路径
            _reject_protected_file(full_path)
            if not _is_safe_path(full_path, thumb_dir):
                abort(403)
        except Exception as e:
            # abort() 抛的 HTTPException 也是 Exception：不还原 e.code 的话，
            # 越界访问会从 403 变成 400（语义错、也不利于排查）。
            # 与下面 serve_media_file 的写法保持一致。
            code = getattr(e, 'code', None)
            if code in (400, 403, 404):
                abort(code)
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
                    '<link rel="stylesheet" href="/shell/folder-picker.css">'
                    '<link rel="stylesheet" href="/shell/effects.css">'
                    '<script src="/shell/base.js"></script>'
                    '<script src="/shell/folder-picker.js"></script>'
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
                return html.replace('</head>', inject + '</head>')
        return send_from_directory(plugin_dir, filename)

    return app
