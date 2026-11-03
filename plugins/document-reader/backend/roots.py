"""文档根目录的解析、切换与旧状态迁移。

这一块只关心"书放在哪、状态目录叫什么、旧数据怎么搬"。状态自持：根列表、主目录与
状态目录都在这里，换根走 `rebind()`，由入口编排；其它组件不再随手改别人身上的路径。

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
from typing import Any, Callable, List, Optional

from shell.backend.ports import SettingsPort

log = logging.getLogger(__name__)


class RootLayout:
    """文档根目录与状态目录的布局。

    `names` 是 `const.LayoutNames` 的实例（目录名、文件名、旧名、缓存版本），
    `default_root` 是入口给的闭包 —— 没有配置根目录时回落到基类的默认数据目录。
    """

    def __init__(self, settings: SettingsPort, default_root: Callable[[], Path],
                 names: Any, plugin_name: str) -> None:
        self._settings = settings
        self._default_root = default_root
        self._names = names
        self._plugin_name = plugin_name
        self.roots: List[str] = []
        self.document_dir = ''
        self.cache_dir = Path()
        self.rebind(settings.setting('root_dir'))

    # ===== 文件服务根目录 =====

    def data_root(self) -> Path:
        """主目录（第一行）：缓存、EPUB 解包产物与阅读进度都在它下面。"""
        return Path(self.document_dir)

    def file_roots(self, extra: Optional[Path]) -> List[Path]:
        r"""`/file` 允许访问的根。

        除了全部配置的文档目录（EPUB 解包出来的图片按绝对路径引用），还要包含
        **朗读缓存目录**（传入的 `extra`，即 `<config>/plugins/<插件名>`）—— 音频是
        经 `/file` 交给 `<audio>` 播放的，不列进来就 403。

        **不要用 is_dir() 过滤**：只读调用不会创建缓存目录（见 `TtsCache.cache_dir`），
        首次合成前它并不存在 —— 过滤掉就会让"合成成功后音频仍取不回来"（403）。
        """
        roots = [Path(root) for root in self.roots]
        if extra is not None and extra not in roots:
            roots.append(extra)
        return roots

    # ===== 根目录解析与切换 =====

    def parse(self, raw: Any) -> List[str]:
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
                log.warning(f'[{self._plugin_name}] 忽略无法解析的目录: {text}')
                continue
            if resolved not in roots:
                roots.append(resolved)
        if not roots:
            roots = [str(Path(self._default_root()).resolve())]
        return roots

    def rebind(self, raw: Any) -> None:
        """按设置值重建根目录与状态目录（入口在构造与设置变更时调用）。"""
        self.roots = self.parse(raw)
        self.document_dir = self.roots[0]
        self.cache_dir = Path(self.document_dir) / self._names.cache_dir
        # 迁移必须早于 makedirs：新目录一旦建出来，旧目录就搬不过去了
        self._migrate_legacy_state()
        os.makedirs(self.cache_dir, exist_ok=True)

    def cache_path(self, name: str) -> Path:
        """状态目录里的一个文件（缓存 / 进度 / 书签都从这里派生）。"""
        return self.cache_dir / name

    def extract_dir(self, document_id: str) -> Path:
        """EPUB 内嵌图片的解包目录（目录名用哈希，书名里的字符不进路径）。"""
        import hashlib
        return self.cache_dir / 'extract' / hashlib.md5(document_id.encode('utf-8')).hexdigest()

    def _migrate_legacy_state(self) -> None:
        """把旧插件名下的状态目录/文件搬到新名。

        状态放在**用户的文档目录**里（`.novel_state/`），改名后新代码找的是
        `.document_state/` —— 不搬就是"升级后所有阅读进度归零"。
        """
        names = self._names
        legacy_dir = Path(self.document_dir) / names.legacy_dir
        if not legacy_dir.is_dir():
            return
        try:
            if not self.cache_dir.is_dir():
                os.rename(legacy_dir, self.cache_dir)
                log.info(f'[{self._plugin_name}] 已迁移状态目录 {names.legacy_dir} → '
                         f'{names.cache_dir}')
            # 目录搬过来后文件名还是旧的，逐个补上（包含解包产物所在子目录，不用动）。
            # 源文件可能在新目录里（刚整体搬过来），也可能还在旧目录里（新目录先建好了）。
            for old_name, new_name in ((names.legacy_cache_file, names.cache_file),
                                       (names.legacy_progress_file, names.progress_file)):
                dst = self.cache_path(new_name)
                if dst.exists():
                    continue
                for base in (self.cache_dir, legacy_dir):
                    src = base / old_name
                    if src.is_file():
                        os.replace(src, dst)
                        break
        except OSError as e:
            # 迁移失败只影响缓存与进度，不该让插件加载不了
            log.warning(f'[{self._plugin_name}] 旧状态目录迁移失败: {e}')
            return
        # 搬空的旧目录不该继续留在用户的文档目录里
        try:
            if legacy_dir.is_dir() and not os.listdir(legacy_dir):
                os.rmdir(legacy_dir)
        except OSError:
            pass


def migrate_legacy_root_dir(settings: SettingsPort, store: Any, plugin_name: str,
                            legacy_name: str,
                            update_setting: Callable[[str, Any], bool]) -> Optional[str]:
    """把旧插件名（novel-reader）设置里的 `root_dir` 迁到新名下。

    构造期壳还没有注入 `_settings_store`（`setting()` 那时只能读到启动时预设），
    所以迁移放在 `on_load`：先补写新名下的设置，再由入口按它重建根目录。
    迁移只在新区没有值时发生，用户之后自己保存的设置不会被覆盖。

    返回迁移过来的 root_dir；没有可迁移的值时返回 None。
    """
    if store is None or str(settings.setting('root_dir') or '').strip():
        return None
    try:
        legacy = store.get(legacy_name) or {}
    except (TypeError, ValueError) as e:
        log.warning(f'[{plugin_name}] 读取旧插件名下的设置失败: {e}')
        return None
    root_dir = str(legacy.get('root_dir') or '').strip()
    if not root_dir:
        return None
    update_setting('root_dir', root_dir)
    log.info(f'[{plugin_name}] 已从 {legacy_name} 的设置迁移 root_dir')
    return root_dir
