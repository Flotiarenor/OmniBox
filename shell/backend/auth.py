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

"""OmniBox HTTP 数据路由的访问令牌。

`--web-only` / 局域网部署时，`/api`、`/file`、`/thumbs` 等数据路由会携带
用户数据，必须持有令牌才能访问。令牌持久化在 `<config>/auth_token.txt`，
首次启动自动生成；文件缺失或损坏时自动重建。

携带方式二选一（浏览器页面会由服务端自动种下 HttpOnly Cookie，无需手动处理）：
- Cookie: `omnibox_token=<token>`（页面加载时自动下发，同源请求自动携带）
- 请求头: `X-Omnibox-Token: <token>`（curl / nginx 注入等场景）
"""

import hmac
import os
import secrets
from pathlib import Path

TOKEN_COOKIE = 'omnibox_token'
TOKEN_HEADER = 'X-Omnibox-Token'
TOKEN_FILE_NAME = 'auth_token.txt'

# 进程内缓存：保证同一进程多次调用返回同一个令牌
# （令牌文件写入失败时回退为本次启动的随机令牌，也依赖此缓存保持一致）
_token_cache: str | None = None


def get_token_file(config_dir: Path) -> Path:
    """返回令牌文件路径：<config_dir>/auth_token.txt"""
    return Path(config_dir) / TOKEN_FILE_NAME


def get_or_create_token(config_dir: Path) -> str:
    """返回本进程的访问令牌：优先从文件加载，缺失/损坏则生成并持久化。

    持久化令牌保证应用重启后令牌不变（nginx 反代配置、浏览器书签、
    外部脚本等无需随重启更新）。文件不可写时退化为本次启动随机令牌。
    """
    global _token_cache
    if _token_cache:
        return _token_cache

    token: str | None = None
    token_file = get_token_file(config_dir)
    try:
        if token_file.exists():
            candidate = token_file.read_text(encoding='utf-8').strip()
            if len(candidate) >= 16:
                token = candidate
    except OSError:
        token = None

    if not token:
        token = secrets.token_urlsafe(32)
        try:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(token, encoding='utf-8')
            try:
                os.chmod(token_file, 0o600)
            except OSError:
                pass  # Windows 上 chmod 仅设置只读位，忽略
        except OSError:
            pass  # 无法持久化：本次启动使用随机令牌

    _token_cache = token
    return token


def token_matches(supplied: str, token: str) -> bool:
    """恒定时间比较，避免时序侧信道；空值/类型异常一律不通过。"""
    if not supplied or not token:
        return False
    try:
        return hmac.compare_digest(supplied, token)
    except (TypeError, ValueError):
        return False
