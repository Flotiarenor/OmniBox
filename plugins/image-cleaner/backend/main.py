"""image-viewer 的 Companion 插件：全相册重复 / 相似图片清理。入口（facade）。

本文件只做三件事：声明对壳暴露的 API 表、把组件装配起来、把壳按名调用的钩子一行委派给
组件。设置项 schema 与扩展条目在 `const.py`，业务逻辑在各组件里（分层门禁硬上限 250 行，
目标 200 行以内）。

该插件不修改 image-viewer 宿主，只通过依赖实例复用其数据根目录与删除能力；宿主实例的
解析（含"未加载则报错"）留在入口，组件通过 `HostBridge` 按需问"宿主在哪"。

接缝说明：`load_sibling` 返回 `ModuleType`，装配出来的组件属性在静态检查里是 `Any`。这是
有意的取舍 —— 需要检查的东西都在组件里；为让接缝有类型而把插件目录塞进 `sys.path` 会换来
模块名冲突。

装配顺序就是依赖顺序：宿主适配 → 指纹库 / 结果缓存 → 扫描 → 分组 → 编排 → 后台任务 →
统一刷新；反向依赖一律由构造参数注入。
"""

import logging
from pathlib import Path
from typing import Any, ClassVar, Dict, List

from shell.backend.freshness import engine_for
from shell.backend.plugin_base import PluginBase
from shell.backend.plugin_utils import load_sibling

log = logging.getLogger(__name__)

_const = load_sibling(__file__, 'const', 'image_cleaner')
_cancel = load_sibling(__file__, 'cancel', 'image_cleaner')
_host = load_sibling(__file__, 'host', 'image_cleaner')
_hashes = load_sibling(__file__, 'hashes', 'image_cleaner')
_scanner = load_sibling(__file__, 'scanner', 'image_cleaner')
_duplicates = load_sibling(__file__, 'duplicates', 'image_cleaner')
_similar = load_sibling(__file__, 'similar', 'image_cleaner')
_results = load_sibling(__file__, 'results', 'image_cleaner')
_pipeline = load_sibling(__file__, 'pipeline', 'image_cleaner')
_background = load_sibling(__file__, 'background', 'image_cleaner')
_freshness = load_sibling(__file__, 'freshness', 'image_cleaner')


class ImageCleanerPlugin(PluginBase):
    #: 设置项声明是纯数据，放在 const.py（入口只做装配与委派）。
    settings_schema: ClassVar[List[Dict[str, Any]]] = _const.SETTINGS_SCHEMA

    def __init__(self, manifest: dict, config: dict) -> None:
        super().__init__(manifest, config)
        # 宿主实例解析一次后缓存在入口：`get_dependency()` 要在插件自身身份上完成，
        # 组件只拿"问宿主在哪"与"取宿主的根清单"两个闭包。
        self._host = None
        self.host = _host.HostBridge(self._get_host, self._host_root_listings)
        self.hashes = _hashes.DHashStore(self.get_cache_dir, _const.DHASH_VERSION)
        self.results = _results.ScanResults(
            self.get_cache_dir, self.host, _const.SCAN_MODES)
        self.scanner = _scanner.AlbumScanner(self.host, _const.ALLOWED_EXTENSIONS)
        self.duplicates = _duplicates.DuplicateFinder(_cancel.check_cancel)
        self.similar = _similar.SimilarFinder(self, self.hashes, _cancel.check_cancel)
        self.pipeline = _pipeline.ScanPipeline(
            self.name, self.scanner, self.duplicates, self.similar, self.results,
            self.hashes, _const.SCAN_MODES)
        self.scans = _background.ScanTaskRunner(
            'image-cleaner-scan', self.pipeline, self.results,
            _cancel.check_cancel, _cancel.ScanCancelled, lambda: engine_for(self))
        self.fresh = _freshness.FreshnessProvider(
            self.host, self.hashes, self.results, self.pipeline,
            _const.ALLOWED_EXTENSIONS, lambda: engine_for(self))

    def _get_host(self) -> Any:
        """宿主实例（`image-viewer`）；未加载时抛错。

        返回类型标 `Any`：宿主在 `PluginBase` 之外还注册了 `list_roots` / `delete_files`，
        按实例取用时的具体形状不是本插件声明的；入口只在这一处承担这个接缝。
        """
        if self._host is None:
            self._host = self.get_dependency('image-viewer')
            if self._host is None:
                raise RuntimeError('image-cleaner 需要 image-viewer 插件已加载并声明依赖')
        return self._host

    def _host_root_listings(self):
        """宿主的根目录清单；旧版宿主没有 `list_roots()` 时返回 None（按单根处理）。

        `list_roots` 是宿主后来才加的能力，所以这里按"可能不存在"取。这个取法留在入口，
        因为它按名字从宿主实例取成员：`tools/check_plugins.py` 的附属插件契约检查要看得见。
        """
        lister = getattr(self._get_host(), 'list_roots', None)
        return lister() if callable(lister) else None

    def register_api(self) -> dict:
        # `get_settings` / `save_settings` **必须登记**：壳的设置弹窗（宿主渲染那条与
        # 本页回落那条）走的都是 `Bridge.call('get_settings')` / `Bridge.call('save_settings')`
        # → `POST /api/image-cleaner__<方法>`，而 `PluginManager` 只会额外登记
        # `<插件>__get_settings_schema`。漏登记的表现是"弹窗打得开、root_dir 信息行空白、
        # 阈值恒为默认值、保存失败"，且前端全程无报错。
        return {
            'duplicate_scan': self.pipeline.duplicate_scan,
            'similar_scan': self.pipeline.similar_scan,
            # scan_* / *_scan：只重算单一模式的兼容入口，两个前端页面都不调用
            # （它们的扫描入口是共享组件上的「校验」）。
            'scan_start': self.scans.start,
            'scan_status': self.scans.status,
            'scan_cancel': self.scans.cancel,
            'get_cached_scan': self.results.get_cached_scan,
            'delete_files': self.delete_files,
            'get_status': self.get_status,
            'get_settings': self.get_settings,
            'save_settings': self.save_settings,
        }

    def get_status(self) -> Dict:
        roots = [str(root) for root, _prefix in self.host.roots()]
        return {
            'host': self.host.name(),
            'root_dir': roots[0] if roots else str(self.host.data_root()),
            'roots': roots,
            'scope': 'all',
        }

    def get_settings(self) -> Dict:
        """统一设置 + 只读信息行「相册根目录」。

        根目录不落在本插件的设置里 —— 它就是宿主 image-viewer 的根目录（主目录 + 额外
        图片目录），所以每次现取：用户在图片相册里改了根，设置弹窗里这一行跟着变。
        多根时逐行列出（`.field-info` 允许换行）。宿主不可用时留空字符串（信息行空着，
        但不能因此让整个设置弹窗打不开）。
        """
        settings = super().get_settings()
        try:
            settings['root_dir'] = '\n'.join(
                str(root) for root, _prefix in self.host.roots())
        except RuntimeError:
            settings['root_dir'] = ''
        return settings

    # ===== 壳按名调用的钩子 =====

    def get_data_root(self) -> Path:
        return self.host.data_root()

    def get_file_roots(self) -> List[Path]:
        return self.host.file_roots()

    def ensure_thumb(self, rel_path: str) -> None:
        self.host.ensure_thumb(rel_path)

    def get_thumb_data(self, rel_path: str):
        return self.host.thumb_data(rel_path)

    def delete_files(self, rel_paths: List[str]) -> Dict:
        """复用 image-viewer 的删除接口，保持路径安全与宿主缓存清理一致。

        删掉的是宿主的图片根里的文件，宿主的 `delete_files` 只清列表缓存与缩略图
        （相册树与目录快照仍按自己的 TTL 报旧计数），所以转发成功之后要显式通知
        宿主失效派生数据 —— 否则删完回到相册页，卡片数与封面还是旧的。
        """
        result = self._get_host().delete_files(rel_paths)
        if isinstance(result, dict) and result.get('deleted'):
            self._notify_host_derived()
        return result

    def _notify_host_derived(self) -> None:
        """通知宿主「刚删掉了你根目录里的文件」。

        宿主契约是 `invalidate_derived(reason)`；旧宿主没有这个方法时用 `getattr`
        跳过。这条通知只影响宿主的缓存，异常一律吞掉 —— 文件已经删掉，不能因为
        通知失败把结果报成失败。
        """
        try:
            invalidate = getattr(self._get_host(), 'invalidate_derived', None)
            if callable(invalidate):
                invalidate('image-cleaner:delete')
        except Exception as error:      # 通知是尽力而为：宿主怎么坏都不影响删除结果
            log.warning('[image-cleaner] 通知宿主失效派生数据失败: %s', error)

    def get_extensions(self) -> List[dict]:
        """注册到 image-viewer 左侧栏的通用扩展入口（条目数据在 const.py）。"""
        return _const.extension_entries()

    def freshness_spec(self) -> Dict:
        return self.fresh.spec()

    def on_settings_changed(self, changed_keys) -> None:
        """阈值变化 → 相似分组必须重算（否则改完阈值看到的还是旧分组）。"""
        if 'threshold' in changed_keys:
            self.results.mark_stale('similar')
            self.results.persist_stale()
        self.fresh.reset_throttle()

    def on_unload(self) -> None:
        """进程退出收尾：取消正在跑的扫描任务（已写出的结果缓存保留）。"""
        self.scans.shutdown()
