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

OmniBox 的**主体（principal）上下文**：把"请求带来的凭据"映射成"谁在调用"。

## 为什么需要它

壳原先只有一枚**进程级全局令牌**（`auth_token.txt`）：它回答"这个请求带没带对的
令牌"，但答不出"它是谁"。于是插件只能退化为"本机身份即操作者"——任何持令牌者
都被当成同一个人（设计文档 `docs/group-mesh-design.md` §12 第 1/2 项）。

本模块提供两件事：

1. **凭据到主体的映射**：`principals.json` 里每个主体有自己的令牌（只存
   SHA-256，不存明文），查表得到主体身份与角色；
2. **受信注入**：`current_principal()` 读的是**当前请求上下文**里的主体，
   由壳在鉴权通过后设置，插件无法通过请求参数影响它。

## 三条硬约束

* **只从凭据取主体**。请求体 / query 里的 `principal`、`principal_id` 一类字段
  一律不读 —— 否则"伪造 principal 让插件以为自己是别人"就是一行 JSON 的事。
* **后台线程没有主体**。`ContextVar` 不跨线程继承，插件自起的线程读到的永远是
  `None`（设计文档 §12 明确要求"涉及主体的后台任务必须显式携带主体信息"）。
  这个 `None` 是**有意义的失败态**，不是"等于本机主体"。
* **未登记的令牌不放行**，也不降级成匿名主体：鉴权失败就是 401。

## 与"本机身份"的区别

本机身份（group-mesh 的 `group-mesh/backend/main.py` 读的
`identity/principal.json`）是**这台机器是谁**；主体上下文是**这次调用是谁**。
两者可以指向同一个主体（`public_key` 字段就是给这种对齐用的），但语义不同：
局域网/反向代理部署下，多个使用者连的是同一个壳进程，这时只有主体上下文能区分他们。
'''

from __future__ import annotations

import contextvars
import functools
import hashlib
import hmac
import json
import logging
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# 主体存储文件名（与 auth_token.txt 同级、同权限）
PRINCIPALS_FILE_NAME = 'principals.json'

# 角色。语义与团体名单（`shell/groupmesh/roster.py`）保持一致，便于以后把
# 本机主体与名单里的主体对齐：
#   owner  群主/拥有者，可改设置、可管理其他主体
#   admin  管理员，可改设置
#   member 普通成员，只能用数据面能力
ROLE_OWNER = 'owner'
ROLE_ADMIN = 'admin'
ROLE_MEMBER = 'member'
ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER)

# 凭据来源。出现在日志与界面上，用于回答"这个主体是怎么来的"：
#   auth-token  由**既有的**全局令牌自动登记（向后兼容：老部署的令牌继续可用）
#   enrolled    在界面上登记产生
#   imported    从插件身份（如 group-mesh 的主体）导入
SOURCE_AUTH_TOKEN = 'auth-token'
SOURCE_ENROLLED = 'enrolled'
SOURCE_IMPORTED = 'imported'

# 判定角色是否具备"管理"能力（改设置、管主体）
ADMIN_ROLES = (ROLE_OWNER, ROLE_ADMIN)


@dataclass(frozen=True)
class PrincipalContext:
    """一次调用的主体。**不含凭据**——凭据不该离开存储层。"""

    id: str
    name: str
    role: str
    source: str
    # 可选：与插件身份对齐用的 Ed25519 公钥（base64 文本，便于落盘与比对）
    public_key: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES

    def to_dict(self) -> Dict[str, Any]:
        return {'id': self.id, 'name': self.name, 'role': self.role,
                'source': self.source, 'public_key': self.public_key}


# 当前请求的主体。默认 None = "没有主体"（后台线程、CLI、自检都读到这里）。
#
# 为什么用 ContextVar 而不是 threading.local：Flask 的请求可能被 URL 映射或
# 异步扩展放到不同线程上执行，而 ContextVar 跟随的是**执行上下文**；
# 同时它天然不跨线程继承，恰好就是我们要的"后台线程没有主体"。
CURRENT_PRINCIPAL: contextvars.ContextVar[Optional[PrincipalContext]] = \
    contextvars.ContextVar('omnibox_principal', default=None)


def current_principal() -> Optional[PrincipalContext]:
    """当前执行上下文里的主体；没有则返回 `None`（**不抛异常**）。

    返回 `None` 而不是抛异常的理由：CLI、自检、插件后台线程都需要"没有主体"
    这个正常状态。需要强制有主体的地方请显式判空或调 `require_principal()`。
    """
    return CURRENT_PRINCIPAL.get()


def require_principal() -> PrincipalContext:
    """当前主体必须存在，否则抛 `PermissionError`。

    给"没有主体就不能做"的操作（改设置、读他人共享缓存）用：让拒绝发生在
    业务逻辑之前，而不是让 `None` 一路传下去被某处 `or '本机'` 兜住。
    """
    principal = CURRENT_PRINCIPAL.get()
    if principal is None:
        raise PermissionError('该操作需要已认证的主体（当前上下文没有主体：'
                              '可能是后台线程或未经壳注入的直接调用）')
    return principal


class AdminRequired(PermissionError):
    """当前主体存在但不是管理员，而该操作要求管理员。

    单独一个类型（而不是直接抛 `PermissionError`）是为了让调用方能把它翻译成
    **403**：在壳的 HTTP 路径上"令牌有效但角色不够"与"没有令牌"必须是两个状态码，
    401 会把使用者踢回登录流程（见 `file_server.api_proxy`）。
    """


def require_admin(fn: Callable) -> Callable:
    """包一层：当前主体必须是管理员，否则抛 `AdminRequired`。

    为什么需要这个包装，而不是把 `if not principal.is_admin` 抄在每个端点里：
    同一批壳级端点在**两条通道**上暴露 —— HTTP 的 `/api/<方法>`（file_server）与
    桌面模式的 pywebview `js_api`（main.py）。判定原先只写在 HTTP 路径里，桌面模式
    因此完全不判角色：插件 iframe 经 `Bridge.callSystem` 沿 parent 链取到
    `pywebview.api` 就能直接调用 `system_get_config` 一类端点（审计项 P1-8 / P2-3）。
    判定下沉到方法本身、由两条通道共用同一份包装，就不会再出现"加了一处、漏了另一处"。
    """
    @functools.wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        principal = CURRENT_PRINCIPAL.get()
        if principal is None or not principal.is_admin:
            who = principal.name if principal is not None else '（当前上下文没有主体）'
            raise AdminRequired(f'该操作需要管理员权限（当前主体：{who}）')
        return fn(*args, **kwargs)

    return guarded


class PrincipalScope:
    """`with use_principal(p):` —— 显式设置主体，退出时恢复原值。

    给两类调用方用：
      * 壳的 `before_request`（每次请求现设，请求结束自动恢复）；
      * 用例（在不发请求的前提下模拟"某个主体在调用"）。
    """

    __slots__ = ('_principal', '_token')

    def __init__(self, principal: Optional[PrincipalContext]) -> None:
        self._principal = principal
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> Optional[PrincipalContext]:
        self._token = CURRENT_PRINCIPAL.set(self._principal)
        return self._principal

    def __exit__(self, *exc_info: Any) -> None:
        if self._token is not None:
            CURRENT_PRINCIPAL.reset(self._token)
            self._token = None


def use_principal(principal: Optional[PrincipalContext]) -> PrincipalScope:
    """`with use_principal(p):` 的构造入口（可读性用，等价于 `PrincipalScope(p)`）。"""
    return PrincipalScope(principal)


def hash_secret(secret: str) -> str:
    """把令牌压成可落盘的摘要。

    用 SHA-256 而不是口令 KDF：令牌是 `secrets.token_urlsafe(32)` 级别的高熵
    随机串，不存在字典攻击面，而 KDF 会让每个请求都多花几十毫秒。
    """
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def _secret_matches(supplied_hash: str, stored_hash: str) -> bool:
    """恒定时间比较两个十六进制摘要（避免按前缀命中的时序侧信道）。"""
    if not supplied_hash or not stored_hash:
        return False
    try:
        return hmac.compare_digest(supplied_hash, stored_hash)
    except (TypeError, ValueError):
        return False


@dataclass
class PrincipalRecord:
    """落盘的一条主体记录。凭据以摘要形式保存。"""

    id: str
    name: str
    role: str
    source: str
    secret_sha256: str
    created_at: int = 0
    public_key: Optional[str] = None

    def context(self) -> PrincipalContext:
        return PrincipalContext(id=self.id, name=self.name, role=self.role,
                                source=self.source, public_key=self.public_key)

    def to_dict(self) -> Dict[str, Any]:
        return {'id': self.id, 'name': self.name, 'role': self.role,
                'source': self.source, 'secret_sha256': self.secret_sha256,
                'created_at': self.created_at, 'public_key': self.public_key}


def _record_from_dict(data: Any) -> Optional[PrincipalRecord]:
    """解析一条记录；形状不对返回 None（**单条坏记录不让整份凭据表作废**）。

    这一点是刻意的：凭据表损坏的后果是"谁都进不来"，而它同时是管理员唯一的
    入口 —— 一旦因为一条脏记录整体抛错，用户就只能手工改文件。
    """
    if not isinstance(data, dict):
        return None
    identifier = str(data.get('id') or '').strip()
    secret = str(data.get('secret_sha256') or '').strip()
    if not identifier or len(secret) != 64:
        return None
    role = str(data.get('role') or ROLE_MEMBER).strip().lower()
    if role not in ROLES:
        role = ROLE_MEMBER
    created = data.get('created_at')
    public_key = data.get('public_key')
    return PrincipalRecord(
        id=identifier,
        name=str(data.get('name') or identifier),
        role=role,
        source=str(data.get('source') or SOURCE_ENROLLED),
        secret_sha256=secret.lower(),
        created_at=created if isinstance(created, int) and not isinstance(created, bool) else 0,
        public_key=str(public_key) if isinstance(public_key, str) and public_key else None,
    )


class PrincipalStore:
    """`<config>/principals.json` 的读写与令牌查表。

    ## 向后兼容（重要）

    老部署只有 `auth_token.txt`。构造时传入 `bootstrap_token`，若表里还没有
    任何记录，就把这枚**既有令牌**登记成 `owner` 主体（`source=auth-token`）。
    这样升级不会把用户锁在门外，而"令牌→主体"这条链从第一天就是通的。
    已经登记过就不再重复写入（保留它可能是被改过的角色）。
    """

    def __init__(self, config_dir: Path, bootstrap_token: str = '',
                 file_name: str = PRINCIPALS_FILE_NAME) -> None:
        self.path = Path(config_dir) / file_name
        self._records: List[PrincipalRecord] = []
        self._mtime_ns: Optional[int] = None
        self._size: Optional[int] = None
        self._loaded = False
        if bootstrap_token:
            self._bootstrap_token = bootstrap_token
        else:
            self._bootstrap_token = ''

    # ── 读盘 ──────────────────────────────────────────────────────────────

    def _stamp(self) -> Optional[tuple]:
        """文件指纹：`(mtime_ns, size)`；读不到（不存在）返回 `None`。

        只看 mtime 不够：文件系统的时间戳粒度可能比"两次写入的间隔"还粗
        （Windows 上的实测表现是连续两次 `os.replace` 拿到**同一个** `mtime_ns`），
        那样"外部改了凭据表"会被判成没变、撤销的令牌继续有效。大小是独立的正交
        信号，两者合起来在没有内容哈希的代价下把这类漏检堵掉。文件很小且这是
        每次请求一次 `stat()`，不值得为它读内容算哈希。
        """
        try:
            stat_result = self.path.stat()
        except OSError:
            return None
        return (stat_result.st_mtime_ns, stat_result.st_size)

    def _load(self, force: bool = False) -> None:
        """按需重读。

        每次请求都看一次指纹：`principals.json` 是**凭据**表，改完必须立即
        生效（不然"撤销某个主体的令牌"要等重启才生效）。文件不存在时视为空表，
        不报错（全新安装就是这种情况）。
        """
        stamp = self._stamp()
        if stamp is None:
            self._records = []
            self._mtime_ns = None
            self._size = None
            self._loaded = True
            return
        if not force and self._loaded and stamp == (self._mtime_ns, self._size):
            return
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            raw_records = payload.get('principals') if isinstance(payload, dict) else None
            if not isinstance(raw_records, list):
                raw_records = []
        except (OSError, ValueError) as exc:
            # 读坏了不能让所有请求 500：保留上一次成功读到的表（没有则空表），
            # 并明确记 warning —— 静默当成空表会表现成"所有人都被踢出去"。
            log.warning(f'[Principal] {self.path} 无法解析，沿用上一次成功读取的结果: {exc}')
            self._loaded = True
            return
        records = [r for r in (_record_from_dict(item) for item in raw_records) if r is not None]
        if len(records) != len(raw_records):
            log.warning(f'[Principal] {self.path} 中有 {len(raw_records) - len(records)} '
                        f'条记录形状非法，已忽略')
        self._records = records
        self._mtime_ns, self._size = stamp
        self._loaded = True

    def reload(self) -> None:
        """强制重读（用例与"改完立即生效"的入口用）。"""
        self._load(force=True)

    def records(self) -> List[PrincipalRecord]:
        self._load()
        return list(self._records)

    def contexts(self) -> List[PrincipalContext]:
        """对外可展示的主体列表（**不含凭据摘要**）。"""
        return [record.context() for record in self.records()]

    # ── 查表 ──────────────────────────────────────────────────────────────

    def resolve(self, token: str) -> Optional[PrincipalContext]:
        """按凭据解析主体；未登记返回 `None`。

        比较的是**摘要**，且逐个用 `hmac.compare_digest`：主体数量是个位数，
        线性扫描的代价可以忽略，换来的是不需要为"哈希表按 key 命中"担心
        时序差异。
        """
        if not token:
            return None
        self._load()
        supplied = hash_secret(token)
        for record in self._records:
            if _secret_matches(supplied, record.secret_sha256):
                return record.context()
        return None

    def get(self, principal_id: str) -> Optional[PrincipalContext]:
        self._load()
        for record in self._records:
            if record.id == principal_id:
                return record.context()
        return None

    # ── 写入 ──────────────────────────────────────────────────────────────

    def _write(self, records: List[PrincipalRecord]) -> None:
        """原子写：先写临时文件再 `os.replace`，与内核注册表同一套做法。

        凭据表被写坏 = 所有主体都进不来，因此不做"直接覆盖整文件"。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'principals': [r.to_dict() for r in records]}
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
                       encoding='utf-8')
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows 上 chmod 语义受限（真正生效的是目录 ACL），不阻断写入
            pass
        os.replace(tmp, self.path)
        self._records = list(records)
        stamp = self._stamp()
        if stamp is None:
            self._mtime_ns = None
            self._size = None
        else:
            self._mtime_ns, self._size = stamp
        self._loaded = True

    def add(self, principal_id: str, token: str, role: str = ROLE_MEMBER,
            name: str = '', source: str = SOURCE_ENROLLED,
            public_key: Optional[str] = None) -> PrincipalContext:
        """登记一个主体。`principal_id` 已存在时**覆盖**其凭据与角色。"""
        identifier = str(principal_id or '').strip()
        if not identifier:
            raise ValueError('主体 id 不得为空')
        if not token:
            raise ValueError('凭据不得为空')
        normalized_role = str(role or ROLE_MEMBER).strip().lower()
        if normalized_role not in ROLES:
            raise ValueError(f'未知角色 {role!r}（可选：{", ".join(ROLES)}）')
        self._load()
        record = PrincipalRecord(id=identifier, name=name or identifier,
                                 role=normalized_role, source=source,
                                 secret_sha256=hash_secret(token),
                                 created_at=int(time.time()), public_key=public_key)
        records = [r for r in self._records if r.id != identifier]
        records.append(record)
        self._write(records)
        return record.context()

    def remove(self, principal_id: str) -> bool:
        """移除一个主体。返回是否确有删除（不存在的 id 返回 False）。"""
        self._load()
        records = [r for r in self._records if r.id != principal_id]
        if len(records) == len(self._records):
            return False
        self._write(records)
        return True

    # ── 自举 ──────────────────────────────────────────────────────────────

    def ensure_bootstrap(self) -> Optional[PrincipalContext]:
        """表为空时把既有全局令牌登记成 `owner`。返回登记出的主体。

        只在**一条记录都没有**时动手：已有主体时再自动登记就等于每次启动都
        重置管理员凭据，用户改过的角色会被悄悄覆盖。
        """
        if not self._bootstrap_token:
            return None
        self._load()
        if self._records:
            return None
        log.info('[Principal] 凭据表为空，把既有访问令牌登记为本机 owner 主体')
        return self.add('owner', self._bootstrap_token, role=ROLE_OWNER,
                        name='本机', source=SOURCE_AUTH_TOKEN)


def principals_file(config_dir: Path) -> Path:
    """主体凭据表路径：`<config_dir>/principals.json`。"""
    return Path(config_dir) / PRINCIPALS_FILE_NAME
