# -*- coding: utf-8 -*-
"""触发并展示 OmniBox 各类 HTTP 状态页 / 应用内错误区域，便于调试。

用法:
    python tests/debug_status_pages.py [--port 18089] [--no-open]

行为:
    1. 创建临时「坏插件」(backend 正常、frontend 缺失 → 404)，
       与真实插件一起加载，以 --status-debug 语义启动 web-only 服务器；
    2. 用 requests 触发一轮全部异常场景并打印结果表；
    3. 自动用系统浏览器打开壳内调试面板 http://127.0.0.1:<port>/status：
       - 200 健康检查、API 鉴权 401/200、错误令牌 401（演示已禁用 Cookie）
       - 401 / 403 / 404 独立标记页（后端兜底，新标签打开）
       - 插件 iframe 404 → 壳检测 → 跳转 /status?code=404 错误卡片（重试返回）
       - SPA fallback、API 404 Toast 等
    4. Ctrl+C 退出，自动清理坏插件目录。
"""

import argparse
import json
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

BAD_PLUGIN_NAME = 'debug-bad-plugin'
BAD_PLUGIN_DIR = PROJECT_ROOT / '.build' / 'debug-plugins' / BAD_PLUGIN_NAME
DEBUG_DATA_ROOT = PROJECT_ROOT / '.build' / 'debug-status-data'

BAD_PLUGIN_MANIFEST = {
    "name": BAD_PLUGIN_NAME,
    "version": "1.0.0",
    "displayName": "调试坏插件 (404)",
    "icon": "💥",
    "description": "调试用：backend 正常、frontend 缺失，用于触发壳内 404 错误视图",
    "dependencies": [],
    "permissions": [],
    "backend": {"entry": "backend/main.py", "class": "DebugBadPlugin"},
    "frontend": {"entry": "frontend/index.html", "route": "/debug-bad-plugin"},
}

BAD_PLUGIN_BACKEND = '''\
# -*- coding: utf-8 -*-
"""调试用坏插件后端：正常加载，但 frontend/index.html 不存在。"""
from shell.backend.plugin_base import PluginBase


class DebugBadPlugin(PluginBase):
    settings_schema = []

    def register_api(self) -> dict:
        return {}
'''


def make_bad_plugin() -> None:
    """创建临时坏插件（backend 正常、frontend 缺失）。"""
    if BAD_PLUGIN_DIR.exists():
        shutil.rmtree(BAD_PLUGIN_DIR)
    backend_dir = BAD_PLUGIN_DIR / 'backend'
    backend_dir.mkdir(parents=True)
    (BAD_PLUGIN_DIR / 'manifest.json').write_text(
        json.dumps(BAD_PLUGIN_MANIFEST, ensure_ascii=False, indent=2), encoding='utf-8')
    (backend_dir / 'main.py').write_text(BAD_PLUGIN_BACKEND, encoding='utf-8')
    # 注意：不创建 frontend/ 目录 → /plugins/debug-bad-plugin/frontend/index.html 返回 404


def cleanup() -> None:
    shutil.rmtree(BAD_PLUGIN_DIR, ignore_errors=True)
    shutil.rmtree(DEBUG_DATA_ROOT, ignore_errors=True)


def start_server(port: int):
    """启动带坏插件的 web-only 服务器（--status-debug 模式），返回 (app, manager)。"""
    from shell.backend.file_server import create_app
    from shell.backend.plugin_manager import PluginManager

    config = {
        'server': {'host': '127.0.0.1', 'port': port},
        'directories': {'data_root': str(DEBUG_DATA_ROOT)},
        # 调试模式：壳内 /status 视图显示调试面板
        'debug': {'status_debug': True},
    }
    DEBUG_DATA_ROOT.mkdir(parents=True, exist_ok=True)

    search_dirs = [PROJECT_ROOT / 'plugins', BAD_PLUGIN_DIR.parent]
    manager = PluginManager(search_dirs, config=config)
    manager.load_all()
    app = create_app(config, manager)

    threading.Thread(
        target=lambda: app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False, threaded=True),
        daemon=True,
    ).start()
    return app, manager


def wait_health(port: int, timeout: float = 10.0) -> bool:
    import requests
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f'http://127.0.0.1:{port}/health', timeout=0.5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def probe_all(port: int) -> None:
    """用 requests 触发一轮全部场景并打印结果表。"""
    import requests
    base = f'http://127.0.0.1:{port}'

    s = requests.Session()
    s.get(base + '/', timeout=5)  # 拿到令牌 Cookie

    def show(no, name, r):
        ctype = r.headers.get('Content-Type', '')[:22]
        mark = 'data-status-page' in r.text if 'html' in ctype else '-'
        print(f'[{no:>2}] {name:<46} -> {r.status_code}  [{ctype}] {mark}')

    print('\n===== 触发结果表 =====')
    show(1, 'GET /health', requests.get(base + '/health', timeout=5))
    show(2, 'POST /api/system_get_config (无令牌)', requests.post(base + '/api/system_get_config', json={}, timeout=5))
    show(3, 'POST /api/system_get_config (错误令牌)', requests.post(
        base + '/api/system_get_config', json={}, headers={'X-Omnibox-Token': 'bad'}, timeout=5))
    show(4, 'POST /api/system_get_config (带Cookie)', s.post(base + '/api/system_get_config', json={}, timeout=5))
    show(5, 'POST /api/no_such_method (带Cookie, 404)', s.post(base + '/api/no_such_method', json={}, timeout=5))
    show(6, 'GET /thumbs/x.png (无令牌, 401页)', requests.get(base + '/thumbs/x.png', timeout=5))
    show(7, 'GET /file?path=../secret (越权, 403页)', s.get(base + '/file?path=..%2Fsecret&plugin=image-viewer', timeout=5))
    show(8, 'GET /plugins/nope/frontend/index.html (404页)', requests.get(base + '/plugins/nope/frontend/index.html', timeout=5))
    show(9, 'GET /plugins/debug-bad-plugin/frontend/index.html (坏插件404)', requests.get(
        base + '/plugins/debug-bad-plugin/frontend/index.html', timeout=5))
    show(10, 'GET /api/system_get_config (GET方法, 404 JSON)', requests.get(base + '/api/system_get_config', timeout=5))
    show(11, 'GET /some/spa/route (SPA fallback)', requests.get(base + '/some/spa/route', timeout=5))
    print('==================================\n')


# ===== 调试模式说明 =====
# 调试面板不再是独立页面：它由壳内建视图 /status 渲染（与 /settings 同级），
# 启动参数 --status-debug 时显示健康检查 / API 鉴权 / 标记页演示。
# 本脚本以 --status-debug 语义启动服务器，浏览器打开 /status 即为调试面板；
# 导航栏「💥 调试坏插件」用于触发「iframe 404 → 壳检测 → 跳转 /status?code=404」链路。


def main():
    # 行缓冲：后台/管道运行时结果表也能实时看到
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description='触发并展示 OmniBox 状态页/错误区域（调试用）')
    parser.add_argument('--port', type=int, default=18089, help='调试服务器端口（默认 18089）')
    parser.add_argument('--no-open', action='store_true', help='不自动打开浏览器')
    args = parser.parse_args()

    port = args.port
    make_bad_plugin()
    try:
        app, manager = start_server(port)
        if not wait_health(port):
            print(f'[debug] 服务器启动失败（端口 {port} 可能被占用），尝试下一个端口…')
            port += 1
            app, manager = start_server(port)
            if not wait_health(port):
                print('[debug] 启动失败，请检查端口与日志')
                return

        probe_all(port)

        url = f'http://127.0.0.1:{port}/status'
        print(f'调试面板: {url}（壳内 /status 视图，需 --status-debug 启动）')
        print('导航栏中新增「💥 调试坏插件」入口，点击可触发: iframe 404 → 壳检测 → 跳转 /status?code=404 → 错误卡片 → 重试返回')
        print('Ctrl+C 退出（自动清理坏插件与临时数据）。')
        if not args.no_open:
            webbrowser.open(url)

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n[debug] 退出中…')
    finally:
        cleanup()


if __name__ == '__main__':
    main()
