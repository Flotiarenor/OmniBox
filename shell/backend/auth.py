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

OmniBox HTTP 数据路由的访问令牌。

`--web-only` / 局域网部署时，`/api`、`/file`、`/thumbs` 等数据路由会携带
用户数据，必须持有令牌才能访问。令牌持久化在 `<config>/auth_token.txt`，
首次启动自动生成；文件缺失或损坏时自动重建。

携带方式二选一（浏览器页面会由服务端自动种下 HttpOnly Cookie，无需手动处理）：
- Cookie: `omnibox_token=<token>`（页面加载时自动下发，同源请求自动携带）
- 请求头: `X-Omnibox-Token: <token>`（curl / nginx 注入等场景）'''

import hmac
import os
import secrets
from pathlib import Path
from typing import Dict

TOKEN_COOKIE = 'omnibox_token'
TOKEN_HEADER = 'X-Omnibox-Token'
TOKEN_FILE_NAME = 'auth_token.txt'

# 进程内缓存：{配置目录: 令牌}。保证同一进程、同一配置目录多次调用返回同一个令牌
# （令牌文件写入失败时回退为本次启动的随机令牌，也依赖此缓存保持一致）。
#
# 必须**按配置目录分键**：它原先是一个进程级单值，而"同一进程里出现多个配置目录"在
# 用例里是常态（`mock.patch('...get_config_dir')` 或 `OMNIBOX_HOME` 指向临时目录）。
# 只存一个值时，先跑的临时目录会把令牌留在缓存里，后面使用真实配置目录的用例拿到的
# 是那枚临时令牌 —— 表现是"单独跑绿、按某种顺序跑 401"，而 401 完全不指向真实原因。
_token_cache: Dict[str, str] = {}


def get_token_file(config_dir: Path) -> Path:
    """返回令牌文件路径：<config_dir>/auth_token.txt"""
    return Path(config_dir) / TOKEN_FILE_NAME


def get_or_create_token(config_dir: Path) -> str:
    """返回本进程该配置目录的访问令牌：优先从文件加载，缺失/损坏则生成并持久化。

    持久化令牌保证应用重启后令牌不变（nginx 反代配置、浏览器书签、
    外部脚本等无需随重启更新）。文件不可写时退化为本次启动随机令牌。
    """
    cached = _token_cache.get(str(config_dir))
    if cached:
        return cached

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

    _token_cache[str(config_dir)] = token
    return token


def token_matches(supplied: str, token: str) -> bool:
    """恒定时间比较，避免时序侧信道；空值/类型异常一律不通过。"""
    if not supplied or not token:
        return False
    try:
        return hmac.compare_digest(supplied, token)
    except (TypeError, ValueError):
        return False
