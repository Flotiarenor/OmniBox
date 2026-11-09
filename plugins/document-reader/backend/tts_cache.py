"""朗读音频缓存：位置、命中判定、落盘路径与容量清理。

缓存放在 `<config>/plugins/<插件名>/tts/`：
  * 跟其它状态放一起（`<config>` 本来就是本机状态的家），容易找到与清理；
  * 文档根目录换成外接盘/网络盘时，缓存不会散落在各个盘上；
  * `<config>` 已被 .gitignore 与打包门禁排除，不会混进产物。
不放在文档目录的 `.document_state/` 里：那里埋得深、跟着文档根到处跑，
用户想清缓存得先知道根目录在哪。

统一口径：本组件的所有路径参数与返回值都是 `Path`（"缓存目录是 Path、比较用 `str`"
是历史上那 6 条类型错误的来源）。状态自持：缓存目录与体积上限都在这里。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.paths import get_plugins_config_dir

log = logging.getLogger(__name__)

_AUDIO_EXTS = ('.mp3', '.wav', '.ogg', '.opus', '.m4a')
# 缓存键形状：前端给的是文本指纹（SHA-1 前 32 位十六进制；没有 SubtleCrypto 时
# 是 `f<hex>`）。它会被拼成**文件名前缀**，所以只能接受十六进制 —— 一旦放开字符集，
# 一个 `../../../../tmp/x` 就是"以插件权限往任意目录写文件"（见 prefix）。
_CACHE_KEY_RE = re.compile(r'^[0-9a-fA-F]{1,64}$')


class TtsCache:
    """朗读音频与同名 `.json` 元数据的缓存目录。"""

    def __init__(self, subdir: str, limit_bytes: int, plugin_name: str) -> None:
        self._subdir = subdir
        self._limit_bytes = limit_bytes
        self._plugin_name = plugin_name

    # ===== 目录 =====

    def cache_dir(self) -> Path:
        """朗读缓存目录：`<config>/plugins/<插件名>/tts`。

        **纯路径计算，不建目录、不落盘**：只读的调用方（`get_file_roots()` /
        `info()`）不该因为问一次路径就造出一个空目录。需要写入的地方
        （`TtsService.speak`）自己 `mkdir`。
        """
        return get_plugins_config_dir() / self._subdir / 'tts'

    def cache_dir_for_write(self) -> Path:
        """取可写的缓存目录，必要时创建；`<config>` 不可写时退到系统临时目录。"""
        cache_dir = self.cache_dir()
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning(f'[{self._plugin_name}] 朗读缓存目录不可写（{e}），改用临时目录')
            cache_dir = (Path(tempfile.gettempdir()) / 'omnibox-tts'
                         / self._subdir / 'tts')
            cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def file_root(self) -> Optional[Path]:
        r"""`/file` 允许根里的朗读缓存父目录；解析失败返回 None。

        音频是经 `/file` 交给 `<audio>` 播放的，不列进允许根就 403。缓存目录在
        `<config>/plugins/<插件名>/tts/`，那里受保护的只有插件设置文件本身，
        音频不受影响。
        """
        try:
            return self.cache_dir().parent
        except Exception as e:
            # 缓存目录解析失败不该让整个文件路由失效：正文里的图片仍然要能取
            log.warning(f'[{self._plugin_name}] 朗读缓存根无法解析，/file 将不含它: {e}')
            return None

    # ===== 缓存管理（给界面用：用户要能看到它在哪、多大、能不能一键清） =====

    def info(self) -> Dict[str, Any]:
        """朗读缓存的位置与占用。界面把它显示出来，用户不必自己去翻目录。"""
        cache_dir = self.cache_dir()
        files = 0
        total = 0
        try:
            for entry in os.scandir(cache_dir):
                if entry.is_file():
                    files += 1
                    total += entry.stat().st_size
        except OSError:
            pass
        return {'path': str(cache_dir), 'files': files,
                'bytes': total, 'limitBytes': self._limit_bytes}

    def clear(self) -> Dict[str, Any]:
        """清空朗读缓存。缓存按需重建，删掉不影响书签与阅读进度。

        "目录还不存在"不是错误 —— 从没合成过就是这状态（清理是幂等的）。
        """
        cache_dir = self.cache_dir()
        if not cache_dir.is_dir():
            return {'success': True, 'removed': 0, 'bytes': 0}
        removed = 0
        total = 0
        for entry in os.scandir(cache_dir):
            if not entry.is_file():
                continue
            try:
                size = entry.stat().st_size
                os.remove(entry.path)
                removed += 1
                total += size
            except OSError:
                continue
        log.info(f'[{self._plugin_name}] 已清除朗读缓存 {removed} 个文件 / {total / 1024 / 1024:.1f} MB')
        return {'success': True, 'removed': removed, 'bytes': total}

    # ===== 文件名与落盘路径 =====

    @staticmethod
    def prefix(text: str, cache_key: str, params: Dict[str, Any]) -> str:
        """缓存文件名前缀：内容哈希 + 参数指纹。

        指纹里带上引擎/音色/语速/端点 —— 换了任何一个都该重新合成，否则用户切换后
        听到的还是上一种声音（缓存"命中"得太成功）。

        `cache_key` 由前端给，但**请求体里的东西一律不可信**：它只接受十六进制
        指纹（`_CACHE_KEY_RE`），形状不符就回落到按文本现算。历史缺口：它被当成
        文件名前缀直接拼进 `os.path.join`，于是 `cache_key='../../../../tmp/x'`
        就能把音频与元数据写到缓存目录之外（审计项 P1-3）。
        """
        raw_key = str(cache_key or '')
        if not _CACHE_KEY_RE.match(raw_key):
            raw_key = hashlib.md5(text.encode('utf-8')).hexdigest()
        key = raw_key[:32]
        stamp = hashlib.md5(
            f"{params['mode']}|{params['voice']}|{params['rate']}|{params['base_url']}"
            .encode('utf-8')).hexdigest()[:8]
        return f'{key}-{stamp}'

    @staticmethod
    def path(cache_dir: Path, name: str) -> Optional[Path]:
        """拼出缓存文件路径；结果不在 `cache_dir` 之内时返回 None。

        纵深防御：前缀已经做过字符白名单，这一层保证"即使以后有人放宽了白名单，
        落盘位置也不会跑出缓存目录"。同一判定也用在 .json 元数据上。
        """
        try:
            base = cache_dir.resolve()
            target = (cache_dir / name).resolve()
        except (OSError, ValueError):
            return None
        return target if target.is_relative_to(base) else None

    # ===== 命中与元数据 =====

    @staticmethod
    def read_json(path: Path) -> dict:
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def meta_marks(self, prefix: str) -> list:
        meta = self.read_json(self.cache_dir() / (prefix + '.json'))
        return meta.get('marks') or []

    def hit(self, cache_dir: Path, prefix: str) -> Optional[Tuple[Path, dict]]:
        """缓存命中：返回 (音频路径, 元信息)。

        只认音频扩展名 —— 同一把钥匙旁边还躺着 `.json` 元数据文件，用 startswith
        直接取第一个会把元数据当成音频返回（前端拿到噪音）。

        **空文件不算命中**：写盘写了一半就被中断（进程被杀、磁盘满）会留下 0 字节的
        mp3，它会被当成"已缓存"永久命中，表现是这段文字再也读不出声。删掉重新合成。
        """
        try:
            names = os.listdir(cache_dir)
        except OSError:
            return None
        for name in names:
            if not (name.startswith(prefix + '.') and name.lower().endswith(_AUDIO_EXTS)):
                continue
            path = cache_dir / name
            try:
                if path.stat().st_size <= 0:
                    raise OSError('缓存音频为空')
            except OSError as e:
                log.warning(f'缓存音频不可用（{e}），删除后重新合成: {name}')
                for target in (path, cache_dir / (prefix + '.json')):
                    try:
                        if target.exists():
                            target.unlink()
                    except OSError:
                        pass
                return None
            meta = self.read_json(cache_dir / (prefix + '.json'))
            # 路径统一过 path()：它 resolve 到长名，而这里的 cache_dir 可能还是
            # 8.3 短名（Windows 临时目录常见）。不复原成同一种形态，同一份缓存两次朗读
            # 会返回指向同一文件却逐字不同的 URL（首次合成走 path() 是长名，
            # 命中走这里曾是短名）。
            hit_path = self.path(cache_dir, name)
            return (hit_path or path), meta
        return None

    # ===== 容量清理 =====

    def prune(self, cache_dir: Path, keep_bytes: Optional[int] = None) -> None:
        """缓存目录超过上限时，从最旧的开始删（音频 + 时间戳成对删）。

        `ponytail:` 上限是"整目录字节数"，不是"按书/按章"，也没有专门的索引文件 ——
        一次 os.scandir 排序足够；真到了几十万文件再看是否需要数据库。
        """
        limit = self._limit_bytes if keep_bytes is None else keep_bytes
        entries: List[Tuple[float, int, str]] = []
        total = 0
        try:
            for entry in os.scandir(cache_dir):
                if not entry.is_file():
                    continue
                stat = entry.stat()
                total += stat.st_size
                entries.append((stat.st_mtime, stat.st_size, entry.path))
        except OSError:
            return
        if total <= limit:
            return
        entries.sort()
        for _mtime, size, path in entries:
            # 一对文件（音频 + 时间戳）都要删，但**只按音频**扣减总量 ——
            # 时间戳的字节本来就没计入 total，两边都减会让统计偏低、多删一批。
            stem, ext = os.path.splitext(path)
            for target in (path, stem + '.json'):
                try:
                    if os.path.exists(target):
                        os.remove(target)
                except OSError:
                    pass
            if ext.lower() in _AUDIO_EXTS:
                total -= size
            if total <= limit:
                break
        log.info(f'朗读缓存超过上限，已清理到 {total / 1024 / 1024:.0f} MB')
