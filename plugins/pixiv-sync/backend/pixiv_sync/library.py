"""本地图片树 → 已下载作品集合（统一刷新基建的插件侧实现）。

为什么需要它：这是本插件唯一的文件系统派生数据。迁移前每次刷新记录/校验内容都全盘
walk 一遍（`store.scan_local`），而它们并没有共同的失效判据。接入基建后：目录级短路
决定"要不要重算"，**判据（缺页才算未完成）与语义（谁进/谁出 ids）完全不变** —— 只是
不再每次都走一遍磁盘。

另一方面，刷新记录与校验内容还有各自的并发缺口（只挡下载线程、自己不占位），现在
两者都转发到引擎，由引擎的单飞与状态托管；本组件只实现钩子。

状态自持：`_local_changed`（本趟看到的本地变化数）与引擎引用都在这里，换根时 reset()。
"""

import logging
from pathlib import Path
from typing import Callable, Dict, Optional, Set, Tuple

from shell.backend.freshness import FreshnessEngine

from . import store

log = logging.getLogger(__name__)


class LocalLibrary:
    def __init__(
        self,
        paths,
        records,
        db: Callable,
        engine: Callable[[], Optional[FreshnessEngine]],
        lock,
        include: Tuple[str, ...],
    ) -> None:
        self._paths = paths
        self._records = records
        self._db = db
        self._engine = engine
        self._lock = lock
        self._include = include
        #: 本趟看到的本地变化数（0 = 无需按指纹库重算 ids）
        self._local_changed = 0

    # ---------- 引擎与声明 ----------

    def cache_dir(self) -> Path:
        return self._paths.cache_dir()

    def engine(self) -> Optional[FreshnessEngine]:
        """统一刷新引擎（按需创建并缓存在插件实例上）。"""
        return self._engine()

    def spec(self) -> Dict:
        """`freshness_spec()` 的内容：受管根 + 三个钩子 + 条目计量单位。"""
        return {
            'roots': lambda: [self._paths.pixiv_root()],
            # 与 store.ID_NAME_RE 认识的扩展名保持一致：键集合要能反推出作品页数
            'include': self._include,
            'derive': self.derive,
            'prune': self.prune,
            'on_verified': self.audit,
            'on_pass_end': self.pass_end,
            'rebuild': self.rebuild,
            'content_version': 1,
            'unit': '张',
            # 图片树可能上万文件：被动同步间隔放宽，避免每次进页面都走一遍
            'min_sync_interval': 10.0,
        }

    def state(self) -> Dict:
        """引擎进度的投影；引擎不可用时返回"未接入"的空形状。"""
        engine = self._engine()
        empty = {"available": False, "busy": False, "task": None,
                 "entries": 0, "dirs": 0, "last_verify_at": 0}
        if engine is None:
            return empty
        try:
            state = engine.state()
        except Exception as e:  # 引擎读库失败不该把整个状态接口打挂
            log.error(f"[pixiv-sync] 读取本地索引状态失败: {e}")
            return empty
        return {
            "available": bool(state.get("available", True)),
            "busy": bool(state.get("busy")),
            "task": state.get("task"),
            "entries": int(state.get("entries") or 0),
            "dirs": int(state.get("dirs") or 0),
            "last_verify_at": float(state.get("last_verify_at") or 0),
        }

    def reset(self) -> None:
        """换根：丢掉"本趟变化数"，下一次重算从零累计。"""
        self._local_changed = 0

    # ---------- 引擎钩子 ----------

    def derive(self, items) -> Dict:
        self._local_changed += len(items)
        return {'count': len(items)}

    def prune(self, keys) -> int:
        self._local_changed += len(keys)
        return len(keys)

    def audit(self, keys) -> Dict:
        """全量校验：以指纹库的有效键集合重建记录（含清单 done 快照）。"""
        # 目录遍历走完后还有一段"按清单对账"的尾巴（几万个 stat），实测要好几秒；
        # 不写一句话，进度条会停在 100% 让人以为卡死。
        engine = self._engine()
        if engine is not None:
            engine.progress("正在核对已下载记录…")
        return self.sync_ids_from_keys(keys, reset_done=True, remove_zero=True)

    def pass_end(self, report, verified: bool) -> Dict:
        """整趟结束：集合完整时才据它重算 ids（部分遍历会把没走到的当成消失）。"""
        if report.get('partial') or report.get('errors'):
            return {'skipped': 'incomplete'}
        if not (verified or self._local_changed):
            return {'skipped': 'unchanged'}
        self._local_changed = 0
        return self.sync_ids_from_keys(self.engine_keys(), reset_done=bool(verified),
                                       remove_zero=True)

    def rebuild(self, kind: str = "derived") -> Dict:
        """逃生门：按磁盘重算记录。

        **不删除** downloaded_ids.json —— 那是用户数据，删掉等于让整个库重下一遍。
        """
        return self.sync_ids_from_keys(self.engine_keys(), reset_done=True,
                                       remove_zero=True)

    def engine_keys(self) -> Set[str]:
        engine = self._engine()
        return engine.valid_keys() if engine is not None else set()

    # ---------- 有效键集合 → ids ----------

    def local_pages(self, keys, remove_zero: bool = False):
        """有效键集合 → ({作品 id: {页号}}, 删除的 0 字节文件数)。

        0 字节残片（下载中断留下的）**不算有效页**，并按旧行为顺手删掉：让它算一页
        会把"半截下载"判成"下全了"，缺页的作品就再也补不回来（判据与
        `store.scan_local(remove_zero=True)` 一致）。
        """
        root = self._paths.pixiv_root()
        pages: Dict[int, set] = {}
        zero = 0
        for key in keys or ():
            path = root / str(key)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= 0:
                if remove_zero:
                    try:
                        path.unlink()
                        zero += 1
                    except OSError:
                        pass
                continue
            match = store.ID_NAME_RE.match(path.name)
            if not match:
                continue
            iid = int(match.group(1))
            page = int(match.group(2)) if match.group(2) else 0
            pages.setdefault(iid, set()).add(page)
        return pages, zero

    def sync_ids_from_keys(self, keys, reset_done: bool, remove_zero: bool) -> Dict:
        """按有效键集合把"已下载作品"与磁盘对齐（刷新记录与校验内容共用的实现）。

        `stale` 与 `incomplete` 必须是两类，不能合并：前者是"本地一个有效文件都没有"
        （失效记录），后者是"本地有文件但页数不足"（半截下载）。界面按两个数字分开
        说明（"移除失效记录 N 条，缺页重新入队 M 件"），合并后就只剩一个笼统的数字。
        两者都会被移出去重集合，因此 `ids` 最终等于 `recorded`。
        """
        pages, zero = self.local_pages(keys, remove_zero=remove_zero)
        local_ids = {iid for iid, pg in pages.items() if pg}
        incomplete = self._db().incomplete_ids(pages)
        recorded = set(self._db().importable_ids(pages))
        before = set(self._records.downloaded())
        stale = sorted(before - local_ids)
        failed = self._records.failed()
        # 失败跳过里"本地已经有有效文件"的，应当被清掉（下次同步会重新检查）
        failed_cleared = [iid for iid in failed if iid in local_ids]
        with self._lock:
            # **就地增删**：`_run_sync` 里的 `ids` 是同一个 set 对象，换成新对象会让
            # 下载线程随后写回的记录全部丢失（`save_downloaded` 读的是同一个集合）。
            ids = self._records.downloaded()
            ids.intersection_update(recorded)
            ids.update(recorded)
            self._records.save_downloaded()
            if failed_cleared:
                for iid in failed_cleared:
                    failed.discard(iid)
                self._records.save_failed()
        reset = sorted(set(stale) | set(incomplete))
        if reset_done and reset:
            self._db().reset_done_ids(reset)
        return {
            'ok': True,
            'total': len(recorded),
            'imported': len(recorded - before),
            'stale_removed': len(stale),
            'incomplete': len(incomplete),
            'incomplete_skipped': len(local_ids) - len(recorded),
            'failed_cleared': len(failed_cleared),
            'zero_removed': zero,
        }

    def scan_existing_ids(self) -> int:
        """扫描下载目录中已存在的图片，按命名规则提取作品 id 并入去重集合。

        用于识别用户手动放入的旧图：文件名符合 `{id}.jpg` / `{id}_p0.jpg` 规则即可被
        识别，之后全量更新会直接跳过，不会重复检查/下载。

        清单已记录、但本地页数少于清单 page_count 的作品**不并入**：半截下载若在这里
        被认成「已下载」，下载阶段就再也不会碰它，「校验内容」刚移出的作品也会被重新
        认回去，缺的页永远补不回来（判据见 db.WorksDB.importable_ids）。

        优先用指纹库的键集合（同步刚走过，等于白拿）；引擎不可用时退回全盘 walk。
        """
        keys = self.engine_keys()
        if keys:
            pages, _zero = self.local_pages(keys)
        else:
            _ids, _zero, pages = store.scan_local(self._paths.pixiv_root())
        ids = self._records.downloaded()
        before = len(ids)
        ids |= self._db().importable_ids(pages)
        found = len(ids) - before
        if found:
            with self._lock:
                self._records.save_downloaded()
        return found
