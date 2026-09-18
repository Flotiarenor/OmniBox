"""身份与团体名单（GroupMeshPlugin 的一个 mixin 分片）。

方法从 main.py 逐字搬来，状态仍由 GroupMeshPlugin.__init__ 持有 —— 分片只把方法
挂到同一个类上，因此方法与调用点都没有变。
"""

from __future__ import annotations

import logging
import socket
from typing import Any, Dict

from shell.backend.plugin_utils import load_sibling
from shell.groupmesh.identity import Identity
from shell.groupmesh.records import RecordError
from shell.groupmesh.roster import Roster, RosterEntry, founding_roster, next_roster

log = logging.getLogger(__name__)

_common = load_sibling(__file__, 'common', 'group_mesh')
_opts = _common._opts

class GroupMixin:
    """身份初始化、团体创建 / 加入、邀请串与成员增减。"""

    # ── 身份与团体 ────────────────────────────────────────────────────────

    # 下面这些公开 API 统一接受 `(opts: dict)` 或逐个位置参数两种调用形态，
    # 见 `_opts()` 的说明。

    def init_identity(self, opts: Any = None, name: str = '') -> Dict[str, Any]:
        """创建本机身份（主体 + 首台设备）。身份已存在时拒绝覆盖。"""
        options = _opts(opts)
        principal_name = (options.get('name') or name or self.setting('principal_name') or
                          socket.gethostname() or 'omnibox')
        try:
            identity = Identity.init(self.identity_dir, principal_name, socket.gethostname())
        except RecordError as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'identity': {'name': identity.principal.name,
                                              'id': identity.principal.id,
                                              'device_id': identity.device.id}}

    def create_group(self, opts: Any = None, group: str = '') -> Dict[str, Any]:
        """创建团体：调用方成为群主，创始名单里只有自己的一台设备。"""
        options = _opts(opts)
        identity = self._load_identity()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份，请先初始化身份'}
        if self._load_roster() is not None:
            return {'success': False,
                    'error': '本机已有团体名单。删除 roster.json 才能重建（会丢掉当前名单链）'}
        group_name = options.get('group') or group or self.setting('group_name') or 'omnibox-group'
        try:
            roster = founding_roster(group_name, identity.principal,
                                     [identity.device.public_key],
                                     ttl_seconds=int(self.setting('ttl_days', 180)) * 86400)
        except RecordError as e:
            return {'success': False, 'error': str(e)}

        # 走 `_save_roster` 而不是直接写文件：它同时把名单并入历史 ——
        # 少了那一步，本机就"不认得"自己签发过的旧版本，落后多版的设备会被准入
        # 判定挡住（见 roster_history.py）。
        self._save_roster(roster)
        return {'success': True, 'group': roster.group, 'version': roster.version,
                'invite': self._invite_string(roster)}

    def join_group(self, opts: Any = None, invite: str = '') -> Dict[str, Any]:
        """用带外收到的邀请串加入团体，**或把本机名单更新到更高版本**。

        两种情形走同一条路径，因为验证规则是同一套（§5.2）：

        * 本机没有名单 → 规则 6：直接信任带外分发的创始名单；
        * 本机已有名单 → 规则 1/2 要求 `version` 前进且 `prev` 指向当前名单哈希，
          因此**只有直接后继会被接受**（乱序或伪造的名单会被拒）。

        为什么必须有"更新"这条路：群主加人后签发的是新名单，而名单靠**带外分发**
        （邀请串）。如果成员端只有一个"加入"入口、加入后再无入口，成员就会永远停在
        旧名单上 —— 表现是"我已被加进名单却连不上任何人"，且各方看到的成员表不一致。
        """
        import base64
        import json as jsonlib

        options = _opts(opts)
        if self._load_identity() is None:
            return {'success': False, 'error': '尚未创建身份，请先初始化身份'}
        text = str(options.get('invite') or invite or '').strip()
        if not text.startswith('gm1:'):
            return {'success': False, 'error': '邀请串必须以 gm1: 开头'}
        try:
            payload = base64.urlsafe_b64decode(text[4:].encode('ascii'))
            roster = Roster.from_dict(jsonlib.loads(payload.decode('utf-8')))
        except Exception as e:
            return {'success': False, 'error': f'邀请串解析失败: {e}'}

        current = self._load_roster()
        try:
            roster.accepts(current)
        except RecordError as e:
            hint = ''
            if current is not None and roster.version <= current.version:
                hint = f'（本机已是 v{current.version}，拿到的邀请串是 v{roster.version}）'
            return {'success': False, 'error': f'名单未被接受: {e}{hint}'}

        previous_version = current.version if current is not None else None
        # 走 `_save_roster`：同时并入名单历史（每收到新版本都留下，
        # 否则本机不认得自己曾经持有的旧版本）
        self._save_roster(roster)
        identity = self._load_identity()
        entry = roster.entry_of(identity.principal.public_key)
        return {'success': True,
                'updated': previous_version is not None,
                'previous_version': previous_version,
                'group': roster.group,
                'version': roster.version,
                'role': roster.role_of(identity.principal.public_key),
                'in_roster': entry is not None,
                'warning': None if entry is not None else
                           '本主体还不在名单里，其他成员不会接受你的连接。'
                           '请把设备公钥交给群主执行 roster add。'}

    def _invite_string(self, roster) -> str:
        import base64
        import json as jsonlib

        raw = jsonlib.dumps(roster.to_dict(), separators=(',', ':'), sort_keys=True).encode('utf-8')
        return 'gm1:' + base64.urlsafe_b64encode(raw).decode('ascii')

    def get_invite(self) -> Dict[str, Any]:
        """导出当前名单的邀请串（群主分发给成员用）。"""
        roster = self._load_roster()
        if roster is None:
            return {'success': False, 'error': '本机没有团体名单'}
        return {'success': True, 'invite': self._invite_string(roster),
                'group': roster.group, 'version': roster.version}

    def get_device_keys(self) -> Dict[str, Any]:
        """给群主登记用的本机公钥（主体 + 设备）。"""
        identity = self._load_identity()
        if identity is None:
            return {'success': False, 'error': '尚未创建身份'}
        import base64
        return {'success': True,
                'principal': base64.b64encode(identity.principal.public_key).decode('ascii'),
                'device': base64.b64encode(identity.device.public_key).decode('ascii'),
                'name': identity.principal.name}

    def add_member(self, opts: Any = None, principal: str = '', device: str = '',
                   name: str = '') -> Dict[str, Any]:
        """群主/管理员把一名主体（及其设备）加入名单。

        权限由内核的 `Roster.accepts()` 在签名后复核：管理员改群主或改管理员
        集合都会被拒绝，因此这里不需要（也不应该）在插件层重写一套角色判断。
        """
        import base64
        options = _opts(opts)
        principal = str(options.get('principal') or principal or '')
        device = str(options.get('device') or device or '')
        name = str(options.get('name') or name or '')
        identity = self._load_identity()
        roster = self._load_roster()
        if identity is None or roster is None:
            return {'success': False, 'error': '需要先创建身份与团体'}

        def decode(value: str, field: str) -> bytes:
            raw = base64.b64decode((value or '').strip(), validate=True)
            if len(raw) != 32:
                raise ValueError(f'{field} 必须是 32 字节公钥的 base64')
            return raw

        try:
            principal_key = decode(principal, '主体公钥')
            device_keys = [decode(device, '设备公钥')] if device.strip() else []
        except Exception as e:
            return {'success': False, 'error': f'公钥解析失败: {e}'}

        members = [RosterEntry(m.name, m.principal_key, list(m.device_keys))
                   for m in roster.members]
        existing = next((m for m in members if m.principal_key == principal_key), None)
        if existing is not None:
            added = [k for k in device_keys if k not in existing.device_keys]
            if not added:
                return {'success': False, 'error': '该主体与其设备都已在名单里'}
            existing.device_keys.extend(added)
            action = f'补登记 {len(added)} 台设备'
        else:
            members.append(RosterEntry(name or principal_key[:8].hex(), principal_key, device_keys))
            action = '新增成员'

        candidate = next_roster(roster, group=roster.group, owner_key=roster.owner_key,
                                members=members, admin_keys=list(roster.admin_keys),
                                ttl_seconds=int(self.setting('ttl_days', 180)) * 86400)
        candidate.sign(identity.principal.private_key)
        try:
            candidate.accepts(roster)
        except RecordError as e:
            return {'success': False, 'error': f'你的角色无权做这次改动: {e}'}

        # 走 `_save_roster` 而不是直接写文件：它同时把新名单并入历史，
        # 本机才"认得"自己签发过的每一版（落后多版的设备靠它通过准入判定）
        self._save_roster(candidate)
        return {'success': True, 'action': action, 'version': candidate.version,
                'invite': self._invite_string(candidate)}
