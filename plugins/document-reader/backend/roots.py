"""roots 分片：文档根目录的解析、切换与旧状态迁移。

从 `main.py` 切出来。这一块只关心"书放在哪、状态目录叫什么、旧数据怎么搬"。

三条约定都是踩过坑写下来的：

* **配置了但暂时不存在的目录要保留**：外接盘没插时把用户的选择丢掉，插上后还要
  重新配一遍。扫描阶段自然会跳过不存在的根。
* **迁移必须早于 makedirs**：新状态目录一旦建出来，旧目录就搬不过去了。
* **改名迁移只在新区没有值时发生**：用户之后自己保存的设置不能被旧值覆盖。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)


class RootsMixin:
    """根目录与状态目录的布局。"""

    CACHE_DIR_NAME = '.document_state'
    # 改名前的名字：只用于把旧数据搬过来，别拿它们当新写入口
    LEGACY_CACHE_DIR = '.novel_state'
    LEGACY_CACHE_FILE = '.novel_cache.json'
    LEGACY_PROGRESS_FILE = '.novel_progress.json'
    # 插件改名：旧名下的设置文件里存着用户配置的 root_dir
    LEGACY_PLUGIN_NAME = 'novel-reader'

    # 子类（main.py 的插件类）提供：_document_cache 等缓存字典、_load_cache()、
    #   on_load() 里的设置注入由基类完成
    document_dir: str
    _cache_dir: str
    _roots: List[str]

    # ===== 文件服务根目录 =====

    def get_data_root(self) -> Path:
        """主目录（第一行）：缓存、EPUB 解包产物与阅读进度都在它下面。"""
        return Path(self.document_dir)

    def get_file_roots(self) -> List[Path]:
        """`/file` 允许访问的根：全部配置目录（EPUB 解包出来的图片按绝对路径引用）。"""
        return [Path(root) for root in self._roots]

    # ===== 根目录解析与切换 =====

    def _parse_roots(self, raw: Any) -> List[str]:
        """把「文档根目录」解析成有序列表。

        值格式与其它插件一致：字符串、每行一个目录（shell 的目录字段 `multi: True`）。
        为空时回落到默认数据目录；配置了但暂时不存在的目录保留着（外接盘没插时不该
        把用户的选择丢掉），扫描阶段自然会跳过它。
        """
        entries = ('\n'.join(str(item) for item in raw)
                   if isinstance(raw, (list, tuple)) else str(raw or ''))
        roots: List[str] = []
        for line in entries.splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                resolved = str(Path(text).expanduser().resolve())
            except (OSError, ValueError):
                log.warning(f'[{self.name}] 忽略无法解析的目录: {text}')
                continue
            if resolved not in roots:
                roots.append(resolved)
        if not roots:
            # 走基类默认数据目录（super() 在这条 MRO 上仍是 PluginBase）
            roots = [str(Path(super().get_data_root()).resolve())]
        return roots

    def _set_roots(self, roots: List[str]) -> None:
        self._roots = roots
        self.document_dir = roots[0]
        self._cache_dir = os.path.join(self.document_dir, self.CACHE_DIR_NAME)
        # 迁移必须早于 makedirs：新目录一旦建出来，旧目录就搬不过去了
        self._migrate_legacy_state()
        os.makedirs(self._cache_dir, exist_ok=True)

    def _migrate_legacy_state(self) -> None:
        """把旧插件名下的状态目录/文件搬到新名。

        状态放在**用户的文档目录**里（`.novel_state/`），改名后新代码找的是
        `.document_state/` —— 不搬就是"升级后所有阅读进度归零"。
        """
        legacy_dir = os.path.join(self.document_dir, self.LEGACY_CACHE_DIR)
        if not os.path.isdir(legacy_dir):
            return
        try:
            if not os.path.isdir(self._cache_dir):
                os.rename(legacy_dir, self._cache_dir)
                log.info(f'[{self.name}] 已迁移状态目录 {self.LEGACY_CACHE_DIR} → '
                         f'{self.CACHE_DIR_NAME}')
            # 目录搬过来后文件名还是旧的，逐个补上（包含解包产物所在子目录，不用动）。
            # 源文件可能在新目录里（刚整体搬过来），也可能还在旧目录里（新目录先建好了）。
            for old_name, new_name in ((self.LEGACY_CACHE_FILE, self.CACHE_FILE),
                                       (self.LEGACY_PROGRESS_FILE, self.PROGRESS_FILE)):
                dst = os.path.join(self._cache_dir, new_name)
                if os.path.exists(dst):
                    continue
                for base in (self._cache_dir, legacy_dir):
                    src = os.path.join(base, old_name)
                    if os.path.isfile(src):
                        os.replace(src, dst)
                        break
        except OSError as e:
            # 迁移失败只影响缓存与进度，不该让插件加载不了
            log.warning(f'[{self.name}] 旧状态目录迁移失败: {e}')
            return
        # 搬空的旧目录不该继续留在用户的文档目录里
        try:
            if os.path.isdir(legacy_dir) and not os.listdir(legacy_dir):
                os.rmdir(legacy_dir)
        except OSError:
            pass

    def _apply_root_dir(self, raw_dir: Any) -> None:
        """切换根目录：所有缓存都按"根内相对路径"为键，换根必须整体失效。"""
        self._set_roots(self._parse_roots(raw_dir))
        self._document_cache = {}
        self._chapter_cache = {}
        self._offset_cache = {}
        self._full_content_cache = {}
        self._doc_cache = {}
        self._marks_cache = None
        self._load_cache()

    def on_settings_changed(self, changed_keys) -> None:
        if 'root_dir' in changed_keys:
            self._apply_root_dir(self.setting('root_dir'))

    def on_load(self) -> None:
        """改名迁移：旧插件名（novel-reader）的设置文件里存着用户配置的 root_dir。

        构造期 `_settings_store` 尚未注入（`setting()` 那时只能读到壳预解析的
        `_resolved_config`），所以迁移放在 on_load：先补写新名下的设置，再按它重建根目录。
        迁移只在新区没有 root_dir 时发生，用户之后自己保存的设置不会被覆盖。
        """
        if self._settings_store is None or str(self.setting('root_dir') or '').strip():
            return
        try:
            legacy: Dict[str, Any] = self._settings_store.get(self.LEGACY_PLUGIN_NAME) or {}
        except (TypeError, ValueError) as e:
            log.warning(f'[{self.name}] 读取旧插件名下的设置失败: {e}')
            return
        root_dir = str(legacy.get('root_dir') or '').strip()
        if not root_dir:
            return
        self.update_setting('root_dir', root_dir)
        log.info(f'[{self.name}] 已从 {self.LEGACY_PLUGIN_NAME} 的设置迁移 root_dir')
        self._apply_root_dir(root_dir)
