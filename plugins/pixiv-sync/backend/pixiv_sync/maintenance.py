"""「账号与维护」里的显式维护动作：刷新已下载记录、校验本地内容、停止校验。

为什么单列一个组件：这三个动作都是"用户主动触发的本地对账"，与同步任务是两条触发
路径（同步任务绝不顺带做本地核对）。它们共用统一刷新引擎，但**不做下载**，也不碰
任务线程的状态机；把它们塞进 SyncRunner 会让"同步"与"维护"两种失败原因混在一起。
"""

import logging
from typing import Dict

log = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, records, library, runner, engine) -> None:
        self._records = records
        self._library = library
        self._runner = runner
        self._engine = engine

    def refresh_downloaded(self) -> Dict:
        """刷新已下载记录（兼容入口，转发到统一刷新的「同步」）。

        语义不变：以本地为准重建 ids（手动删过的移除、手动加的导入、0 字节清理），
        清单已记录但本地页数不足的作品不并入（半截下载不算「已下载」，要补齐请点
        「校验内容」把它重新入队）。差别只是"要不要真扫一遍"现在由指纹决定。
        """
        engine = self._engine()
        if engine is None:
            # 旧壳没有统一刷新基建：退回直接走盘，至少能把手动放入的作品并进来。
            found = self._library.scan_existing_ids()
            return {"ok": True, "total": len(self._records.downloaded()), "imported": found,
                    "stale_removed": 0, "zero_removed": 0, "failed_cleared": 0,
                    "incomplete": 0, "incomplete_skipped": 0}
        engine.sync("", force=True)
        last = (engine.state().get("last_report") or {}).get("pass_end") or {}
        if not last or last.get("skipped"):
            # 同步没有走到重算（集合不完整）：退回显式按指纹库重算一次
            last = self._library.sync_ids_from_keys(
                self._library.engine_keys(), reset_done=False, remove_zero=True)
        return {"ok": True, **{k: last.get(k, 0) for k in
                               ("total", "zero_removed", "stale_removed",
                                "failed_cleared", "incomplete_skipped")}}

    def verify_downloaded(self) -> Dict:
        """启动本地内容校验（后台任务，立即返回）。

        这是面板「账号与维护 → 校验本地内容」按钮的入口：一次走盘把"手动放入的作品"
        并进来、把本地已无文件的失效记录移除、清理 0 字节残片、清掉"本地已有文件"的
        失败记录，并把页数不足的作品重置 done 以便下次同步补齐。

        为什么走后台而不是同步返回：全量校验在 6 万个文件上是几十秒量级（要遍历目录、
        写索引、再按清单对账），同步调用会让按钮看起来"点了没反应"。进度经
        `get_status.library` 暴露（目录数 processed/total + 当前目录），前端画进度条。
        """
        engine = self._engine()
        if engine is None:
            # 旧壳没有统一刷新基建：退回同步走盘，只能导入，无法给出进度。
            found = self._library.scan_existing_ids()
            return {"ok": True, "started": False, "finished": True,
                    "total": len(self._records.downloaded()), "imported": found}
        if self._runner.is_running():
            return {"ok": False, "error": "同步任务进行中，结束后再校验本地内容"}
        result = engine.start_verify()
        return {"ok": True, **result}

    def cancel_verify(self) -> Dict:
        """停止正在跑的本地内容校验（面板进度条上的「停止校验」）。"""
        engine = self._engine()
        if engine is None:
            return {"ok": False, "error": "刷新基建未接入"}
        return {"ok": bool(engine.request_cancel())}
