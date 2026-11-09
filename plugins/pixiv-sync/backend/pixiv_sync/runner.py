"""同步 / 刷新任务线程的生命周期与任务状态推进。

状态自持：任务对象、线程、取消标志、设置变更的延迟重置标记都在这里。为什么线程与
取消标记必须有唯一归属：`scan` / `download` 的模块函数以插件实例为操作数，运行期间
会直接读写取消标志（协作式取消），两处各存一份就会出现"点了取消但任务还在跑"。

接缝：`scan` / `download` 的公开函数签名仍是迁移前的"以插件实例为操作数"，因此本组件
持有入口实例（operand）并把依赖通过入口的短委派取用；这两组函数的签名与既有用例是
同一份契约，重构它们属于另一件事。
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from pixiv_mini import PixivCancelled, PixivError

from . import download, scan
from . import tasks as tasks_mod
from .limiter import RateLimitError

log = logging.getLogger(__name__)


class SyncRunner:
    def __init__(
        self,
        operand,
        records,
        session,
        workspace,
        library,
        selection,
        lock,
        task_lock,
        invalidate_host: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._operand = operand
        self._records = records
        self._session = session
        self._workspace = workspace
        self._library = library
        self._selection = selection
        self._lock = lock              # 保护任务线程启动/取消
        self._task_lock = task_lock    # 保护计数 / ids / failed 集合 / 清单写
        self._invalidate_host = invalidate_host   # 写入宿主根之后的通知（入口注入）
        self._thread: Optional[threading.Thread] = None
        self._task: Optional[Dict[str, Any]] = None
        # 取消令牌：每次任务一个新的 Event，线程把自己的那个登记到 `_run_tokens`。
        # 不用单个布尔量是因为"取消后立刻开始下一次同步"时，上一次的线程可能还活着
        # （在等 socket 超时），它必须看到自己的令牌已置位、从而丢弃全部写回。
        self._cancel_event = threading.Event()
        self._run_tokens: Dict[int, threading.Event] = {}
        self._deferred_root_reset = False
        self._deferred_client_reset = False
        self._task = workspace.load_task()

    # ---------- 供入口短委派的实例状态 ----------

    @property
    def thread(self) -> Optional[threading.Thread]:
        return self._thread

    @property
    def cancel_event(self) -> threading.Event:
        """当前线程所属任务的取消令牌（非任务线程取最近一次任务的令牌）。"""
        return self._run_tokens.get(threading.get_ident(), self._cancel_event)

    @property
    def cancel_flag(self) -> bool:
        return self.cancel_event.is_set()

    @cancel_flag.setter
    def cancel_flag(self, value: bool) -> None:
        if value:
            self.cancel_event.set()
        else:
            self.cancel_event.clear()

    def task_snapshot(self) -> Optional[Dict[str, Any]]:
        """当前任务对象的拷贝（状态轮询用，避免读到写入一半的字段）。"""
        with self._lock:
            return dict(self._task) if self._task else None

    def is_running(self) -> bool:
        """用户视角的"任务在跑"：点了取消就不再算在跑，哪怕线程还在收尾。"""
        return bool(
            self._thread and self._thread.is_alive() and not self._cancel_event.is_set()
        )

    # ---------- 同步任务 ----------

    def sync_following(self) -> Dict:
        """同步画师：内部先自动刷新关注清单（增量），再按清单下载新作。"""
        return self._start("following")

    def sync_bookmarks(self) -> Dict:
        """同步喜欢：内部先自动刷新收藏清单（增量），再按清单下载新作。"""
        return self._start("bookmarks")

    def _start(self, kind: str) -> Dict:
        with self._lock:
            # 已经取消、正在收尾的旧线程不算"在运行"：用户可以立刻开下一次同步，
            # 旧线程的写回由它自己的令牌挡住（见 `cancel_event`）。
            if self._thread and self._thread.is_alive() and not self._cancel_event.is_set():
                return {"ok": False, "error": "已有同步任务在运行"}
            # 校验与同步都会改写"已下载记录"：互斥，避免两边对账互相覆盖。
            if self._library.state().get("busy"):
                return {"ok": False, "error": "正在校验本地内容，请等它结束再同步"}
            self._cancel_event = threading.Event()
            token = self._cancel_event
            self._thread = threading.Thread(
                target=self._run_sync, args=(kind, token), daemon=True
            )
            self._thread.start()
        return {"ok": True, "data": {"kind": kind}}

    def _set_phase(self, task: Dict, text: str) -> None:
        """推进任务阶段：写 `current` 并落盘，同时把"本阶段耗时"记进日志。

        阶段文案就是用户在面板上看到的进度明细。任务一旦进入线程就立刻置为
        `running`，`queued` 只保留线程启动前的瞬间——否则用户看到的是"排队中 /
        准备中"这种没有信息量的状态，而真正耗时的登录、本地核对、拉清单都藏在里面。
        """
        now = time.time()
        with self._task_lock:
            since = now - float(task.get("phase_started_at") or task.get("started_at") or now)
            task["current"] = text
            task["phase_started_at"] = now
        self._workspace.persist_task(task)
        log.info("[pixiv-sync] 阶段: %s（上一阶段 %.1fs）", text, since)

    def _run_sync(self, kind: str, token: Optional[threading.Event] = None) -> None:
        token = token or self._cancel_event
        self._run_tokens[threading.get_ident()] = token
        self._workspace.begin()
        task = tasks_mod.new_task(kind)
        # 线程已经起来了：先置 running，再用 current 表达阶段。queued 只覆盖线程启动前的瞬间。
        task["state"] = "running"
        self._workspace.persist_task(task)
        self._task = task
        ids = self._records.downloaded()
        failed = self._records.failed()
        try:
            self._set_phase(task, "正在登录 Pixiv…")
            self._session.authenticate()
            # 把令牌挂到客户端上：重试退避与流式下载的分块都靠它中断。
            self._session.client().set_stop_event(token)
            # 本地已有作品不再在每次同步前全盘核对：那是显式维护动作
            # （refresh_downloaded / verify_downloaded，面板「账号与维护」里的两个按钮）。
            # 同步直接用 downloaded_ids.json 这份记录，避免每次几秒的目录遍历。
            if token.is_set():
                task["state"] = "cancelled"
                task["current"] = "已取消"
                return
            task["state"] = "running"
            self._workspace.persist_task(task)

            # 阶段 1：自动刷新清单（增量、带断点，用户无需先手动点「刷新」）。
            #   同步画师 = 刷新关注清单（新画师全量 + illust_follow 聚合增量 + 兜底）；
            #   同步喜欢 = 刷新收藏清单（首次全量/增量翻页）。
            #   此阶段触发 429 会抛 RateLimitError 停车（见下），冷却后重点同步即续跑。
            self._set_phase(task, f"正在更新{'关注' if kind == 'following' else '收藏'}作品清单…")
            if kind == "following":
                items, scan_data = scan.collect_following_pending(self._operand, task, ids)
            else:
                items, scan_data = scan.collect_bookmarks_pending(self._operand, task, ids)
            self._workspace.db().save_pending(kind, items, scan_data)
            if token.is_set():
                task["state"] = "cancelled"
                task["current"] = "已取消"
                return

            # 阶段 2：按清单下载
            download.download_pending(self._operand, task, ids, failed, kind)

            task["state"] = "cancelled" if token.is_set() else "done"
        except RateLimitError as e:
            task["state"] = "failed"
            task["error"] = str(e)
            self._session.note_rate_limited()
        except PixivCancelled:
            task["state"] = "cancelled"
        except PixivError as e:
            task["state"] = "failed"
            task["error"] = str(e)
        except Exception as e:
            task["state"] = "failed"
            task["error"] = f"{type(e).__name__}: {e}"
        finally:
            try:
                self._session.client().set_stop_event(None)
            except Exception:
                pass
            self._run_tokens.pop(threading.get_ident(), None)
            task["finished_at"] = time.time()
            task["current"] = ""
            self._records.save_downloaded()
            self._records.save_failed()
            # 取消后任务已由 `cancel_task` 定稿；这里再写一次是幂等的，但要把
            # 状态留在 cancelled 上，不能被收尾流程改成 done。
            if token.is_set():
                task["state"] = "cancelled"
            self._workspace.persist_task(task)
            # 只在**这一趟确有下载**时通知宿主（每趟一次，不是每张作品一次）：未配
            # token 的失败、清单为空或全部已下的零下载空跑都没改过它的目录，清一次
            # 相册索引只会让它白重扫。失败/取消前已写出的文件同样要刷新，判据只看
            # `downloaded`；回调由入口注入、自己吞异常，通知不影响任务终态。
            if self._invalidate_host is not None and int(task.get("downloaded") or 0) > 0:
                self._invalidate_host('pixiv-sync:download')
            self._finish_task_runtime()

    # ---------- 只刷新名单（不下载） ----------

    def refresh_following_lists(self) -> Dict:
        """仅刷新关注画师作品清单（不下载）。

        「同步画师」已内置自动刷新；本入口用于只想更新清单看统计、
        或手动控制大批量首次铺底的刷新时机。
        """
        return self._start_refresh("following")

    def refresh_bookmarks_lists(self) -> Dict:
        """仅刷新喜欢画作清单（不下载）；「同步喜欢」已内置自动刷新。"""
        return self._start_refresh("bookmarks")

    def _start_refresh(self, kind: str) -> Dict:
        with self._lock:
            if self._thread and self._thread.is_alive() and not self._cancel_event.is_set():
                return {"ok": False, "error": "已有任务在运行"}
            if self._library.state().get("busy"):
                return {"ok": False, "error": "正在校验本地内容，请等它结束再刷新名单"}
            self._cancel_event = threading.Event()
            token = self._cancel_event
            self._thread = threading.Thread(
                target=self._run_refresh, args=(kind, token), daemon=True
            )
            self._thread.start()
        return {"ok": True}

    def _run_refresh(self, kind: str, token: Optional[threading.Event] = None) -> None:
        token = token or self._cancel_event
        self._run_tokens[threading.get_ident()] = token
        self._workspace.begin()
        task = tasks_mod.new_task(f"refresh_{kind}")
        task["state"] = "running"
        self._workspace.persist_task(task)
        self._task = task
        try:
            self._set_phase(task, "正在登录 Pixiv…")
            self._session.authenticate()
            self._session.client().set_stop_event(token)
            self._set_phase(task, f"正在更新{'关注' if kind == 'following' else '收藏'}作品清单…")
            ids = self._records.downloaded()
            if kind == "following":
                items, scan_data = scan.collect_following_pending(self._operand, task, ids)
            else:
                items, scan_data = scan.collect_bookmarks_pending(self._operand, task, ids)
            self._workspace.db().save_pending(kind, items, scan_data)
            task["state"] = "cancelled" if token.is_set() else "done"
            task["total"] = len(items)
            done_cnt = sum(1 for i in items if i.get("done"))
            task["current"] = (
                f"清单 {len(items)} 条（已下 {done_cnt}）· 已扫描"
                + ("完成" if (scan_data or {}).get("complete") else "部分（可再点刷新继续）")
            )
        except RateLimitError as e:
            task["state"] = "failed"
            task["error"] = str(e)
            self._session.note_rate_limited()
        except PixivCancelled:
            task["state"] = "cancelled"
        except PixivError as e:
            task["state"] = "failed"
            task["error"] = str(e)
        except Exception as e:
            task["state"] = "failed"
            task["error"] = f"{type(e).__name__}: {e}"
        finally:
            try:
                self._session.client().set_stop_event(None)
            except Exception:
                pass
            self._run_tokens.pop(threading.get_ident(), None)
            task["finished_at"] = time.time()
            if token.is_set():
                task["state"] = "cancelled"
            self._workspace.persist_task(task)
            self._finish_task_runtime()

    # ---------- 取消 / 重试 ----------

    def cancel_task(self) -> Dict:
        """立刻取消：界面马上是"已取消"，不等后台线程走到检查点。

        后台线程可能正卡在 socket 上（请求超时最长一个 `api_timeout`）。它拿不到
        我们想要的结果，也不该再影响用户：令牌置位后它的全部写回都会被挡住，等它
        自己退出即可——这就是"看起来立刻停了"的实现方式，不需要杀进程。
        """
        with self._lock:
            if not self._task or self._task.get("state") not in ("queued", "running"):
                return {"ok": True}
            self._cancel_event.set()
            self._task["state"] = "cancelled"
            self._task["current"] = ""
            self._task["finished_at"] = time.time()
            self._workspace.persist_task(self._task)
            self._records.save_downloaded()
            self._records.save_failed()
            log.info("[pixiv-sync] 取消任务 %s（后台线程收尾中）", self._task.get("kind"))
        return {"ok": True}

    def retry_failed(self) -> Dict:
        """一键清除永久跳过的 404 记录，并立即重新同步一次（重试失败作品）。

        重新同步的来源取最近一次任务（画师/喜欢）；无历史任务时默认同步画师。
        清空后失败的清单条目保持未下载状态，会自然回到待下载队列。
        """
        with self._lock:
            if self._thread and self._thread.is_alive() and not self._cancel_event.is_set():
                return {"ok": False, "error": "已有任务在运行"}
            failed = self._records.failed()
            with self._task_lock:
                self._records.clear_failed()
            self._records.save_failed()
            kind = "following"
            t = self._workspace.load_task()
            if t and t.get("kind") in ("following", "bookmarks"):
                kind = t["kind"]
            self._cancel_event = threading.Event()
            token = self._cancel_event
            self._thread = threading.Thread(
                target=self._run_sync, args=(kind, token), daemon=True
            )
            self._thread.start()
        return {"ok": True, "cleared": len(failed), "kind": kind}

    # ---------- 根目录固定 / 设置变更 ----------

    def _finish_task_runtime(self) -> None:
        """任务结束：解除根目录固定，并应用运行期间用户修改设置的延迟重置。"""
        self._workspace.finish()
        if self._deferred_root_reset:
            self._deferred_root_reset = False
            self.reset_root_state()
        if self._deferred_client_reset:
            self._deferred_client_reset = False
            self._session.reset_client()

    def reset_root_state(self) -> None:
        """下载目录变更后重置所有依赖根目录的缓存状态。"""
        with self._task_lock:
            self._workspace.close_db()
            self._records.reset()
            self._selection.reset()
            self._library.reset()
        self._task = self._workspace.load_task()
        engine = self._library.engine()
        if engine is not None:
            # 换根后清掉去抖，让下一次被动同步立刻按新根重扫
            engine.reset_throttle()

    def apply_settings_changed(self, changed_keys) -> None:
        """设置变更：运行中的任务延迟到结束后重置，空闲时立刻重置。"""
        running = self.is_running()
        if changed_keys & {"refresh_token", "proxy"}:
            if running:
                # 运行中的任务继续用旧 client/代理，结束后再重置。
                self._deferred_client_reset = True
            else:
                self._session.reset_client()
        if "download_dir" in changed_keys:
            # 下载根目录变了：所有缓存文件路径与 SQLite 连接都指向新目录。
            # 若任务正在跑，先固定旧根目录，任务结束后再切换。
            if running:
                self._deferred_root_reset = True
            else:
                self.reset_root_state()
        if "rate_limit" in changed_keys:
            self._session.apply_rate_setting()

    # ---------- 收尾 ----------

    def shutdown(self) -> None:
        """进程退出收尾：停同步线程、落盘最后一次任务状态、关 SQLite。

        不关连接会留下 -wal/-shm 边车文件并一直占着 works.db —— Windows 下
        文件被占用时连复制/移动数据目录都会失败。落盘的任务若仍是 running，
        下次 load_task 会归一成 paused 续跑。
        """
        self.cancel_flag = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)  # 协作式取消：给 worker 一个收尾窗口，不强等
        with self._task_lock:
            if self._task:
                self._workspace.persist_task(self._task)
        self._workspace.close_db()
