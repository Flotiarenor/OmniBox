"""本机路径与落盘数据。

状态自持：数据根、派生路径、名单历史缓存、以及"名单写回后要唤醒后台同步"这一个
回调。身份 / 名单 / 共享项声明的读写全在这里 —— 别的组件只问"要什么"，不自己拼
路径，也不自己 `json.loads`。

为什么唤醒后台同步的回调要在构造时注入：名单写回有多个入口（创建团体、加入、加成员、
对端推来新名单、节点侧的 `roster_saver`），少一处就会退化成"改完名单要靠下一轮轮询
才推给对端"。回调由入口编排，本类不依赖任何上层组件。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.ports import SettingsPort
from shell.groupmesh import roster_history as roster_history_mod
from shell.groupmesh.identity import Identity
from shell.groupmesh.records import RecordError
from shell.groupmesh.roster import Roster
from shell.groupmesh.shares import LocalShare

log = logging.getLogger(__name__)


class Storage:
    def __init__(self, root: Path, settings: SettingsPort, pulse: Any) -> None:
        self.root = root
        self._settings = settings
        self._pulse = pulse
        # 名单历史（`roster-history.json`）：准入判定要回答"本机认不认得对端手里
        # 那份"，只有当前名单说明不了"见过它前面的那些"（见 roster_history.py）。
        # 惰性构造：不是每个实例都会跑节点。
        self._roster_history_store: Optional[roster_history_mod.RosterHistory] = None

    # ── 路径 ──────────────────────────────────────────────────────────────

    @property
    def identity_dir(self) -> Path:
        return self.root / 'identity'

    @property
    def cache_dir(self) -> Path:
        """可重建的派生数据（目录快照、连接待用）。**不是**缩略图目录。"""
        return self.root / '.cache'

    @property
    def downloads_dir(self) -> Path:
        """从对端取回的文件落点：设置项 `download_dir`，留空时用数据根下的 downloads。

        刻意与 `.cache` 分开：`.cache` 可以随时删（删了只是重新拉），而下载目录里
        放的是用户明确要回来的文件，删掉就是数据丢失。
        """
        configured = str(self._settings.setting('download_dir', '') or '').strip()
        if configured:
            return Path(configured).expanduser().resolve()
        return self.root / 'downloads'

    @property
    def remote_cache_dir(self) -> Path:
        """远端内容的本地物化根：`<cache>/remote/<设备ID>/<共享标识>/<相对路径>`。

        目录结构与文件字节分开落地：目录树来自 `list_directory`（很轻），
        文件字节按需取回（`download_remote`），因此不会为了"看一眼"而整份同步。
        """
        return self.cache_dir / 'remote'

    @property
    def staging_dir(self) -> Path:
        """按需取字节的暂存根：`<cache>/staging/<设备ID>/<共享标识>/<相对路径>`。

        刻意与 `remote_cache_dir` **平级**而不是放它里面：物化根下的路径会被
        `is_content_placeholder()` / `ensure_file()` 按 `<设备ID>/<共享标识>/…`
        反解，暂存文件混进去就会被当成一条物化条目来查索引（`.tmp` 会被当成设备 ID）。

        为什么不用用户下载目录（改动前的做法）：那是"用户明确要回来的文件"，
        浏览一张图就顺手往里扔一份，既污染用户目录，又让 `os.replace` 依赖
        "下载目录与数据根同卷"这个默认配置（跨卷会抛 OSError）。暂存根与被替换目标同在
        `.cache` 下，同卷由构造保证。
        """
        return self.cache_dir / 'staging'

    def protected_paths(self, base: List[Path]) -> List[Path]:
        """在壳给出的受保护清单之后追加上身份目录。

        身份目录整个申报为受保护：里面是主体的长期私钥与设备私钥。设备私钥不导出：
        私钥内容由 `secret_store` 保护（Windows DPAPI / 桌面 keyring /
        `OMNIBOX_SECRET_KEY` 口令），这里再申报为受保护路径，保证它不会经 `/file`、
        `/files`、`/thumbs` 被端出去 —— `plain` 回落时这条是唯一防线，因此不能省。
        """
        return [*base, self.identity_dir]

    # ── 用量 ──────────────────────────────────────────────────────────────

    @staticmethod
    def tree_usage(root: Path, limit: Optional[int] = None,
                   max_entries: int = 200_000) -> Tuple[int, bool]:
        """目录树总字节数。`limit` 非空时与它取小 —— 只关心"有没有超配额"。

        返回 `(字节数, 是否被上限截断)`：截断表示这个数字是下界而不是精确值，
        界面必须据此显示"≥"，否则用户会以为配额算错了。

        **两个提前返回都算截断**：命中 `limit` 时停下同样没走完整棵树，数字只是
        下界 —— 原先这里返回 `False`，与上面这段约定自相矛盾，界面会把"扫到配额
        就停了"显示成精确用量。
        """
        total = 0
        seen = 0
        for base, _dirs, files in os.walk(root):
            for name in files:
                seen += 1
                if seen > max_entries:
                    return total, True
                try:
                    total += os.path.getsize(os.path.join(base, name))
                except OSError:
                    continue
                if limit is not None and total >= limit:
                    return total, True
        return total, False

    @staticmethod
    def count_entries(root: Path, max_entries: int = 200_000) -> int:
        """目录树里的条目数（文件 + 目录），用于"这个共享项有多大"的粗略提示。

        到上限就返回上限值：这时它是下界，但**不设**独立标志 —— 用量那边已经有
        `truncated`（同一次遍历的两个数字一起被截断，界面按同一个"≥"显示即可）。
        """
        count = 0
        for _base, dirs, files in os.walk(root):
            count += len(dirs) + len(files)
            if count > max_entries:
                return max_entries
        return count

    # ── 身份 / 名单 ───────────────────────────────────────────────────────
    #
    # 内核是 `shell/groupmesh`，一个普通包，**import 层面直接可用**：
    # 既不需要往 sys.path 里塞路径，也不需要"找不到内核时降级"这种分支
    # （打包时由 HIDDEN_IMPORTS 保证它随包分发，缺失会在 check_packaging 阶段暴露）。

    def load_identity(self) -> Optional[Identity]:
        """读取本机身份；尚未初始化时返回 None。"""
        if not (self.identity_dir / 'principal.json').is_file():
            return None
        try:
            return Identity.load(self.identity_dir)
        except RecordError as e:
            log.warning(f'[group-mesh] 身份读取失败: {e}')
            return None

    def load_roster(self) -> Optional[Roster]:
        path = self.identity_dir / 'roster.json'
        if not path.is_file():
            return None
        return Roster.from_dict(json.loads(path.read_text(encoding='utf-8')))

    def save_roster(self, roster: Roster) -> None:
        """把名单写回 `identity/roster.json`（原子写），并并入名单历史。

        两件事必须一起做：

        1. **落盘当前名单**：只更新内存副本的话，插件重载（改设置、升级、壳重启）
           会退回旧名单，表现成"刚升级完又变回去了"。
        2. **并入历史**（`roster-history.json`）：准入判定要回答"本机认不认得对端
           手里那份"，而本机当前名单只说明"见过这一份"。缺了历史，落后多版的设备
           （群主连加两人，它只有 v1）就取不到新名单，只能回去要邀请串。
        """
        self.identity_dir.mkdir(parents=True, exist_ok=True)
        path = self.identity_dir / 'roster.json'
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(roster.to_dict(), ensure_ascii=False, indent=2) + '\n',
                       encoding='utf-8')
        os.replace(tmp, path)
        self.record_roster_history(roster)
        # 名单变了：唤醒后台同步，把它 push 给对端；否则只能等对方下一次轮询来拉。
        self._pulse.request(roster_dirty=True)

    def roster_history(self) -> roster_history_mod.RosterHistory:
        """名单历史表（读盘按需，进程内缓存由一个实例承载）。"""
        history = self._roster_history_store
        if history is None:
            history = roster_history_mod.RosterHistory(self.identity_dir)
            self._roster_history_store = history
        return history

    def record_roster_history(self, roster: Roster) -> None:
        """把一份**已签名**的名单并入历史。失败只记日志：历史不是通信的前提。"""
        try:
            self.roster_history().record(roster)
        except Exception as e:
            log.warning(f'[group-mesh] 名单历史写入失败（不影响通信）: {e}')

    def load_roster_history(self) -> List[Roster]:
        try:
            return self.roster_history().load()
        except Exception as e:
            log.warning(f'[group-mesh] 名单历史读取失败，按空历史处理: {e}')
            return []

    # ── 共享项 ────────────────────────────────────────────────────────────

    def share_roots_file(self) -> Path:
        return self.identity_dir / 'share_roots.json'

    def load_share_roots(self) -> Dict[str, Dict[str, Any]]:
        """读共享项的本地位置表（本机事实，从不发给对端）。

        读不到 / 损坏时返回空表而不是抛异常：位置表丢了只会让共享项不可用，
        而抛异常会让整个 `get_status()` 失败 —— 界面连"哪里坏了"都显示不出来。
        """
        path = self.share_roots_file()
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            log.warning('[group-mesh] share_roots.json 无法解析，按空位置表处理')
            return {}
        roots = payload.get('roots') if isinstance(payload, dict) else None
        return roots if isinstance(roots, dict) else {}

    def save_share_roots(self, roots: Dict[str, Dict[str, Any]]) -> None:
        self.identity_dir.mkdir(parents=True, exist_ok=True)
        self.share_roots_file().write_text(
            json.dumps({'roots': roots}, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')

    def load_shares(self) -> Dict[str, LocalShare]:
        """把「声明的协议对象」与「本机的路径/配额」合成 `LocalShare`。

        位置以 `share_roots.json` 为准；该文件里没有条目时回落到 `shares.json`
        自带的 `path`（迁移前的布局），因此升级不需要跑任何脚本 —— 第一次
        `add_share` / `remove_share` 会把位置表补齐。
        """
        path = self.identity_dir / 'shares.json'
        if not path.is_file():
            return {}
        roots = self.load_share_roots()
        shares: Dict[str, LocalShare] = {}
        for share_id, item in (json.loads(path.read_text(encoding='utf-8')).get('shares') or {}).items():
            try:
                share = LocalShare.from_dict(item)
            except RecordError as e:
                log.warning(f'[group-mesh] 跳过损坏的共享项 {share_id}: {e}')
                continue
            entry = roots.get(share_id)
            if isinstance(entry, dict) and entry.get('path'):
                share.path = str(entry['path'])
                if 'max_bytes' in entry:
                    share.max_bytes = entry['max_bytes']
            shares[share_id] = share
        return shares

    def save_shares(self, shares: Dict[str, LocalShare]) -> None:
        """声明写 `shares.json`，位置写 `share_roots.json`（两类数据分开存）。"""
        self.identity_dir.mkdir(parents=True, exist_ok=True)
        payload = {'shares': {sid: share.to_dict() for sid, share in shares.items()}}
        (self.identity_dir / 'shares.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        self.save_share_roots({sid: {'path': share.path, 'max_bytes': share.max_bytes}
                               for sid, share in shares.items()})

    # ── 手动登记的对端 ────────────────────────────────────────────────────

    def peers_file(self) -> Path:
        """手动登记的对端端点。放在 `.cache/` 里：它是**发现用的提示**，不是权威数据
        （权威数据是设备自签的注册记录），删掉只会让发现退回初始状态。"""
        return self.cache_dir / 'peers.json'

    def load_manual_peers(self) -> List[Dict[str, Any]]:
        path = self.peers_file()
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            log.warning('[group-mesh] peers.json 无法解析，按空表处理')
            return []
        entries = payload.get('peers') if isinstance(payload, dict) else None
        return entries if isinstance(entries, list) else []

    def save_manual_peers(self, entries: List[Dict[str, Any]]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.peers_file().write_text(
            json.dumps({'peers': entries}, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')
