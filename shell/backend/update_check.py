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

应用内检查更新：A（检查与提示）+ B（下载与校验）。

为什么放在壳后端而不是前端：检查要读运行期形态（是否冻结、是否便携、安装目录里有没有
安装器留下的卸载器），下载要写用户数据根并做流式摘要校验 —— 这两件事都只有后端知道。
版本号仍由前端把构建期常量当入参传进来（`system_check_update(currentVersion)`），
后端不另存一份版本副本，见 docs/update-check.md。

三条落地约束：

* **不抛异常**：所有失败都变成带 `code` 的信封返回。这些端点由用户点一下按钮触发，
  一次 DNS 失败、一次限流不该变成 `api_proxy` 的 500，也不该让设置页弹阻塞框。
* **下载地址只来自上一次检查的缓存**：`start_download` 不接受 URL，只接受版本号与
  资产名，两者必须与缓存完全一致，否则判 `stale`。否则这个端点就成了"让壳去下载任意
  URL 并写到数据根"的入口。
* **重定向手动逐跳校验**：`requests` 自动跟随对 `file://` 抛 `InvalidSchema`（不是
  `ConnectionError`），会被错误分类成"连不上"；而且自动跟随不给逐跳检查主机名的机会。
  因此这里 `allow_redirects=False` + 自己循环，每跳都校验协议与主机。

下载复用 `shell/backend/tasks.py` 的 `BackgroundTask`：进度、取消、状态查询都是它已有的
语义，另起一套线程壳只会让"取消"与"进度"各有一份实现。
'''

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from shell.backend.paths import (
    get_install_dir,
    get_user_data_dir,
    is_frozen,
    is_portable,
)
from shell.backend.tasks import BackgroundTask

log = logging.getLogger(__name__)

# ===== 生产常量（测试通过 UpdateTransport 注入替身，改这里不需要动用例） =====

#: GitHub REST 的根。检查只读一个响应，不拉 `/releases` 列表（草稿与预发布不会出现在
#: `/releases/latest` 里，自己挑一条会把预发布提示成正式升级）。
API_BASE = 'https://api.github.com'
RELEASES_PATH = '/repos/Flotiarenor/Cubby/releases/latest'
RELEASES_PAGE_URL = 'https://github.com/Flotiarenor/Cubby/releases/latest'
PROJECT_URL = 'https://github.com/Flotiarenor/Cubby'

#: 同一次运行内复用检查结果的时长（秒）。限流额度是每小时 60 次、按来源 IP 计，
#: 重复点击没有必要再花一次。
CHECK_CACHE_SECONDS = 60.0

#: 连接 / 读取超时。检查用短的：用户点完按钮在等结果。
API_TIMEOUT: Tuple[float, float] = (5.0, 15.0)
#: 下载用长的：资产是几十 MB 的安装器。
DOWNLOAD_TIMEOUT: Tuple[float, float] = (10.0, 60.0)
#: 累计墙钟上限：慢速滴流（每次读都不超时）会把界面永远卡在"下载中"。
DOWNLOAD_WALL_SECONDS = 900.0
#: 单份资产上限，同时对 `assets[].size` 与 `Content-Length` 预检。
MAX_UPDATE_BYTES = 512 * 1024 * 1024
#: `.sha256` 是几十字节的文本，读超这个量说明拿到的不是摘要文件。
MAX_SHA256_BYTES = 4096
#: 允许的重定向跳数。
MAX_REDIRECTS = 5

#: 允许的协议与主机（主机项以 `.` 开头表示后缀匹配）。
#:
#: 生产上**只**允许 https：http 一跳就能被中间人换掉安装器，摘要再对也没意义。
ALLOWED_SCHEMES: Tuple[str, ...] = ('https',)
ALLOWED_HOSTS: Tuple[str, ...] = ('github.com', '.githubusercontent.com')

#: 安装器写进 `{app}` 的卸载器。它是 Inno 的唯一稳定产物，zip 产物在编译安装器**之前**
#: 打出来，所以两者互斥；发布链的静默安装冒烟也断言它存在。
UNINSTALLER_NAME = 'unins000.exe'
ZIP_ASSET_NAME = 'Cubby-windows-x64.zip'
INSTALLER_ASSET_TEMPLATE = 'Cubby-Setup-{version}.exe'
SHA256_SUFFIX = '.sha256'

#: 下载中的半成品后缀（前面还有 pid，见 `_download_worker`）。
PART_SUFFIX = '.part'
#: 超过这个年龄的 `.part` 视为上次进程留下的残骸（进程被杀时来不及删）。
STALE_PART_SECONDS = 3600.0

#: `updates/` 目录名（挂在用户数据根下：系统 temp 可能被清理程序删掉，而"已下载待安装"
#: 要跨重启存活）。
UPDATE_DIRNAME = 'updates'
#: 流式写盘的分块大小。
DOWNLOAD_CHUNK_BYTES = 64 * 1024

#: Windows 上杀软 / 索引服务持有句柄是毫秒级的，`os.replace` 与 `unlink` 因此小步重试。
FS_RETRIES = 5
FS_RETRY_DELAY = 0.1

#: 五种形态：平台不支持 / 开发模式 / 便携 / 安装器装的 / zip 解压的。
FORM_PLATFORM = 'platform'
FORM_DEV = 'dev'
FORM_PORTABLE = 'portable'
FORM_INSTALLER = 'installer'
FORM_ZIP = 'zip'
_DOWNLOAD_FORMS = frozenset({FORM_INSTALLER, FORM_ZIP})

#: 形态 → 界面要显示的原因（`system_get_update_state` 的 `reason`）。
_FORM_REASONS = {
    FORM_PLATFORM: '当前平台不提供应用内检查更新',
    FORM_DEV: '开发模式不检查更新',
    FORM_PORTABLE: '便携模式只提示：数据在程序目录（不可写时在默认数据根），'
                   '请手动解压覆盖到同一目录',
    FORM_INSTALLER: '将用安装器关闭 Cubby 并覆盖安装，用户数据不受影响',
    FORM_ZIP: '将替换程序目录，用户数据在默认位置不受影响',
}

#: `X.Y.Z` 三段整数。刻意不用 packaging / semver：合法 tag 的形状由发版门禁强制，
#: 引通用版本库等于把"预发布""本地版本号"这些不支持的形状也放进来。
_VERSION_RE = re.compile(r'(\d+)\.(\d+)\.(\d+)')
#: `.sha256` 的形态：`<64 位十六进制> <文件名>`（发布链就是 `print(hexdigest, name)`）。
#: `*` 是 `sha256sum --binary` 会加的标记，一并认下。
_SHA256_LINE_RE = re.compile(r'([0-9a-fA-F]{64})\s+\*?(\S.*)')
#: User-Agent 里只允许这些字符：版本串来自前端，带 CR/LF 会让请求头构造失败。
_UA_CHARS_RE = re.compile(r'[^0-9A-Za-z._+-]')


class _UpdateError(Exception):
    """worker 内部的预期失败：`code` 直接进 `job['code']`。

    只在 worker 内部抛与接 —— 它不会穿出 `BackgroundTask`，因此不会变成
    `status()['errors']` 里的一条噪声，也不会把任务的 `success` 判成 False。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UpdateTransport:
    """一次检查 / 下载要用到的网络与限额参数。

    存在的唯一理由是**可测性**：生产默认值就是模块级常量，用例换成指向本地假服务的
    基地址、允许的主机与很小的字节上限，不必给被测函数塞一堆平铺参数。
    """

    api_base: str = API_BASE
    api_timeout: Tuple[float, float] = API_TIMEOUT
    download_timeout: Tuple[float, float] = DOWNLOAD_TIMEOUT
    max_bytes: int = MAX_UPDATE_BYTES
    allowed_schemes: Tuple[str, ...] = ALLOWED_SCHEMES
    allowed_hosts: Tuple[str, ...] = ALLOWED_HOSTS


# ===== 纯函数：版本、形态、资产、摘要、落点 =====


def parse_version(value: object) -> Optional[Tuple[int, int, int]]:
    """把版本串解析成三段整数元组；不符合 `[v]X.Y.Z` 时返回 None。

    只去掉**一个**前导 `v`：`vv1.2.3` 与 `v1.2.3.4` 都是不合法形状，宁可判
    `bad_version` 让用户去发布页，也不要猜一个版本出来比较。
    """
    if not isinstance(value, str):
        return None
    # `removeprefix` 只去掉**一个**前导 `v`：`vv1.2.3` 因此仍是不合法形状。
    text = value.strip().removeprefix('v')
    match = _VERSION_RE.fullmatch(text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _version_text(value: object) -> str:
    """把 tag 归一成用于展示与比较的版本串（去一个前导 `v`）。"""
    if not isinstance(value, str):
        return ''
    return value.strip().removeprefix('v')


def install_form(*, frozen: bool, portable: bool, install_dir: Path, platform: str) -> str:
    """判定这份程序属于哪种形态（顺序即优先级）。

    * 非 Windows → `platform`（2.0 不纳入应用内检查更新）；
    * 未冻结 → `dev`（从源码跑时没有"发行版本"可对照）；
    * 有便携标记 → `portable`（数据跟随程序目录，自替换等于搬走数据）；
    * 安装目录里有 `unins000.exe`（**必须是文件**）→ `installer`；
    * 其余 → `zip`。

    `unins000.exe` 是目录时仍判 `zip`：判据要的是"安装器写过这个文件"，而一个同名
    目录说明不了这件事，也不能被 `os.startfile` 当程序启动。
    """
    if platform != 'win32':
        return FORM_PLATFORM
    if not frozen:
        return FORM_DEV
    if portable:
        return FORM_PORTABLE
    if (Path(install_dir) / UNINSTALLER_NAME).is_file():
        return FORM_INSTALLER
    return FORM_ZIP


def current_form() -> str:
    """本进程当前的形态（判定输入全部来自运行期事实，不读注册表）。"""
    return install_form(
        frozen=is_frozen(),
        portable=is_portable(),
        install_dir=get_install_dir(),
        platform=sys.platform,
    )


def expected_asset_name(form: str, version: str) -> Optional[str]:
    """该形态要下载的资产名；不下载的形态返回 None。"""
    if form == FORM_INSTALLER:
        return INSTALLER_ASSET_TEMPLATE.format(version=version)
    if form in (FORM_PORTABLE, FORM_ZIP):
        # 便携形态只给手动步骤，但"要解压哪个包"仍然是同一个 zip 资产。
        return ZIP_ASSET_NAME
    return None


def select_asset(form: str, assets: Sequence[Any], version: str) -> Optional[Dict[str, Any]]:
    """按形态从发布信息里挑出主资产，并确认 `.sha256` 资产也在。

    返回 `{'name', 'size', 'url', 'sha256_url'}`；没有该形态的资产、或没有配套的
    `.sha256`（没有它就无法证明文件完整）时返回 None —— 调用方据此判 `no_asset`，
    不发起任何下载。
    """
    expected = expected_asset_name(form, version)
    if expected is None:
        return None
    entries: Dict[str, Dict[str, Any]] = {}
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if isinstance(name, str) and name:
            entries[name] = item

    main = entries.get(expected)
    if main is None:
        return None
    url = main.get('browser_download_url')
    if not isinstance(url, str) or not url.strip():
        return None
    sha_entry = entries.get(expected + SHA256_SUFFIX)
    if sha_entry is None:
        return None
    sha_url = sha_entry.get('browser_download_url')
    if not isinstance(sha_url, str) or not sha_url.strip():
        sha_url = url.strip() + SHA256_SUFFIX

    size = main.get('size')
    total = int(size) if isinstance(size, int) and not isinstance(size, bool) else 0
    return {
        'name': expected,
        'size': total,
        'url': url.strip(),
        'sha256_url': sha_url.strip(),
    }


def parse_sha256(text: str) -> Optional[Tuple[str, str]]:
    """解析 `.sha256` 的内容 → `(摘要, 文件名)`；形状不符返回 None。

    形状错（不是一行、摘要不是 64 位十六进制、没有文件名）与"文件名不等于资产名"
    都归 `bad_response`：那是发布链或传输出了问题，不是"文件内容被换掉"。
    `digest_mismatch` 只表示摘要本身不相等。
    """
    if not isinstance(text, str):
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    match = _SHA256_LINE_RE.fullmatch(lines[0])
    if match is None:
        return None
    name = match.group(2).strip()
    if not name:
        return None
    return match.group(1).lower(), name


def get_updates_dir() -> Path:
    """下载落点：`<用户数据根>/updates/`（可随时删除的临时区）。

    不落系统 temp：那里可能被清理程序删掉，而"已下载待安装"要跨重启存活；放在数据根
    下也符合"运行期只写数据根"的既有约定。
    """
    return get_user_data_dir() / UPDATE_DIRNAME


# ===== 内部小工具 =====


def _ua_token(version: str) -> str:
    """把版本串收敛成可放进请求头的 token（挡住 CR/LF 与超长输入）。"""
    cleaned = _UA_CHARS_RE.sub('', str(version or '')).strip('.')[:32]
    return cleaned or 'unknown'


def _user_agent(version: str) -> str:
    return f'Cubby/{_ua_token(version)} (+{PROJECT_URL})'


def _safe_url(url: str) -> str:
    """日志里只写 scheme://host/path。

    GitHub 资产的重定向地址带签名串（query 里有下载凭据），整条写进日志等于把一次
    下载的授权复制到 `cubby.log` 里。
    """
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def _host_allowed(host: str, allowed_hosts: Sequence[str]) -> bool:
    """主机是否在白名单里：以 `.` 开头的项按后缀匹配（`*.githubusercontent.com`）。"""
    lowered = host.lower()
    for entry in allowed_hosts:
        item = entry.lower().strip()
        if not item:
            continue
        if item.startswith('.'):
            if lowered.endswith(item):
                return True
        elif lowered == item:
            return True
    return False


def _url_problem(url: str, transport: UpdateTransport) -> Optional[str]:
    """这个地址能不能请求；能则返回 None，否则返回一句原因。"""
    parts = urlsplit(str(url))
    scheme = (parts.scheme or '').lower()
    if scheme not in transport.allowed_schemes:
        return f'协议不允许: {scheme or "(空)"}'
    host = parts.hostname or ''
    if not host:
        return '缺少主机'
    if not _host_allowed(host, transport.allowed_hosts):
        return f'主机不允许: {host}'
    return None


def _retry_after_seconds(response: requests.Response) -> Optional[int]:
    """限流还要等多久：`Retry-After` 优先，其次 `X-RateLimit-Reset - now`。"""
    header = response.headers.get('Retry-After')
    if header:
        try:
            return max(0, int(float(str(header).strip())))
        except ValueError:
            pass
    reset = response.headers.get('X-RateLimit-Reset')
    if reset:
        try:
            return max(0, int(float(str(reset).strip()) - time.time()))
        except ValueError:
            pass
    return None


def _percent(received: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(100, int(received * 100 / total)))


def _remove_file(path: Path) -> bool:
    """删除一个文件；被占用时小步重试（Windows 上杀软与索引服务的句柄是毫秒级的）。"""
    for attempt in range(FS_RETRIES):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt == FS_RETRIES - 1:
                log.warning(f'[update] 删除 {path.name} 失败（重试 {FS_RETRIES} 次仍被占用）')
                return False
            time.sleep(FS_RETRY_DELAY)
        except OSError as exc:
            log.warning(f'[update] 删除 {path.name} 失败: {exc}')
            return False
    return False


def _replace_with_retry(source: Path, target: Path) -> None:
    """`os.replace` + 小步重试；重试还不行就抛 `io_error`。"""
    for attempt in range(FS_RETRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if attempt == FS_RETRIES - 1:
                raise _UpdateError(
                    'io_error', f'写入 {target.name} 失败（重试 {FS_RETRIES} 次仍被占用）'
                ) from exc
            time.sleep(FS_RETRY_DELAY)


def _cleanup_stale_parts() -> None:
    """清掉 `updates/*.part` 里超过 1 小时的残骸。

    进程被强杀时来不及删 `.part`，而它们每个都是几百 MB —— 不清理的话数据根会缓慢
    长大。只删够旧的：另一个实例正在写的 `.part` 不能被这边删掉。
    """
    try:
        folder = get_updates_dir()
        if not folder.is_dir():
            return
        deadline = time.time() - STALE_PART_SECONDS
        for path in folder.glob('*' + PART_SUFFIX):
            try:
                if path.stat().st_mtime < deadline:
                    _remove_file(path)
            except OSError:
                continue
    except OSError as exc:
        log.warning(f'[update] 清理残留下载失败: {exc}')


def _idle_job() -> Dict[str, Any]:
    return {
        'state': 'idle',
        'version': '',
        'asset': '',
        'received': 0,
        'total': 0,
        'path': '',
        'code': '',
        'message': '',
    }


def _envelope(*, form: str, current: str) -> Dict[str, Any]:
    """失败信封的完整骨架（成功时复用同一份键，只改值）。"""
    return {
        'ok': False,
        'code': 'unexpected',
        'current': current,
        'latest': None,
        'form': form,
        'download_allowed': False,
        'asset': None,
        'html_url': RELEASES_PAGE_URL,
        'retry_after_seconds': None,
        'message': '',
        'checked_at': int(time.time()),
        'cached': False,
    }


def _fail(form: str, current: str, code: str, message: str,
          *, retry_after: Optional[int] = None) -> Dict[str, Any]:
    envelope = _envelope(form=form, current=current)
    envelope['code'] = code
    envelope['message'] = message
    envelope['retry_after_seconds'] = retry_after
    return envelope


class UpdateService:
    """检查 / 下载的状态机。一个进程一个实例（见 `service()`）。"""

    def __init__(self, transport: Optional[UpdateTransport] = None) -> None:
        self.transport = transport or UpdateTransport()
        self._lock = threading.Lock()
        # 单飞锁：一次只允许一个升级动作。**2.1 的安装器启动与目录交换复用同一把**
        # —— 下载完就释放会让"下载"与"安装"同时进行，而它们都动 `updates/`。
        self._action_lock = threading.Lock()
        self._task: Optional[BackgroundTask] = None
        self._job: Dict[str, Any] = _idle_job()
        # 上一次检查结果（信封），带时间戳用于 60 秒复用。
        self._cache_envelope: Optional[Dict[str, Any]] = None
        self._cache_current = ''
        self._cache_at = 0.0
        # 上一次检查挑出的下载候选（含 URL）：`start_download` 只认这一份。
        self._candidate: Optional[Dict[str, Any]] = None
        self._ua_version = 'unknown'
        # 首次创建时清一次残骸（进程被杀留下的 .part）。
        _cleanup_stale_parts()

    # ===== A：检查 =====

    def check(self, currentVersion: object = '') -> Dict[str, Any]:
        """查最新发布并给出结论。**不抛异常**，失败也返回完整信封。"""
        form = current_form()
        current = currentVersion.strip() if isinstance(currentVersion, str) else ''
        if form in (FORM_DEV, FORM_PLATFORM):
            return _fail(form, current, 'disabled', _FORM_REASONS[form])

        parsed_current = parse_version(current)
        if parsed_current is None:
            return _fail(form, current, 'bad_version',
                         f'当前版本号无法解析: {current or "(空)"}')

        cached = self._cached(current)
        if cached is not None:
            return cached

        self._ua_version = current or 'unknown'
        envelope, candidate = self._fetch_release(form, current, parsed_current)
        with self._lock:
            self._cache_envelope = dict(envelope)
            self._cache_current = current
            self._cache_at = time.monotonic()
            self._candidate = dict(candidate) if candidate is not None else None
        return envelope

    def _cached(self, current: str) -> Optional[Dict[str, Any]]:
        """60 秒内、且当前版本没变时，直接复用上一次的信封。"""
        with self._lock:
            cached = self._cache_envelope
            same_version = self._cache_current == current
            fresh = time.monotonic() - self._cache_at < CHECK_CACHE_SECONDS
        if cached is None or not same_version or not fresh:
            return None
        envelope = dict(cached)
        envelope['cached'] = True
        asset = cached.get('asset')
        envelope['asset'] = dict(asset) if isinstance(asset, dict) else None
        log.info(f"[update] 复用 {int(CHECK_CACHE_SECONDS)} 秒内的检查结果（{cached.get('code')}）")
        return envelope

    def _new_session(self) -> requests.Session:
        """新建会话。

        **不传 `proxies`**：用户环境里的 `HTTP(S)_PROXY` 是他自己的网络选择，必须生效
        —— 与本机探活那条"显式禁用代理"的取舍相反，那条请求访问的是 127.0.0.1。
        """
        session = requests.Session()
        session.max_redirects = MAX_REDIRECTS
        session.headers['User-Agent'] = _user_agent(self._ua_version)
        return session

    def _fetch_release(self, form: str, current: str,
                       parsed_current: Tuple[int, int, int]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        url = self.transport.api_base.rstrip('/') + RELEASES_PATH
        log.info(f'[update] 检查最新发布: {_safe_url(url)}（当前 {current}）')
        try:
            with self._new_session() as session:
                response = session.get(
                    url,
                    timeout=self.transport.api_timeout,
                    headers={'Accept': 'application/vnd.github+json'},
                )
        except requests.Timeout as exc:
            # 必须先于 ConnectionError：Timeout 不是它的子类（反过来，SSLError 与
            # ProxyError 是），顺序反了会漏掉超时这一支。
            log.warning(f'[update] 检查超时: {type(exc).__name__}: {exc}')
            return _fail(form, current, 'network', f'检查失败：连不上 GitHub（{type(exc).__name__}）'), None
        except requests.ConnectionError as exc:
            log.warning(f'[update] 检查失败（连接）: {type(exc).__name__}: {exc}')
            return _fail(form, current, 'network', f'检查失败：连不上 GitHub（{type(exc).__name__}）'), None
        except requests.RequestException as exc:
            log.warning(f'[update] 检查失败: {type(exc).__name__}: {exc}')
            return _fail(form, current, 'unexpected', f'检查失败：{type(exc).__name__}'), None

        try:
            return self._interpret(form, current, parsed_current, response)
        finally:
            response.close()

    def _interpret(self, form: str, current: str, parsed_current: Tuple[int, int, int],
                   response: requests.Response) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """把一次响应翻译成信封（+ 下载候选）。"""
        status = response.status_code
        if status == 404:
            log.warning('[update] GitHub 没有返回发布信息（404）')
            return _fail(form, current, 'not_found', '没找到发布信息'), None

        remaining = (response.headers.get('X-RateLimit-Remaining') or '').strip()
        if status == 429 or (status == 403 and remaining == '0'):
            retry_after = _retry_after_seconds(response)
            wait = f'{retry_after} 秒后' if retry_after else '稍后'
            log.warning(f'[update] GitHub 限流（HTTP {status}，remaining={remaining or "-"}）')
            return _fail(form, current, 'rate_limited',
                         f'GitHub 限流，请 {wait}重试', retry_after=retry_after), None

        if status in (401, 403):
            log.warning(f'[update] GitHub 拒绝访问（HTTP {status}）')
            return _fail(form, current, 'forbidden', f'检查失败：GitHub 返回 {status}'), None

        if status != 200:
            log.warning(f'[update] GitHub 返回意外状态码 {status}')
            return _fail(form, current, 'unexpected', f'检查失败：GitHub 返回 {status}'), None

        try:
            payload = response.json()
        except ValueError as exc:
            log.warning(f'[update] 发布信息不是合法 JSON: {type(exc).__name__}: {exc}')
            return _fail(form, current, 'bad_response', '检查失败：发布信息无法解析'), None

        if not isinstance(payload, dict):
            log.warning(f'[update] 发布信息顶层应为对象，实际 {type(payload).__name__}')
            return _fail(form, current, 'bad_response', '检查失败：发布信息形状变化'), None

        tag = payload.get('tag_name')
        assets = payload.get('assets')
        if not isinstance(tag, str) or not isinstance(assets, list):
            # 只记"哪个字段、实际是什么类型"：形状变化时这是唯一能远程定位的信息。
            problems = []
            if not isinstance(tag, str):
                problems.append(f'tag_name 应为字符串，实际 {type(tag).__name__}')
            if not isinstance(assets, list):
                problems.append(f'assets 应为数组，实际 {type(assets).__name__}')
            log.warning('[update] 发布信息形状变化: ' + '；'.join(problems))
            return _fail(form, current, 'bad_response', '检查失败：发布信息形状变化'), None

        latest_text = _version_text(tag)
        parsed_latest = parse_version(tag)
        if parsed_latest is None:
            log.warning(f'[update] 远端版本号无法解析: {tag!r}')
            return _fail(form, current, 'bad_version', f'发布版本号无法解析: {tag}'), None

        html_url = payload.get('html_url')
        if not isinstance(html_url, str) or not html_url.strip():
            html_url = RELEASES_PAGE_URL

        if parsed_latest <= parsed_current:
            # 相等 = 已是最新；远端更低 = 用户在测试更新的版本，不提示"降级"。
            log.info(f'[update] 已是最新（当前 {current}，远端 {latest_text}）')
            envelope = _envelope(form=form, current=current)
            envelope.update({'ok': True, 'code': 'up_to_date', 'latest': latest_text,
                             'html_url': html_url, 'download_allowed': form in _DOWNLOAD_FORMS})
            return envelope, None

        candidate = select_asset(form, assets, latest_text)
        if candidate is None:
            log.warning(f'[update] 发布里没有形态 {form} 需要的资产（或缺少 .sha256）')
            return _fail(form, current, 'no_asset', '发布里没有对应的安装包'), None

        log.info(f'[update] 发现新版本 {latest_text}（当前 {current}，资产 {candidate["name"]}）')
        envelope = _envelope(form=form, current=current)
        envelope.update({
            'ok': True,
            'code': 'update_available',
            'latest': latest_text,
            'html_url': html_url,
            'download_allowed': form in _DOWNLOAD_FORMS,
            'asset': {'name': candidate['name'], 'size': candidate['size']},
        })
        download = {
            'version': latest_text,
            'asset': candidate['name'],
            'url': candidate['url'],
            'sha256_url': candidate['sha256_url'],
            'total': candidate['size'],
        }
        return envelope, download

    # ===== 状态 =====

    def state(self) -> Dict[str, Any]:
        """当前形态 + 下载任务快照（前端每秒轮询这一份）。"""
        form = current_form()
        with self._lock:
            job = dict(self._job)
        job['percent'] = _percent(job['received'], job['total'])
        return {
            'form': form,
            'enabled': form in (FORM_PORTABLE, FORM_INSTALLER, FORM_ZIP),
            'download_allowed': form in _DOWNLOAD_FORMS,
            'reason': _FORM_REASONS.get(form, ''),
            'job': job,
        }

    # ===== B：下载 =====

    def start_download(self, version: object = '', assetName: object = '') -> Dict[str, Any]:
        """开始下载上一次检查挑出的资产。**不抛异常**。"""
        _cleanup_stale_parts()
        form = current_form()
        if form not in _DOWNLOAD_FORMS:
            return {'started': False, 'error': 'not_allowed'}

        requested_version = version.strip() if isinstance(version, str) else ''
        requested_asset = assetName.strip() if isinstance(assetName, str) else ''

        with self._lock:
            job = dict(self._job)
            candidate = dict(self._candidate) if self._candidate is not None else None

        if (job['state'] == 'ready' and job['path']
                and job['version'] == requested_version
                and job['asset'] == requested_asset
                and Path(job['path']).is_file()):
            return {'started': False, 'ready': True, 'path': job['path']}

        if candidate is None:
            return {'started': False, 'error': 'no_result'}
        if (candidate.get('version') != requested_version
                or candidate.get('asset') != requested_asset):
            # 版本 / 资产名必须与缓存完全一致：URL 只能来自上一次检查。
            log.warning(f'[update] 下载请求与检查结果不符: {requested_version!r} / {requested_asset!r}')
            return {'started': False, 'error': 'stale'}

        total = int(candidate.get('total') or 0)
        if total > self.transport.max_bytes:
            log.warning(f'[update] 资产体积 {total} 超过上限 {self.transport.max_bytes}')
            return {'started': False, 'error': 'too_large'}

        if not self._action_lock.acquire(blocking=False):
            # 有升级动作在进行（下载，或 2.1 的安装 / 目录交换）。
            if self._running():
                return {'started': False, 'running': True}
            return {'started': False, 'error': 'busy'}

        task = BackgroundTask(kind='update-download')
        with self._lock:
            self._task = task
            self._job = {
                'state': 'downloading',
                'version': str(candidate['version']),
                'asset': str(candidate['asset']),
                'received': 0,
                'total': total,
                'path': '',
                'code': '',
                'message': '',
            }
        try:
            task.start(self._download_worker, (
                str(candidate['version']),
                str(candidate['asset']),
                str(candidate['url']),
                str(candidate['sha256_url']),
                total,
            ))
        except Exception:
            # 线程起不来时不能把手里的单飞锁带走，否则这个进程再也不能下载。
            self._release_action_lock()
            with self._lock:
                self._task = None
                self._job = _idle_job()
            raise
        log.info(f'[update] 开始下载 {candidate["asset"]}（{total} 字节）')
        return {'started': True, 'version': str(candidate['version']),
                'asset': str(candidate['asset']), 'total': total}

    def cancel(self) -> Dict[str, Any]:
        """请求取消当前下载；worker 在下一个分块处停下。"""
        with self._lock:
            task = self._task
        if task is None or not task.status()['running']:
            return {'cancelled': False, 'running': False}
        task.cancel()
        log.info('[update] 已请求取消下载')
        return {'cancelled': True, 'running': True}

    def open_updates_dir(self) -> Dict[str, Any]:
        """在文件管理器里打开 `updates/`。

        路径固定为 `get_updates_dir()`，不接受调用方传入 —— 一旦能打开任意路径，
        这个端点就成了"用受信任的壳去启动任意程序 / 目录"的入口（与
        `ShellInfo.open_log_dir` 同一取舍）。
        """
        path = get_updates_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
            opener = getattr(os, 'startfile', None)  # 仅 Windows 存在
            if opener is not None:
                opener(str(path))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except Exception as exc:
            log.error(f'[update] 打开更新目录失败: {exc}')
            return {'success': False, 'error': str(exc)}
        return {'success': True}

    # ===== worker =====

    def _download_worker(self, task: BackgroundTask, version: str, asset_name: str,
                         url: str, sha_url: str, total: int) -> None:
        """下载 + 校验。预期失败写成 `job` 的结构化结果并**正常返回**。"""
        dest_dir = get_updates_dir()
        # 半成品名带 pid：两个实例先后写同一份资产时不会互相踩到对方的临时文件。
        part_path = dest_dir / f'{asset_name}.{os.getpid()}{PART_SUFFIX}'
        final_path = dest_dir / asset_name
        try:
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                log.info(f'[update] 开始下载 {asset_name}（目标版本 {version}）')
                self._download_verified(task, url, sha_url, asset_name, part_path, final_path, total)
            finally:
                # 任何非成功路径都在**置终态之前**删掉半成品：否则"取消后目录为空"
                # 与状态查询之间会有一个可见的窗口。
                if self._job_state() != 'ready':
                    _remove_file(part_path)
        except _UpdateError as exc:
            state = 'cancelled' if exc.code == 'cancelled' else 'error'
            self._finish(state, code=exc.code, message=exc.message)
            log.warning(f'[update] 下载未完成（{exc.code}）：{exc.message}')
        except requests.RequestException as exc:
            self._finish('error', code='network', message=f'{type(exc).__name__}: {exc}')
            log.warning(f'[update] 下载失败（网络）: {type(exc).__name__}: {exc}')
        except OSError as exc:
            self._finish('error', code='io_error', message=f'{type(exc).__name__}: {exc}')
            log.warning(f'[update] 下载失败（文件）: {type(exc).__name__}: {exc}')
        except Exception as exc:
            self._finish('error', code='unexpected', message=f'{type(exc).__name__}: {exc}')
            log.exception(f'[update] 下载出现未预期异常: {type(exc).__name__}: {exc}')
        finally:
            self._release_action_lock()

    def _download_verified(self, task: BackgroundTask, url: str, sha_url: str,
                           asset_name: str, part_path: Path, final_path: Path,
                           total: int) -> None:
        """真正下载并校验；任何预期失败都抛 `_UpdateError`。"""
        with self._new_session() as session:
            digest = self._fetch_sha256(session, sha_url, asset_name)
            log.info(f'[update] 下载资产: {_safe_url(url)}')
            response = self._open(session, url, self.transport.download_timeout, stream=True)
            try:
                expected_total = total
                header = (response.headers.get('Content-Length') or '').strip()
                if header.isdigit():
                    content_length = int(header)
                    if content_length > self.transport.max_bytes:
                        raise _UpdateError(
                            'too_large',
                            f'资产 {content_length} 字节，超过上限 {self.transport.max_bytes}',
                        )
                    expected_total = content_length
                task.update(total=expected_total, processed=0, current='downloading')
                self._set_job(total=expected_total, received=0, state='downloading')

                received = 0
                hasher = hashlib.sha256()
                started = time.monotonic()
                with open(part_path, 'wb') as handle:
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                        if not chunk:
                            continue
                        if task.cancelled:
                            raise _UpdateError('cancelled', '已取消下载')
                        received += len(chunk)
                        if received > self.transport.max_bytes:
                            raise _UpdateError(
                                'too_large',
                                f'下载超过上限 {self.transport.max_bytes} 字节，已中止',
                            )
                        if time.monotonic() - started > DOWNLOAD_WALL_SECONDS:
                            raise _UpdateError(
                                'timeout',
                                f'下载超过 {int(DOWNLOAD_WALL_SECONDS)} 秒仍未完成，已中止',
                            )
                        hasher.update(chunk)
                        handle.write(chunk)
                        task.update(processed=received)
                        self._set_job(received=received)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                response.close()

            # 摘要计算放在"下载"之外：界面从这一刻起显示"正在校验"。
            task.update(current='verifying')
            self._set_job(state='verifying')
            actual = hasher.hexdigest()
            if actual != digest:
                raise _UpdateError('digest_mismatch', '下载内容的 SHA-256 与发布摘要不一致')

            _replace_with_retry(part_path, final_path)
            self._finish_ready(final_path, received)
            task.update(processed=received, total=received)
            log.info(f'[update] 下载完成并通过校验: {asset_name}（{received} 字节）')

    def _fetch_sha256(self, session: requests.Session, sha_url: str, asset_name: str) -> str:
        """取 `.sha256` 并解析出摘要；形状错或文件名不符归 `bad_response`。"""
        response = self._open(session, sha_url, self.transport.download_timeout, stream=True)
        try:
            raw = b''
            for chunk in response.iter_content(chunk_size=512):
                if not chunk:
                    continue
                raw += chunk[:MAX_SHA256_BYTES + 1 - len(raw)]
                if len(raw) > MAX_SHA256_BYTES:
                    break
        finally:
            response.close()
        if len(raw) > MAX_SHA256_BYTES:
            raise _UpdateError('bad_response',
                               f'摘要文件超过 {MAX_SHA256_BYTES} 字节，不是摘要文件')
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise _UpdateError('bad_response', '摘要文件不是 UTF-8 文本') from exc
        parsed = parse_sha256(text)
        if parsed is None:
            raise _UpdateError('bad_response', '摘要文件格式不符（应为 64 位十六进制摘要 + 文件名）')
        digest, name = parsed
        if name != asset_name:
            raise _UpdateError('bad_response',
                               f'摘要里的文件名与资产名不符: {name!r} != {asset_name!r}')
        return digest

    def _open(self, session: requests.Session, url: str,
              timeout: Tuple[float, float], *, stream: bool) -> requests.Response:
        """带**手动逐跳校验**的 GET，返回 200 的响应。

        为什么不用 `allow_redirects=True`：自动跟随对 `file://` 抛 `InvalidSchema`
        —— 它不是 `ConnectionError` 的子类，会被分类成"意外异常"而不是"重定向可疑"；
        而且自动跟随不给逐跳检查主机名的机会，资产重定向到别家 CDN 就悄悄发生了。
        """
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            problem = _url_problem(current, self.transport)
            if problem:
                raise _UpdateError('bad_redirect', f'重定向地址被拒绝（{problem}）')
            try:
                response = session.get(current, timeout=timeout, stream=stream,
                                       allow_redirects=False)
            except requests.Timeout as exc:
                raise _UpdateError('network', f'下载超时（{type(exc).__name__}）') from exc
            except requests.ConnectionError as exc:
                raise _UpdateError('network', f'下载时连不上服务器（{type(exc).__name__}）') from exc
            except requests.RequestException as exc:
                raise _UpdateError('unexpected', f'{type(exc).__name__}: {exc}') from exc

            if response.status_code in (301, 302, 303, 307, 308):
                location = (response.headers.get('Location') or '').strip()
                response.close()
                if not location:
                    raise _UpdateError('bad_redirect', '重定向缺少 Location')
                current = urljoin(current, location)
                log.info(f'[update] 重定向到 {_safe_url(current)}')
                continue
            if response.status_code != 200:
                status = response.status_code
                response.close()
                code = 'not_found' if status == 404 else 'bad_response'
                raise _UpdateError(code, f'下载失败：HTTP {status}')
            return response
        raise _UpdateError('bad_redirect', f'重定向超过 {MAX_REDIRECTS} 跳')

    # ===== 任务状态小工具 =====

    def _running(self) -> bool:
        with self._lock:
            task = self._task
        return bool(task is not None and task.status()['running'])

    def _job_state(self) -> str:
        with self._lock:
            return str(self._job['state'])

    def _set_job(self, *, received: Optional[int] = None, total: Optional[int] = None,
                 state: Optional[str] = None) -> None:
        with self._lock:
            if state is not None:
                self._job['state'] = state
            if received is not None:
                self._job['received'] = int(received)
            if total is not None:
                self._job['total'] = int(total)

    def _finish(self, state: str, *, code: str = '', message: str = '', path: str = '') -> None:
        with self._lock:
            self._job['state'] = state
            self._job['code'] = code
            self._job['message'] = message
            self._job['path'] = path

    def _finish_ready(self, path: Path, received: int) -> None:
        with self._lock:
            self._job['state'] = 'ready'
            self._job['received'] = int(received)
            self._job['total'] = int(received)
            self._job['path'] = str(path)
            self._job['code'] = ''
            self._job['message'] = ''

    def _release_action_lock(self) -> None:
        try:
            self._action_lock.release()
        except RuntimeError:
            # 没持有就释放：worker 的收尾路径可能被走到两次，不该抛给 `BackgroundTask`。
            pass


# ===== 单例与方法表 =====

_service: Optional[UpdateService] = None
_service_lock = threading.Lock()


def service() -> UpdateService:
    """进程内唯一的 `UpdateService`（首次创建时顺手清一次残留 `.part`）。"""
    global _service
    with _service_lock:
        if _service is None:
            _service = UpdateService()
        return _service


def reset_for_tests() -> None:
    """丢掉单例与它持有的状态（仅供测试）。"""
    global _service
    with _service_lock:
        _service = None


def api_methods() -> Dict[str, Callable]:
    """对外方法表：**唯一来源**，HTTP 与桌面 js_api 两处都 `update` 这一份。

    与 `plugin_manager.freshness_api_methods()` 同一理由：两条通道各手抄一份名字，
    迟早漏一处，而漏了不会报错 —— 只表现为"某个模式下点按钮没反应"。
    """
    instance = service()
    return {
        'system_check_update': instance.check,
        'system_start_update_download': instance.start_download,
        'system_get_update_state': instance.state,
        'system_cancel_update_download': instance.cancel,
        'system_open_update_dir': instance.open_updates_dir,
    }
