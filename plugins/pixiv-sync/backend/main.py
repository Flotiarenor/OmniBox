"""Pixiv 同步插件：image-viewer 的 Companion 插件（入口）。

功能：
  - 同步画师：拉取关注画师全部作品，按画师目录下载
  - 同步喜欢：拉取当前用户公开收藏，按作品画师归入目录
  - 去重：已下载作品 id 记入 <root>/.cache/pixiv-sync/downloaded_ids.json（两同步共用）
  - 永久跳过：同步时 404/已删除的作品记入 failed_ids.json，不再每次重试；
    可通过「重试失败作品」按钮一键清除并自动重新同步一次
  - 断点：任务状态每张一写 <root>/.cache/pixiv-sync/tasks.json，重启后恢复 paused

本文件只做四件事：声明插件对壳暴露什么（设置 schema、扩展入口）、把组件装配起来、
把壳按名调用的钩子一行委派给组件、保留既有模块与用例按实例取用的成员。业务逻辑在
backend/pixiv_sync/ 的组件里（门禁把入口的行数上限钉在 250 行）。

加载语义：manifest.libs 已把 backend 与 backend/libs 放进 sys.path，因此这里用真包
import 取组件（本仓库另一套约定是 load_sibling 的兄弟模块加载）。保持真包 import 的
理由：pixiv_sync 是有 __init__.py 的包，vendored 依赖 pixiv_mini 在其 libs 子目录；
改成分片加载要把整个包拍平、改写全部模块签名与既有用例，收益只是统一一处加载约定，
而 pyright 对 libs 的覆盖已经通过类型检查范围达成。
"""

import threading
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Set

from pixiv_mini import PixivClient
from pixiv_sync import const, oauth
from pixiv_sync.library import LocalLibrary
from pixiv_sync.limits import SettingsLimits
from pixiv_sync.maintenance import Maintenance
from pixiv_sync.paths import CachePaths
from pixiv_sync.records import IdRecords
from pixiv_sync.runner import SyncRunner
from pixiv_sync.selection import ArtistSelection
from pixiv_sync.session import PixivSession
from pixiv_sync.status import StatusReport
from pixiv_sync.workspace import TaskWorkspace

from shell.backend.freshness import FreshnessEngine, engine_for
from shell.backend.plugin_base import PluginBase


class PixivSyncPlugin(PluginBase):
    settings_schema: ClassVar[List[Dict[str, Any]]] = const.SETTINGS_SCHEMA

    def __init__(self, manifest, config) -> None:
        super().__init__(manifest, config)
        self._host = None
        self._lock = threading.Lock()          # 保护任务线程启动/取消（OAuth 向导也读它）
        self._task_lock = threading.RLock()    # 保护计数 / ids / failed 集合 / 清单写（可重入）
        self._oauth_verifier: Optional[str] = None
        # 装配顺序就是依赖顺序：先算根，再建"知道路径的读者"，最后建编排者。
        self.paths = CachePaths(self, self._host_data_root)
        self.session = PixivSession(self, self.update_setting)
        self.records = IdRecords(self.paths)
        self.limits = SettingsLimits(self)
        self.workspace = TaskWorkspace(self.paths, self._task_lock)
        self.library = LocalLibrary(self.paths, self.records, self.workspace.db,
                                    self._engine, self._task_lock, const.FRESHNESS_INCLUDE)
        self.selection = ArtistSelection(self.paths)
        self.runner = SyncRunner(self, self.records, self.session, self.workspace,
                                 self.library, self.selection, self._lock, self._task_lock)
        self.maintenance = Maintenance(self.records, self.library, self.runner, self._engine)
        self.status = StatusReport(self.paths, self.records, self.selection, self.session,
                                   self.runner, self.workspace, self.library, self)

    # ---------- 宿主访问 ----------

    def _get_host(self):
        if self._host is None:
            self._host = self.get_dependency("image-viewer")
            if self._host is None:
                raise RuntimeError("pixiv-sync 需要 image-viewer 插件已加载并声明依赖")
        return self._host

    def _host_data_root(self) -> Path:
        """未配置下载目录时的落点：宿主相册根目录。"""
        return Path(self._get_host().get_data_root())

    def _engine(self) -> Optional[FreshnessEngine]:
        """统一刷新引擎（按需创建并缓存在本实例上）。"""
        return engine_for(self)

    # ---------- 既有模块与用例按实例取用的成员：短委派，名字不变 ----------
    # scan / download / oauth 的模块函数以插件实例为操作数（迁移前就是这份契约），
    # 用例也直接调用其中几个。改名或删掉等于静默失效：调用方一旦带 getattr 兜底，
    # 漏了不报错，只是某个功能退化成空。

    def _root(self) -> Path:
        return self.paths.root()

    def _ids_file(self) -> Path:
        return self.paths.ids_file()

    def _tasks_file(self) -> Path:
        return self.paths.tasks_file()

    def _artists_file(self) -> Path:
        return self.paths.artists_file()

    def _client(self) -> PixivClient:
        return self.session.client()

    def _db(self):
        """清单库连接（懒建，与任务线程共用同一把锁）。"""
        return self.workspace.db()

    def _load_ids(self) -> Set[int]:
        return self.records.downloaded()

    def _load_selected_artists(self) -> Set[str]:
        return self.selection.selected()

    def _scan_existing_ids(self) -> int:
        return self.library.scan_existing_ids()

    def _max_download(self) -> int:
        return self.limits.max_download()

    def _max_artists(self) -> int:
        return self.limits.max_artists()

    def _workers(self) -> int:
        return self.limits.workers()

    def _scan_workers(self) -> int:
        return self.limits.scan_workers()

    @property
    def _pixiv_client(self) -> Optional[PixivClient]:
        return self.session.cached_client

    @_pixiv_client.setter
    def _pixiv_client(self, value: Optional[PixivClient]) -> None:
        self.session.cached_client = value

    @property
    def _rate_limiter(self):
        return self.session.limiter

    @property
    def _thread(self):
        return self.runner.thread

    @property
    def _cancel_flag(self) -> bool:
        return self.runner.cancel_flag

    @_cancel_flag.setter
    def _cancel_flag(self, value: bool) -> None:
        self.runner.cancel_flag = value

    # ---------- 壳按名调用的钩子：一行委派 ----------

    def get_cache_dir(self) -> Path:
        """缓存目录 = 本插件自己的 `<下载根>/.cache/pixiv-sync`。

        必须覆写：本插件不覆写 `get_data_root()`（根来自 `download_dir` 设置，
        未配置时才回落到宿主数据根），基类默认实现会去读 `config['directories']`，
        而本插件的测试与运行都可能不带 `data_root`。
        """
        return self.paths.cache_dir()

    def freshness_spec(self) -> Dict:
        return self.library.spec()

    def on_unload(self) -> None:
        self.runner.shutdown()

    def on_settings_changed(self, changed_keys) -> None:
        self.runner.apply_settings_changed(changed_keys)

    # ---------- OAuth 向导 ----------

    def start_oauth(self) -> Dict:
        return oauth.start(self)

    def finish_oauth(self, code: str) -> Dict:
        return oauth.finish(self, code)

    # ---------- API 表与扩展声明 ----------

    def register_api(self) -> dict:
        return {
            "get_status": self.status.report,
            "sync_following": self.runner.sync_following,
            "sync_bookmarks": self.runner.sync_bookmarks,
            "refresh_following_lists": self.runner.refresh_following_lists,
            "refresh_bookmarks_lists": self.runner.refresh_bookmarks_lists,
            "refresh_downloaded": self.maintenance.refresh_downloaded,
            "verify_downloaded": self.maintenance.verify_downloaded,
            "cancel_verify": self.maintenance.cancel_verify,
            "retry_failed": self.runner.retry_failed,
            "cancel_task": self.runner.cancel_task,
            "open_config": self.selection.open_config,
            "start_oauth": self.start_oauth,
            "finish_oauth": self.finish_oauth,
            "get_settings": self.get_settings,
            "save_settings": self.save_settings,
        }

    def get_extensions(self) -> List[dict]:
        """注册到 image-viewer 左侧栏的扩展入口。"""
        return const.extensions()
