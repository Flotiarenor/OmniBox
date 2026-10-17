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
import json
import logging
import mimetypes
import os
import re
import socket
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import List

from flask import Flask, abort, g, make_response, request, send_file, send_from_directory

from shell.backend.auth import (
    TOKEN_COOKIE,
    TOKEN_HEADER,
    get_or_create_token,
)
from shell.backend.file_dialog import pick_path
from shell.backend.media_catalog import classify_file, list_subdirectories
from shell.backend.paths import get_config_dir
from shell.backend.plugin_manager import PluginManager
from shell.backend.principal import (
    CURRENT_PRINCIPAL,
    AdminRequired,
    PrincipalStore,
    require_admin,
)
from shell.backend.protected_paths import collect as collect_protected_files
from shell.backend.protected_paths import matches as is_protected_path
from shell.backend.protected_paths import normalize as normalize_path
from shell.backend.shell_info import ShellInfo, background_library_dir
from shell.backend.shell_prefs import ShellPrefs
from shell.backend.window_state import (
    get_state as window_get_fullscreen,
)
from shell.backend.window_state import (
    set_fullscreen as window_set_fullscreen,
)
from shell.backend.window_state import (
    toggle_fullscreen as window_toggle_fullscreen,
)

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

# 注入到插件前端**每个** HTML 页面的引导片段（见 serve_plugin_frontend）：
# 壳的样式与共享组件、Bridge 的插件前缀、以及主题/自定义颜色同步。
# `PLACEHOLDER_NAME` 会被替换成路由里的插件名。
_PLUGIN_BOOTSTRAP_SCRIPT = (
    '<link rel="stylesheet" href="/shell/variables.css">'
    # shared.css 是壳页面与插件页共用的组件层，与 variables.css 一起放最前。
    # 它与 base.css 的先后不再影响结果：base.css 里已没有 .btn / .field / .toast /
    # .obx-nav-item / .obx-icon 这五个族的规则，族内次序只在 shared.css 内部决定。
    '<link rel="stylesheet" href="/shell/shared.css">'
    '<link rel="stylesheet" href="/shell/base.css">'
    '<link rel="stylesheet" href="/shell/folder-picker.css">'
    '<link rel="stylesheet" href="/shell/freshness.css">'
    '<link rel="stylesheet" href="/shell/effects.css">'
    '<link rel="stylesheet" href="/shell/splitter.css">'
    '<script src="/shell/base.js"></script>'
    '<script src="/shell/folder-picker.js"></script>'
    '<script src="/shell/freshness.js"></script>'
    '<script src="/shell/motion.js"></script>'
    '<script src="/shell/splitter.js"></script>'
    # 图标 sprite：必须**内联进文档**，不能靠 <use href="外部.svg#名字"> 引用。
    # 实测（pywebview / WebView2 / Edge 153）：外部文件的 <use> 一律不渲染，包围盒恒为 0，
    # 而同文档的 # 引用正常 —— 而 Chrome 会渲染外部引用，所以这个缺陷在浏览器里测不出来。
    # 本脚本把 sprite 注入文档，插件页随后写 <svg class="obx-icon"><use href="#名字"> 即可。
    '<script src="/shell/icons.generated.js"></script>'
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
    # 玻璃壳开关：与主题同一条通路（观察父文档属性 → 写到自己根元素），
    # 插件侧不写代码；据此让出底色的规则在 base.css。
    'var sg = pd.getAttribute("data-shell-glass") || "off";'
    'document.documentElement.setAttribute("data-shell-glass", sg);'
    'new MutationObserver(function(){'
    'var nsg = pd.getAttribute("data-shell-glass") || "off";'
    'document.documentElement.setAttribute("data-shell-glass", nsg);'
    '}).observe(pd, {attributes:true,attributeFilter:["data-shell-glass"]});'
    # 壳的外观变量：写进本页根元素，并**记住写下过哪些键**。
    # 记住是为了能在下一次广播里清掉不再出现的键 —— 只写不清的话，设置页"恢复默认"
    # 清掉的变量会一直挂在插件页根元素上，压住 variables.css 的默认值。
    'var APPLIED = {};'
    'var applyVars = function (json) {'
    'if (!json) return;'
    'try {'
    'var map = JSON.parse(json);'
    'Object.keys(APPLIED).forEach(function (k) {'
    'if (!(k in map)) { document.documentElement.style.removeProperty(k); delete APPLIED[k]; }'
    '});'
    'Object.keys(map).forEach(function (k) {'
    'document.documentElement.style.setProperty(k, map[k]); APPLIED[k] = 1; });'
    '} catch (e) {}'
    '};'
    'applyVars(pd.getAttribute("data-custom-colors"));'
    'new MutationObserver(function () {'
    'applyVars(pd.getAttribute("data-custom-colors"));'
    '}).observe(pd, {attributes:true,attributeFilter:["data-custom-colors"]});'
    '})();'
    '</script>'
)


def _inject_appearance_vars(html: str, prefs: dict) -> str:
    """把用户的外观变量写进插件页的 `<html style="...">`。

    为什么必须在**服务端**写、而不是等插件页加载后的广播：广播是异步的（脚本注入 →
    MutationObserver 首次回调），第一帧一定先按 `variables.css` 的默认值画一遍，
    之后才跳到用户值。侧栏宽度上还叠了一条 120ms 的过渡，于是每次打开插件都能看见
    "先是默认宽度、再滑到用户设置的值"—— 切换插件时反复出现，看着像闪屏。
    写进 HTML 里就没有这一段：文档首帧拿到的就是最终值。

    只写 `settings_schema` 管不到的那几支（颜色/尺寸变量），值仍然由壳偏好表给出，
    与运行时广播的是同一份数据。
    """
    colors = prefs.get('colors')
    if not isinstance(colors, dict) or not colors:
        return html
    from shell.backend.shell_prefs import is_valid_var_name, is_valid_var_value

    pairs = [
        f'{name}:{value}'
        for name, value in colors.items()
        if is_valid_var_name(name) and is_valid_var_value(value)
    ]
    if not pairs:
        return html
    style = ';'.join(pairs)
    # 只动开标签：`<html lang="zh">` → `<html lang="zh" style="…">`
    marker = re.search(r'<html\b[^>]*>', html, re.IGNORECASE)
    if marker is None:
        return html
    tag = marker.group(0)
    # 插件页自己写了 style 时**并到它前面**：合并成一条属性，避免出现两个 style
    existing = re.search(r'\sstyle="([^"]*)"', tag)
    if existing:
        merged = f'{style};{existing.group(1)}' if existing.group(1).strip() else style
        patched = tag[:existing.start()] + f' style="{merged}"' + tag[existing.end():]
    else:
        patched = tag[:-1] + f' style="{style}">'
    return html[:marker.start()] + patched + html[marker.end():]


def _get_shell_dir() -> Path:
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return Path(meipass) / 'shell'
    return Path(__file__).resolve().parent.parent

_SHELL_DIR = _get_shell_dir()


def _get_res_dir() -> Path:
    """仓库级共享资源目录（`res/`，与 `shell/`、`plugins/` 同级）。

    放"不是代码、但要按 URL 发布给壳页面与插件 iframe"的东西，目前只有图标
    sprite（`res/icons/icons.svg`）。它与 `/shell/*` 的分工：
    `/shell/*` 是**壳注入的 CSS/JS**（由 `_PLUGIN_BOOTSTRAP_SCRIPT` 自动挂进每个
    插件页），`/res/*` 是**按需引用**的资源。两者都不必新增打包配置之外的东西：
    产物由 docs/Releases/spec_common.py 收集。
    """
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            return Path(meipass) / 'res'
    return Path(__file__).resolve().parent.parent.parent / 'res'


_RES_DIR = _get_res_dir()

def _is_safe_path(full_path: Path, root: Path) -> bool:
    """路径包含检查：必须位于 root 内，避免字符串前缀误判。"""
    try:
        return full_path.is_relative_to(root)
    except ValueError:
        return False


def _match_file_root(full_path: Path, roots: List[Path],
                     image_only_roots: Iterable[Path]) -> bool:
    """请求路径是否落在**某个**允许根之内。

    `image_only_roots` 是背景图库（`<config>/backgrounds`，见 shell_info 的
    `background_library_dir`）。导入端点只接受图片，这里再挡一道：少了这条限制，
    库目录里出现一个非图片文件（手工放进去的），就等于把它按原路径开放给**所有**
    持令牌主体（含局域网成员）—— `/file` 本身不校验扩展名。

    命中图片根但类型不是图片时用 `continue` 而不是 `return False`：该文件可能
    同时落在另一个普通根之内（例如 data_root），那种情况下它本来就被放行。
    """
    for root in roots:
        if not _is_safe_path(full_path, root):
            continue
        if root in image_only_roots and classify_file(full_path.name) != 'image':
            log.warning(f'[File_Server] 背景图目录只提供图片，已拒绝 {full_path}')
            continue
        return True
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


def _resolve_file_roots(instance, fallback: Path) -> List[Path]:
    """插件允许访问的根目录；形状不对或抛异常时逐级回退，并记 warning。

    `get_file_roots()` 与 `get_data_root()` 都是插件实现（自由代码）。原先这两个调用
    不在任何 try 里：一个插件抛异常，它的**全部** `/file` 请求都变成 500（Flask 默认
    错误页），而同一类调用在 `/thumbs` 早就有一套兜底（`_resolve_thumb_dir`）。
    回退顺序：插件给出的根 → 插件数据根 → 全局数据根（`config['directories']['data_root']`）。
    """
    roots: List[Path] = []
    if instance is not None:
        try:
            declared = instance.get_file_roots()
        except Exception as exc:
            log.warning(f'[File_Server] 读取插件文件根失败，改用插件数据根: {exc}')
        else:
            if isinstance(declared, Iterable) and not isinstance(declared, (str, bytes)):
                roots = [root for root in declared if root]
            else:
                log.warning('[File_Server] get_file_roots() 必须返回路径列表，已忽略')
        if not roots:
            try:
                data_root = instance.get_data_root()
            except Exception as exc:
                log.warning(f'[File_Server] 读取插件数据根失败，改用全局数据根: {exc}')
            else:
                if data_root:
                    roots = [data_root]
    if not roots:
        roots = [fallback]
    return roots


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
_OPEN_ENDPOINTS = {'health', 'serve_shell', 'serve_shell_assets', 'serve_res_assets',
                   'serve_plugin_frontend'}

# ===== 状态标记页 =====
# 401/403/404 等错误在浏览器中返回可读的标记页（/api 前缀返回 JSON，
# 便于前端 fetch 解析）；避免 SPA fallback 把错误路径吞成空白 index.html。
# 页面直接复用 OmniBox 本体样式（/shell/variables.css + shared.css + base.css），
# 主题与自定义颜色的取值由**服务端**从壳偏好注入 —— 偏好已经不在 localStorage 里。
_STATUS_PAGE_TPL = '''<!DOCTYPE html>
<html lang="zh" data-theme="{theme}" data-status-page="{code}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{code} {title} - OmniBox</title>
{shell_style}
<script>
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
<link rel="stylesheet" href="/shell/shared.css">
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


def _status_theme(prefs: dict) -> str:
    """状态标记页的主题：只认 light / dark，其余（含缺省）都按 dark。"""
    return 'light' if prefs.get('theme') == 'light' else 'dark'


def _status_style_block(prefs: dict) -> str:
    """把壳的自定义外观变量写进状态标记页的内联样式。

    标记页与本体共用 `variables.css`，因此把用户改过的变量写回 `:root` 即可得到
    与本体一致的外观。取值逐条转义并限定字符集：颜色值是用户输入，而这里拼的是
    HTML 而不是 JSON 字符串，宁可少写一条也不能让 `</style>` 进到文档里。
    """
    from shell.backend.shell_prefs import _MAX_VARS as _STATUS_MAX_VARS
    from shell.backend.shell_prefs import is_valid_var_name, is_valid_var_value

    colors = prefs.get('colors')
    if not isinstance(colors, dict):
        return ''
    declarations = []
    for name, value in list(colors.items())[:_STATUS_MAX_VARS]:
        if not is_valid_var_name(name) or not is_valid_var_value(value):
            continue
        declarations.append(f'{name}:{html.escape(value, quote=True)}')
    if not declarations:
        return ''
    return '<style>:root{' + ';'.join(declarations) + '}</style>'


def _status_page(code: int, title: str, detail: str, prefs: dict | None = None) -> str:
    """渲染状态标记页 HTML（所有文案先转义，杜绝注入）。"""
    return _STATUS_PAGE_TPL.format(
        code=code,
        title=html.escape(title),
        detail=html.escape(detail),
        theme=_status_theme(prefs or {}),
        shell_style=_status_style_block(prefs or {}),
    )


def _status_response(code: int, title: str, detail: str, prefs: dict | None = None):
    """统一错误响应：/api 命名空间返回 JSON，浏览器路径返回 HTML 标记页。"""
    if request.path == '/api' or request.path.startswith('/api/'):
        return {'error': title, 'detail': detail}, code
    return _status_page(code, title, detail, prefs), code


# 只有管理员（owner / admin）能调用的 API 方法。
#
# 为什么需要这份清单：`/api/<插件>__<方法>` 原先
# 只校验令牌，任何持令牌者都能拿到壳的完整配置（含绑定地址与数据目录）、
# 翻看各插件加载状态、改别人的日志级别或一次清空全部缩略图缓存。
# 这些不是"某个插件的功能"，而是描述这台机器本身的壳级事实。
# 判据是 `PrincipalContext.is_admin`；老部署的全局令牌会自举成 owner
# （见 principal.py），因此本机使用者不受影响。
#
# 限定范围：**只守壳自己的端点**。插件方法（`<插件>__<方法>`）里确实有该限权的
# （group-mesh 的节点开关、权限档位变更），但把插件名硬编码进壳会把两层耦合起来；
# 插件侧需要限权时应调 `PluginBase.require_principal()` 自己判。
_ADMIN_ONLY_API = frozenset({
    'system_get_config',
    'system_get_plugin_status',
    # 壳自身的运维端点：磁盘路径 / 日志级别 / WebView2 GPU 偏好 / 清缓存 / 开目录，
    # 一律管理员专属
    'system_get_shell_info',
    # 背景图库：导入会把**任意绝对路径**的图片复制进 `<config>/backgrounds`，而该
    # 目录随后由 `/file` 对外发布 —— 不限权等于给每个持令牌者一条"把本机文件搬进
    # 服务范围"的通路；删除同理（在壳自己的配置目录里写与删）。
    'system_import_background_image',
    'system_delete_background_image',
    # 读取侧只返回图片库里的文件名，与 `system_get_shell_info` 同属"壳级事实"的展示面。
    # 图片本身不走这些端点，普通成员页面里的背景图仍由 `/file` 直接取。
    'system_list_background_images',
    # 系统文件对话框会在**服务端桌面**上弹窗并阻塞到用户作答：与 system_open_log_dir
    # 同类（都是"对这台机器的桌面动手"），因此同样只给管理员。任意持令牌者能调它，
    # 就等于能远程往宿主桌面叠模态窗口，并拿到用户选中的绝对路径。
    'system_pick_path',
    'system_set_log_level',
    'system_set_webview_high_performance',
    # 壳偏好（启动页 / 主题 / 背景 / 全屏）：与 system_get_shell_info 同属"描述这台
    # 机器、并决定它下次怎么启动"的壳级事实，写入还会直接写壳自己的配置文件。
    # 读路径是 serve_shell 注入为首屏状态，因此非管理员页面照常拿到生效值。
    'system_get_shell_prefs',
    'system_set_shell_prefs',
    'system_clear_thumb_caches',
    'system_open_log_dir',
    # 统一刷新基建：校验只读盘、可被本插件的工具栏按钮触发，因此不设限；
    # **重建**是"丢弃派生缓存"的破坏性操作（与 system_clear_thumb_caches 同类），
    # 且能指定任意插件 —— 不设限就等于让一个插件 iframe 有权清掉别的插件的缓存。
    'system_freshness_rebuild',
    # 全插件状态快照（含各插件条目数）：与 system_get_plugin_status 同属"壳级事实"
    'system_freshness_overview',
    # 插件诊断钩子：能读任意插件的内部状态（缓存规模、配置路径等），属运维端点
    'system_plugin_diagnose',
})


def guard_admin_methods(api_methods: dict) -> dict:
    """给方法表里属于 `_ADMIN_ONLY_API` 的项套上管理员判定，返回新表。

    包装本身在 `principal.require_admin`（判定只写一处）。本函数被**两条通道**共用：
    HTTP 的 `api_proxy` 与桌面模式的 `main._run_app`（pywebview `js_api`）。原先判定
    只写在 HTTP 路径里，桌面模式不判角色 —— 插件 iframe 经 `Bridge.callSystem` 沿
    parent 链就能调到 `system_get_config`（审计项 P1-8 / P2-3）。
    """
    return {name: (require_admin(fn) if name in _ADMIN_ONLY_API else fn)
            for name, fn in api_methods.items()}


def create_app(config: dict, plugin_manager: PluginManager) -> Flask:
    app = Flask(__name__)
    frontend_dist = _SHELL_DIR / 'frontend' / 'dist'
    _token = get_or_create_token(get_config_dir())
    # 凭据 → 主体的映射。自举：凭据表为空时
    # 把**既有的**全局令牌登记为本机 owner，因此老部署升级后令牌继续可用，
    # 而"这次调用是谁"从此有答案。
    _principals = PrincipalStore(get_config_dir(), bootstrap_token=_token)
    _principals.ensure_bootstrap()
    # 壳自身信息 / 运维端点（集中设置页的「数据与缓存 / 诊断 / 关于」段）
    shell_info = ShellInfo(config, str(get_config_dir()))
    # 壳偏好（启动与窗口 / 外观 / 背景 / 玻璃）：读路径由 serve_shell 注入，
    # 写路径走 system_set_shell_prefs（见 shell/backend/shell_prefs.py）。
    # 桌面模式在 main.py 建**同一个配置目录**的实例，两边共用同一份落盘与同一把锁。
    shell_prefs = ShellPrefs(str(get_config_dir()))

    def _protected_paths() -> List[Path]:
        r"""当前必须拒绝返回的路径：壳自己的凭据 + 插件申报的受保护路径。

        判定实现与插件侧的**写/删自查**共用 `shell/backend/protected_paths.py`：
        两处各拼一份规则，就会出现"读挡得住、删挡不住"。那里同时负责路径归一
        （剥 `\\?\` 扩展前缀 + `samefile`），因为 `resolve()` 在 Windows 上会
        保留扩展前缀，纯词法比较会被一次前缀变换绕过。

        每次请求现算而不是建 app 时快照：插件支持运行期卸载与重新加载，快照会让
        新加载插件申报的路径静默失效 —— 静默失效的防护比没有防护更危险。
        """
        return collect_protected_files(plugin_manager, get_config_dir())

    def _reject_protected_file(target: Path) -> None:
        r"""受保护路径一律 403 —— 优先于「是否在允许根之内」的判定。

        为什么必须由壳兜住：文件路由的放行依据是插件自己给出的根（`get_file_roots()`
        / `thumb_dir`），而根可以由插件设置改写。开发模式下
        `<data_root>`（默认 ./data）与 `<config_dir>`（./.config）是兄弟目录，于是
        `<data_root>/../.config/auth_token.txt` 完全落在 /file 的允许范围内。
        auth_token.txt 是长期进程外凭据 —— 读走它，等于把一次前端 XSS 或一次局域网
        泄露自举成持久令牌。插件的设置文件同理（pixiv-sync 的 refresh_token 就存在
        `<config>/plugins/pixiv-sync.json`）。

        受保护清单 = 壳自己的凭据（硬编码，不可协商）+ 插件申报的路径
        （`PluginBase.get_protected_paths`，边界由 PluginManager 校验）。Shell
        **不**维护系统路径黑名单：把整个盘符当媒体根（一整块媒体盘）是正当用法。

        路径归一与 `samefile` 兜底都在 protected_paths 里：`resolve()` 在 Windows 上
        会保留 `\\?\` 扩展前缀，只做 `==` / `is_relative_to` 会被
        `\\?\D:\…\.config\auth_token.txt` 这种形态整个绕过
        （tests/test_protected_paths.py 锁住这条）。
        """
        if is_protected_path(target, _protected_paths()):
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
        """数据路由鉴权 + **确立主体上下文**。

        两件事必须在这里一起做，顺序不能反：

        1. 校验凭据（Cookie 或 `X-Omnibox-Token` 头二选一）；
        2. 凭据通过后，把它解析成主体并写入 `CURRENT_PRINCIPAL`。

        第 2 步是"受信注入"：主体只从**凭据**得到，
        绝不从请求参数读 —— 一个带 `{"principal": "<别人的 id>"}` 的请求与不带
        该字段的请求必须得到同一个主体。因此这里既不读 `request.args`，
        也不读 JSON body。

        令牌一律走 `PrincipalStore.resolve()`：它比的是**摘要**，未登记即 401，
        **不降级为匿名主体**（"没有主体"是后台线程的状态，不是鉴权失败的出路）。
        """
        if request.endpoint in _OPEN_ENDPOINTS:
            return
        supplied = request.cookies.get(TOKEN_COOKIE, '') or request.headers.get(TOKEN_HEADER, '')
        principal = _principals.resolve(supplied)
        if principal is None:
            abort(401)
        # 记下 token，请求收尾时**恢复**原值（见 _release_principal）：只 set 不
        # reset 会让主体泄漏到同一执行上下文里的后续调用 —— 生产环境每个请求
        # 一个上下文时看不出来，但 `test_client`（以及将来任何复用上下文的调用）
        # 会看到"上一个请求的主体还在"，那是比没有主体更危险的形态。
        #
        # token 存在 `g`（请求域）而不是模块级变量：模块级列表会在并发请求之间
        # 互相 pop 对方的 token，把主体错位到另一个请求上。
        g.principal_token = CURRENT_PRINCIPAL.set(principal)

    @app.teardown_request
    def _release_principal(exception=None):
        """请求结束时恢复主体上下文（鉴权失败时没有 token，什么也不做）。"""
        token = g.pop('principal_token', None)
        if token is None:
            return
        try:
            CURRENT_PRINCIPAL.reset(token)
        except (LookupError, ValueError):
            # reset 只能在**同一个** context 里做；跨 context 时无法恢复，
            # 但此时那个 context 已经结束，不会影响后续请求。
            pass

    # ===== 按需请求 trace（原图 / 缩略图的缓存行为排查）=====
    #
    # `<img src="/file?…">` 不经过任何插件代码，客户端到底有没有命中缓存、有没有回源，
    # 服务端这份记录是唯一能同时给出「这次带没带 If-None-Match」与「这次回源花了多久」
    # 的地方。窗口模式没有 DevTools，尤其只能靠它：
    # 原始现象是「同一张图有时瞬间打开、有时从上到下一行行刷」，而刷的那次日志里
    # 必然有一条 304 —— 说明客户端有副本、只是每次都被要求回源验证。
    #
    # 开关：DSH_FILE_TRACE=1（默认关闭，零开销）。只记 /file 与 /thumbs，并用
    # DSH_FILE_TRACE_MIN_KB（默认 64）滤掉占绝大多数的缩略图请求。
    _TRACE = os.environ.get('DSH_FILE_TRACE') == '1'
    try:
        _TRACE_MIN_BYTES = int(os.environ.get('DSH_FILE_TRACE_MIN_KB', '64')) * 1024
    except ValueError:
        _TRACE_MIN_BYTES = 64 * 1024

    @app.before_request
    def _trace_start():
        if not _TRACE:
            return
        path = request.path or ''
        if not path.startswith(('/file', '/thumbs')):
            return
        g._trace = {
            'started': time.perf_counter(),
            'path': path,
            'range': request.headers.get('Range') or '',
            'inm': request.headers.get('If-None-Match') or '',
            'ims': request.headers.get('If-Modified-Since') or '',
            'ua': 'WebView2' if 'Edg/' in (request.headers.get('User-Agent') or '')
                  else 'browser',
        }

    @app.after_request
    def _trace_log(resp):
        info = getattr(g, '_trace', None)
        if not info:
            return resp
        try:
            size = int(resp.headers.get('Content-Length') or 0)
        except ValueError:
            size = 0
        if size < _TRACE_MIN_BYTES and resp.status_code != 304:
            return resp
        elapsed = (time.perf_counter() - info['started']) * 1000
        # `条件请求` 那一列是判据：带了 If-None-Match = 客户端**存着**这份文件、
        # 只是回来问一句（304 就够）；没带 = 客户端没有副本，这次必须重传整个响应体。
        from urllib.parse import unquote
        short = request.query_string.decode('utf-8', 'replace')
        if 'path=' in short:
            short = unquote(short.split('path=', 1)[1].split('&', 1)[0])
            short = short.rsplit('/', 1)[-1]
        log.info(
            f"[file-trace] {resp.status_code} {size / 1024:9.1f}KB {elapsed:8.1f}ms "
            f"条件请求={'有' if (info['inm'] or info['ims']) else '无'} "
            f"cache-control={resp.headers.get('Cache-Control') or '-'} "
            f"range={info['range'] or '-'} ua={info['ua']} {short}"
        )
        return resp

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

    def _page_prefs() -> dict:
        """状态标记页要用的外观取值（主题 + 自定义变量）。读失败一律回默认外观。"""
        try:
            return shell_prefs.read()
        except Exception as e:
            log.warning(f'[File_Server] 读取壳偏好失败，标记页按默认外观渲染: {e}')
            return {}

    @app.errorhandler(400)
    def _err_400(e):
        # Host 校验失败（werkzeug SecurityError）走这里；不注册的话会掉到
        # Werkzeug 的英文默认页，用户看不懂也拿不到统一外观。
        return _status_response(400, '请求无效',
                                '请求的 Host 不在允许列表中，或请求格式不正确。'
                                '若通过局域网 / 反向代理访问，请在 app.yaml 的 '
                                'server.trusted_hosts 中补充你的访问地址。',
                                _page_prefs())

    @app.errorhandler(401)
    def _err_401(e):
        return _status_response(401, '未授权',
                                '访问该资源需要访问令牌（页面 Cookie 或 '
                                'X-Omnibox-Token 请求头）。请通过首页进入应用。',
                                _page_prefs())

    @app.errorhandler(403)
    def _err_403(e):
        return _status_response(403, '禁止访问',
                                '请求的路径超出了允许访问的目录范围，'
                                '或当前使用者没有执行该操作的权限。',
                                _page_prefs())

    @app.errorhandler(404)
    def _err_404(e):
        return _status_response(404, '页面不存在',
                                '请求的资源不存在或已被移动。',
                                _page_prefs())

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
            'system_get_config': lambda: config,
            # 桌面窗口的窗口态：与桌面 js_api 注册的是**同一份实现**
            # （shell/backend/window_state.py）。原先这里是一个恒返回失败的桩，但桥在
            # pywebview 注入之前会先装上 HTTP 替身，桌面窗口里的调用长期走这条 HTTP
            # 路径，只拿到"没有桌面窗口"的错误 —— 表现是「启动后进入全屏」与
            # 「切换窗口全屏」都无效。
            'system_toggle_fullscreen': window_toggle_fullscreen,
            'system_set_window_fullscreen': window_set_fullscreen,
            'system_get_window_fullscreen': window_get_fullscreen,
            # 插件设置弹窗里 type:"directory" 字段的浏览入口（shell/folder-picker.js）：
            # 浏览本机绝对路径，走共享基建（与 image-viewer 的 browse_dir 同一套实现；
            # 空路径/哨兵 = 盘符层）。
            'system_browse_dir': lambda path='': list_subdirectories(path),
            # 系统文件对话框：与桌面 js_api 注册的是**同一个实现**（见 file_dialog 的
            # 模块说明）。桌面模式下 Flask 就在 pywebview 那个进程里，因此这条 HTTP
            # 路径同样能弹对话框 —— 能不能弹取决于"有没有桌面窗口"，与请求走哪条路无关。
            'system_pick_path': pick_path,
            'system_get_shell_info': shell_info.get_info,
            # 背景图库：导入 / 删除 / 枚举。图片本身不经过 API，走 /file 无 plugin
            # 那条壳级路径（允许根见 serve_media_file 的追加逻辑）。
            'system_import_background_image': shell_info.import_background_image,
            'system_delete_background_image': shell_info.delete_background_image,
            'system_list_background_images': shell_info.list_background_images,
            'system_set_log_level': shell_info.set_log_level,
            'system_set_webview_high_performance': shell_info.set_webview_high_performance,
            'system_clear_thumb_caches': shell_info.clear_thumb_caches,
            'system_open_log_dir': shell_info.open_log_dir,
            # 壳偏好（启动与窗口 / 外观 / 背景 / 玻璃）：写路径唯一入口，
            # 读路径是 serve_shell 注入的 __OMNIBOX_SHELL_STATE__（零往返）。
            'system_get_shell_prefs': shell_prefs.read,
            'system_set_shell_prefs': shell_prefs.apply_patch,
        })
        # 统一刷新基建（同步 / 校验）：方法表来自 plugin_manager 一处定义，
        # 桌面模式的 ShellAPI 更新同一张表（见 freshness_api_methods 的说明）
        api_methods.update(plugin_manager.freshness_api_methods())
        # 管理员判定包在方法本身上：HTTP 与桌面 js_api 共用同一份（见 guard_admin_methods）
        api_methods = guard_admin_methods(api_methods)

        fn = api_methods.get(method)
        if fn is None:
            abort(404)

        # 管理员专属端点（见 _ADMIN_ONLY_API）：判定包在方法上（guard_admin_methods，
        # 与桌面模式的 js_api 通道共用）。令牌有效但角色不够时返回 403 而不是 401 ——
        # 401 的意思是"你没登录"，会让前端把人踢回登录流程。
        try:
            payload = request.get_json(silent=True) or {}
            args = payload.get('args', []) if isinstance(payload.get('args'), list) else []
            kwargs = payload.get('kwargs', {}) if isinstance(payload.get('kwargs'), dict) else {}
            result = fn(*args, **kwargs)
            return {'result': result}
        except AdminRequired:
            abort(403)
        except Exception as e:
            # 必须留日志：这一支是"接口抛了意料之外的异常"的唯一出口，而它只把
            # `str(e)` 回给调用方。不记录的话，服务端只剩 werkzeug 的一行
            # `"POST /api/xxx HTTP/1.1" 500 -`，异常类型与栈全都查不到 —— 实测定位
            # 一次"壳偏好偶发写失败"就卡在这里，只能临时插桩复现才拿到原因。
            log.exception(f'[api] {method} 抛出未预期异常：{type(e).__name__}: {e}')
            return {'error': str(e)}, 500

    def _shell_index_html() -> str:
        """读壳的 index.html 并注入首屏状态。

        壳偏好不再放 localStorage，而是由这里在**首帧之前**写好：前端同步读
        `window.__OMNIBOX_SHELL_STATE__`，因此主题与自定义色不会闪一下默认色。
        """
        dist_index = frontend_dist / 'index.html'
        if not dist_index.exists():
            # 前端未构建：退回落盘目录，让 /shell/* 的 dist→public 回退行为保持一致
            abort(404)
        try:
            source = dist_index.read_text(encoding='utf-8')
        except OSError as e:
            log.error(f'[File_Server] 读取 {dist_index} 失败: {e}')
            abort(404)
        state = {
            'prefs': _page_prefs(),
            # 盘上是否已有偏好段：前端据此决定要不要做旧 localStorage 的一次性迁移。
            # 不能用"注入值里有没有某个键"来猜 —— 注入的默认值与"读不到"完全同形。
            'stored': shell_prefs.has_stored(),
        }
        # 转义 `<` 与 `&`：颜色值是用户输入，出现 `</script>` 会直接闭合脚本块
        # （`json.dumps` 本身不转义它们，`\u003c` 才是 HTML 里安全的写法）。
        payload = (json.dumps(state, ensure_ascii=False)
                   .replace('<', '\\u003c')
                   .replace('&', '\\u0026'))
        script = f'<script>window.__OMNIBOX_SHELL_STATE__={payload};</script>'
        # 插在 <head> 的第一个子节点位置：内联脚本必须早于 /src/main.ts 执行
        # （模块脚本是延后执行的，但同文档里"越早写越好"仍是唯一稳妥的写法），
        # 而它不能被后面的任何东西挤掉 —— 找不到 <head> 就整段前置。
        marker = '<head>'
        index = source.find(marker)
        if index >= 0:
            at = index + len(marker)
            return source[:at] + script + source[at:]
        return script + source

    def _shell_index_response():
        """index.html 的响应：动态内容，因此显式禁缓存。

        一份被缓存的 index.html 会把用户刚改的主题与启动页按旧值发出来，而且这种
        "改了但重启后回到旧值"的现象与缓存外的缺陷长得一样，只能靠响应头区分。
        """
        resp = app.response_class(_shell_index_html(), mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        return resp

    @app.route('/')
    @app.route('/<path:filename>')
    def serve_shell(filename='index.html'):
        # /api 命名空间不属于 SPA 前端路由：GET 到不存在的 API 路径应返回
        # 明确的 404，而不是被 fallback 吞成空白 index.html。
        if filename.startswith('api/'):
            abort(404)
        # 未构建的前端：沿用 /shell/* 的两层回退思路，给一句能照着做的日志，
        # 而不是让每个请求都撞一个 404（原来的实现会返回一个空的 index.html）。
        if not (frontend_dist / 'index.html').exists():
            log.error(f'[File_Server] 壳前端未构建，index.html 不存在: {frontend_dist}'
                      f'（请运行 npm --prefix shell/frontend run build）')
            abort(404)
        if filename in ('', 'index.html'):
            return _shell_index_response()
        if (frontend_dist / filename).exists() or filename.startswith('assets'):
            return send_from_directory(frontend_dist, filename)
        # 其余路径是 SPA 前端路由（/settings、/status、插件路由）：交给前端
        return _shell_index_response()
    def _needs_content(instance, full_path: Path) -> bool:
        """该路径是否"存在但内容还没真正取到本地"（决定要不要回调 `ensure_file`）。

        判定：
          * 文件不存在 —— 普通插件按需生成内容的场景，返回 True；
          * **文件存在但是占位** —— 物化类插件（group-mesh）先造 0 字节占位文件、
            再靠 `ensure_file` 取真字节，返回 True。只判"不存在"会把占位当正常文件
            返回（实测踩到：HTTP 200 + 0 字节，远端内容永远不会被取回）；
          * 目录 —— 返回 False，不触发 `ensure_file`。
        """
        if full_path.is_file():
            try:
                return bool(instance.is_content_placeholder(full_path))
            except Exception:
                return False
        # 目录不是"内容还没取回的文件"：交给 ensure_file 是误用（插件会按文件
        # 处理它），目录请求由调用方的 is_file() 判定 404；其余形态（不存在、
        # 断裂符号链接）都按"需要取内容"处理。
        return not full_path.is_dir()

    def serve_media_file(filepath, plugin_name):
        """媒体/文件访问：支持相对路径和绝对路径，并做越权目录校验。"""
        instance = plugin_manager.get_plugin_instance(plugin_name) if plugin_name else None
        if plugin_name and instance is None:
            log.info(f"[FileServer] 找不到插件 {plugin_name} 的实例")
            abort(404)
        # 确定允许访问的根目录（支持插件跨多个媒体目录）：get_file_roots() 是
        # PluginBase 的正式成员，默认 [get_data_root()]，不再做 getattr 探针。
        # 插件实现抛异常时逐级回退到全局数据根，而不是让整条路由 500（审计项 P3-3）。
        roots = _resolve_file_roots(
            instance, Path(config['directories']['data_root']))
        # 壳级请求（无 plugin 参数）：把背景图库（`<config>/backgrounds`）并入允许根。
        #
        # 只在 instance is None 时追加：插件请求的放行范围必须继续由插件自己的
        # get_file_roots() 单独决定，否则"壳的壁纸库"会变成所有插件都能读的根。
        background_dirs: List[str] = []
        if instance is None:
            background_dirs = [str(background_library_dir(get_config_dir()))]
        try:
            # 归一化两侧（剥 `\\?\` 前缀 + resolve）：根写扩展形式时，请求里的普通
            # 形式与它做包含判定会得出"越界"的错误结论；而受保护判定需要请求路径
            # 与受保护路径处在同一坐标系里才拦得住。
            roots = [normalize_path(root) for root in roots if root]
            # 背景图库另算一类根（只放行图片，见 _match_file_root）。归一化后的形式
            # 才与 roots 里的元素相等，因此这行必须与上一行同时完成。
            background_roots = [normalize_path(item) for item in background_dirs if item]
            # 与基础根重合的目录不算图片根：它本来就是全类型放行的根（无 plugin
            # 参数时以 data_root 为根），把 data_root 一并当图片库不该让既有路径
            # 突然 403。嵌套的情形由 roots 的顺序解决 —— 基础根先匹配，基础根里的
            # 文件仍按基础根放行。
            image_only_roots = {root for root in background_roots if root not in roots}
            roots = roots + background_roots
        except Exception:
            abort(400)

        # 安全检查
        try:
            decoded_path = Path(filepath)
            if decoded_path.is_absolute():
                # 绝对路径：逐根目录校验（media-player 等跨根插件使用）
                full_path = normalize_path(decoded_path)
                # 凭据文件优先于「是否在根内」判定：插件可以把根设成包含 .config 的目录
                _reject_protected_file(full_path)
                if not _match_file_root(full_path, roots, image_only_roots):
                    abort(403)
                # 按需把内容取到本地（如 group-mesh 的远端共享项）：必须放在"文件
                # 是否存在"判定之前，且**在根校验之后** —— 插件实现会按这个路径写盘，
                # 先校验才不会让它往根之外写。与 /thumbs 的 ensure_thumb 同一顺序。
                # 判定条件不能只看"文件不存在"：物化出来的占位文件是存在的（0 字节），
                # 只判 exists 会把占位当正常文件返回（实测踩到：200 + 0 字节）。
                if instance is not None and _needs_content(instance, full_path):
                    try:
                        instance.ensure_file(full_path)
                    except Exception:
                        pass
                if not full_path.is_file():
                    abort(404)
                return send_file(full_path, conditional=True)
            # 相对路径：先问插件能不能解释这条虚拟路径（多根目录 / 命名空间前缀，
            # 如 image-viewer 的 `__额外图库/…`），它答不上来再按老规矩用第一根拼。
            # 顺序与绝对路径分支一致：解析 → 受保护判定 → 逐根校验 → 按需取字节。
            resolved = None
            if instance is not None:
                try:
                    candidate = instance.resolve_file_path(filepath)
                except Exception as e:
                    # 插件实现是自由代码：它抛错不能让整条路由 500，退回默认解析
                    log.warning(f'[File_Server] {plugin_name}.resolve_file_path 失败，回退默认解析: {e}')
                    candidate = None
                if isinstance(candidate, Path):
                    resolved = candidate
                elif candidate is not None:
                    log.warning(f'[File_Server] {plugin_name}.resolve_file_path 返回了 '
                                f'{type(candidate).__name__}，已忽略（只接受 Path 或 None）')
            if resolved is not None:
                full_path = normalize_path(resolved)
                _reject_protected_file(full_path)
                # 与绝对路径分支共用同一个包含判定（这条分支只在有插件时进入，
                # image_only_roots 为空，因此语义不变）。
                if not _match_file_root(full_path, roots, image_only_roots):
                    abort(403)
                if instance is not None and _needs_content(instance, full_path):
                    try:
                        instance.ensure_file(full_path)
                    except Exception:
                        pass
                if not full_path.is_file():
                    abort(404)
                return send_file(full_path, conditional=True)
            # 沿用「插件数据根目录」语义
            data_root = roots[0]
            full_path = normalize_path(data_root / filepath)
            _reject_protected_file(full_path)
            if not _is_safe_path(full_path, data_root):
                abort(403)
            # 与上面两个分支一致：文件不存在、或存在但是 0 字节占位时都回调
            # `ensure_file`。只判 `exists()` 会把占位当正常文件直接返回
            # 200 + 0 字节，远端内容永远不会被取回（见 `_needs_content` 的说明；
            # `/files/<相对路径>` 这类请求正走这里）。
            if instance is not None and _needs_content(instance, full_path):
                try:
                    instance.ensure_file(full_path)
                except Exception:
                    pass
            if not full_path.is_file():
                abort(404)
            return send_file(full_path, conditional=True)
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
    def _thumb_dir_for(plugin_name: str, instance) -> Path:
        """本次请求使用的缩略图目录（归一化后的路径）。"""
        fallback = normalize_path(Path(config['directories']['data_root']) / '.cache' / 'thumbs')
        if plugin_name and instance is not None:
            return normalize_path(_resolve_thumb_dir(instance) or fallback)
        # 无插件上下文：回退到全局缩略图目录（通常不存在）
        return fallback

    def _checked_thumb_path(thumb_dir: Path, filepath: str) -> Path:
        """`thumb_dir` 下的目标路径；受保护路径与越界路径在这里就拒绝。

        必须在调用插件的 `get_thumb_data()` / `ensure_thumb()` **之前**执行：那两个
        方法是插件实现，Shell 管不了它们拿到 filepath 之后去读哪个文件。原实现只在
        散文件分支判包含，于是"插件直接返回字节"那条路整条绕过了越界防护 —— 同一个
        `../` 在散文件分支是 403、在字节分支是 200（审计项 P3-1）。两条分支现在共用
        这一个函数，不会再出现"改了其中一条、另一条照旧"。

        异常映射与越界语义：`abort()` 抛的 HTTPException 也是 Exception，不还原
        `e.code` 的话越界会从 403 变成 400（语义错、也不利于排查）。
        """
        try:
            full_path = normalize_path(thumb_dir / filepath)
            # thumb_dir 本身可以由插件指定（PluginBase.thumb_dir）：它若落在 .config 上，
            # "auth_token.txt" 就是一条合法相对路径
            _reject_protected_file(full_path)
            if not _is_safe_path(full_path, thumb_dir):
                abort(403)
        except Exception as e:
            code = getattr(e, 'code', None)
            if code in (400, 403, 404):
                abort(code)
            abort(400)
        return full_path

    @app.route('/thumbs/<path:filepath>')
    def serve_thumb(filepath):
        plugin_name = request.args.get('plugin', '')
        instance = plugin_manager.get_plugin_instance(plugin_name) if plugin_name else None
        if plugin_name and instance is None:
            log.info(f"[File_Server-Thumbs] 找不到插件 {plugin_name} 的实例")
            abort(404)

        thumb_dir = _thumb_dir_for(plugin_name, instance)
        # 受保护判定与包含判定都必须早于任何插件代码（见 _checked_thumb_path）
        full_path = _checked_thumb_path(thumb_dir, filepath)

        # 新路径：插件可直接返回 SQLite 缩略图字节，避免散文件随机 I/O。
        # get_thumb_data() 是 PluginBase 的正式成员，默认返回 None（= 本插件不提供字节）。
        # 优先级：本方法命中优先，返回 None
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

        return _send_thumb_file(plugin_name, instance, filepath, thumb_dir, full_path)

    def _send_thumb_file(plugin_name: str, instance, filepath: str,
                         thumb_dir: Path | None = None, full_path: Path | None = None):
        """thumb_dir 散文件布局：默认 数据根/.cache/thumbs，找不到时按需生成再读。"""
        if thumb_dir is None:
            thumb_dir = _thumb_dir_for(plugin_name, instance)
        # 调用方已经判过就不重复（serve_thumb 两条分支共用同一份判定）
        if full_path is None:
            full_path = _checked_thumb_path(thumb_dir, filepath)

        # 按需生成缩略图：契约是"散文件不存在时现场生成并落盘"
        # （PluginBase.ensure_thumb 的说明）。散文件已存在时不再回调插件，
        # 避免每次 /thumbs 请求都进入插件代码。
        if instance is not None and not full_path.exists():
            try:
                instance.ensure_thumb(filepath)
            except Exception:
                pass

        if not full_path.exists():
            abort(404)

        return send_from_directory(thumb_dir, filepath)
    @app.route('/shell/<path:filename>')
    def serve_shell_assets(filename):
        # 注意：**dist 优先**。改了 shell/frontend/public/shell/* 而不重新构建
        # （npm --prefix shell/frontend run build）时，这里发的仍是 dist 里的旧副本，
        # 表现为"源码改了、界面没变"（曾被误判成 CSS 写错：新标记 + 旧样式 = 胶囊外框消失）。
        dist_shell = _SHELL_DIR / 'frontend' / 'dist' / 'shell'
        if (dist_shell / filename).exists():
            return send_from_directory(dist_shell, filename)
        # 开发期未构建进 dist 的新共享组件直接从 public/shell 提供。
        public_shell = _SHELL_DIR / 'frontend' / 'public' / 'shell'
        return send_from_directory(public_shell, filename)

    @app.route('/res/<path:filename>')
    def serve_res_assets(filename):
        """仓库级共享资源（`res/`）：**只有一份**，没有 dist/public 两层。

        与 `/shell/*` 的关键区别在这里：`/shell/*` 的"优先发 dist"是因为
        `public/shell/*` 会被 Vite 复制进 `dist/shell/`，于是同一个文件存在两份。
        图标 sprite 刻意避开这个模式 —— 早先它放在 `public/shell/` 时确实出现过
        "改了源文件、界面没变"（发的是 dist 里的旧副本）。`res/` 不被 Vite 处理，
        所以这里直接发源文件，改完立即生效，不需要重新构建。
        """
        return send_from_directory(_RES_DIR, filename)

    @app.route('/plugins/<plugin_name>/frontend/<path:filename>')
    def serve_plugin_frontend(plugin_name, filename):
        plugin_root = plugin_manager.get_plugin_dir(plugin_name)
        if not plugin_root:
            abort(404)
        plugin_dir = plugin_root / 'frontend'
        if not plugin_dir.exists():
            abort(404)
        # 插件前端是**免令牌**路由（页面本身不含用户数据），所以它同样不能成为
        # 受保护文件的出口：插件把凭据放进 frontend/、或申报了覆盖它的目录时，
        # 这里不判定就等于绕过了 /file 的同一份清单。
        _reject_protected_file(normalize_path(plugin_dir / filename))
        # 插件前端的**任何** HTML 页面都要注入壳的引导脚本（Bridge / Utils / Toast /
        # 主题同步），不只是 index.html：插件可以有子页面 —— 例如"网络位置"提供方要在
        # 共享目录组件（FolderPicker）弹窗的 iframe 里渲染一个选择器，那个页面同样需要
        # `window.Bridge` 才能调宿主与插件接口。以前只有 index.html 走这条注入，子页面
        # 拿不到 Bridge（症状是"PyWebView API 不可用"，而页面本身看起来完全正常）。
        if filename.lower().endswith('.html'):
            html_path = plugin_dir / filename
            # 手工 open 的文件路径必须自己确认落在插件前端目录内（send_from_directory
            # 那层防护只覆盖它自己的发送路径，不覆盖这里的读文件）。
            if not _is_safe_path(normalize_path(html_path), normalize_path(plugin_dir)):
                abort(403)
            if html_path.exists():
                with open(html_path, 'r', encoding='utf-8') as f:
                    html = f.read()
                inject = _PLUGIN_BOOTSTRAP_SCRIPT.replace('PLACEHOLDER_NAME', plugin_name)
                html = html.replace('</head>', inject + '</head>')
                resp = make_response(_inject_appearance_vars(html, shell_prefs.read()))
                resp.mimetype = 'text/html'
                # 这份 HTML 里带着**当前**的外观变量（见 _inject_appearance_vars）：
                # 让浏览器缓存它，用户改了侧栏宽度或主题后要等到缓存过期才会生效。
                resp.headers['Cache-Control'] = 'no-store, must-revalidate'
                return resp
        return send_from_directory(plugin_dir, filename)

    return app
