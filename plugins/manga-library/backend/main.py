"""manga-library 插件入口：装配、API 表、壳按名调用的钩子。

本文件只做三件事：声明插件对壳暴露什么、把组件装配起来、把壳按名调用的钩子
一行委派给组件。设置项 schema 在 `const.py`，业务逻辑在组件里（分层门禁的入口
硬上限 250 行，目标 200 行以内）。

接缝说明：`load_sibling` 返回 `ModuleType`，装配出来的组件属性在静态检查里是
`Any`。这是有意的取舍 —— 需要检查的东西都在组件里；为了让接缝有类型而把插件
目录塞进 `sys.path` 会换来模块名冲突，代价更大。

装配顺序就是依赖顺序：路径 → 书架（扫描规则由 `scanner` 提供）→ 刷新声明 → 下载中心。
反向依赖（下载执行器、任务表读写、扫描函数）一律由这里按构造参数注入，组件之间
不互相 import。
"""

from pathlib import Path
from typing import Any, ClassVar, Dict, List

from shell.backend.freshness import engine_for
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

_const = load_sibling(__file__, 'const', 'manga_library')
_paths = load_sibling(__file__, 'paths', 'manga_library')
_library = load_sibling(__file__, 'library', 'manga_library')
_freshness = load_sibling(__file__, 'freshness', 'manga_library')
_downloads = load_sibling(__file__, 'downloads', 'manga_library')
_scanner = load_sibling(__file__, 'scanner', 'manga_library')
_dmodels = load_sibling(__file__, 'download_models', 'manga_library_download')
_dstate = load_sibling(__file__, 'download_state', 'manga_library_download')
_downloader = load_sibling(__file__, 'downloader', 'manga_library_download')

#: 禁漫精简客户端装在 `backend/libs`（manifest 的 `libs` 声明，PluginManager 与测试
#: 夹具都会在加载后端前把它加进 sys.path）。它相当于本插件的 vendored 依赖：移植自
#: jmcomic，与 pixiv-sync 的 `pixiv_mini` 同一形态，不参与插件后端的组件分层规则。
from jm_mini import JmClient, save_image  # noqa: E402


class MangaLibraryPlugin(PluginBase):
    # 设置项声明是纯数据，放在 const.py；壳的设置弹窗与 PluginBase 都从类属性读。
    settings_schema: ClassVar[List[Dict[str, Any]]] = _const.SETTINGS_SCHEMA

    # 书架派生数据的规则版本：收录哪些顶层目录、封面怎么挑变了就 +1，
    # 由统一刷新基建整体作废（不再依赖"记得手动清缓存"）
    SHELF_VERSION: ClassVar[int] = _const.SHELF_VERSION

    def __init__(self, manifest: dict, config: dict) -> None:
        super().__init__(manifest, config)
        # 显式写 PluginBase.get_data_root(self) 而不是 super()：不依赖 MRO 顺序。
        root = self.setting('root_dir') or str(PluginBase.get_data_root(self))
        self.paths = _paths.PathLayout(Path(root), _const.DOWNLOAD_STATE_DIRNAME,
                                       _const.DOWNLOAD_STATE_FILE)
        self.library = _library.Library(
            self.paths, _scanner, int(self.setting('recent_count', 10)),
            _const.RECENT_LIMIT, _const.RECENT_COUNT_MIN, _const.RECENT_COUNT_MAX)
        self.freshness = _freshness.ShelfFreshness(
            self.paths, self.library, _scanner.IMAGE_EXTS, self.SHELF_VERSION)
        self.downloads = _downloads.DownloadCenter(
            self.paths, _dmodels.DownloadTask, _dstate.load_tasks,
            _dstate.save_tasks, self._run_download, self._on_download_completed)
        #: 禁漫接口客户端：首个下载任务时才构造（域名引导与版本协商要发请求），
        #: 之后复用同一实例，域名列表与 scramble_id 缓存不会每次重建。
        self._jm_client: Any = None

    def _run_download(self, task, manga_dir: str, state_dir: str, lock, logger):
        """把接口客户端与图片解码注进下载执行器（两者是两件事，各自可替换）。"""
        if self._jm_client is None:
            self._jm_client = JmClient()
        return _downloader.execute_download(
            task, manga_dir, state_dir, lock, logger,
            client=self._jm_client, save_image=save_image)

    def _on_download_completed(self, _task_id: str) -> None:
        """下载完成：图片与 `album_info.json` 已写进漫画根，书架缓存与去抖立即作废。

        下载中心与书架共用同一个数据根却互不 import（分层门禁 R3），"写完该失效
        什么"由入口编排 —— 与换根编排同一形态。不在这里做全树重扫：书架缓存只置
        脏，下一次 `manga_list` / 被动同步才付扫描成本。
        """
        self.library.reset()
        engine = engine_for(self)
        if engine is not None:
            # 清掉去抖：下一次进书架的被动同步立刻按新目录重扫
            engine.reset_throttle()

    def register_api(self) -> dict:
        return {
            'manga_list': self.library.list_manga,
            'manga_search': self.library.search,
            'manga_get_state': self.library.get_state,
            'manga_toggle_favorite': self.library.toggle_favorite,
            'manga_update_recent': self.library.update_recent,
            'manga_get_detail': self.library.get_detail,
            'manga_get_pages': self.library.get_pages,
            'download_submit': self.downloads.submit,
            'download_list': self.downloads.list_tasks,
            'download_pause': self.downloads.pause,
            'download_resume': self.downloads.resume,
            'download_retry': self.downloads.retry,
            'download_delete': self.downloads.delete,
            'download_start_all': self.downloads.start_all,
            'download_pause_all': self.downloads.pause_all,
            'download_clear_completed': self.downloads.clear_completed,
            'download_get_album_info': self.downloads.album_info,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    # ===== 壳按名调用的钩子：名字与签名与迁移前完全一致 =====

    def get_data_root(self) -> Path:
        return self.paths.root

    def get_settings(self) -> Dict:
        return {
            "root_dir": str(self.paths.root),
            "recent_count": self.library.recent_count,
        }

    def on_settings_changed(self, changed_keys) -> None:
        if 'root_dir' in changed_keys:
            new_dir = self.setting('root_dir')
            if new_dir and Path(new_dir).is_dir():
                self._apply_root_dir(Path(new_dir).resolve())
        if 'recent_count' in changed_keys:
            self.library.set_recent_count(self.setting('recent_count', 10))

    def freshness_spec(self) -> Dict[str, Any]:
        return self.freshness.spec()

    # ===== 换根编排（唯一知道"谁跟着根走"的地方）=====

    def _apply_root_dir(self, new_dir: Path) -> None:
        """换根：路径 → 书架状态 → 下载任务表 → 刷新引擎的去抖。"""
        self.paths.rebind(new_dir)
        self.library.rebind()
        self.downloads.rebind()
        engine = engine_for(self)
        if engine is not None:
            # 换根后清掉去抖，让下一次进书架的被动同步立刻按新根重扫
            engine.reset_throttle()
