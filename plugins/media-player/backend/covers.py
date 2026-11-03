"""封面库：按条目 id 读写 ThumbCache，供壳的 `/thumbs` 与前端抽帧回写。

状态自持：ThumbCache 句柄与它的落点（媒体根下 `.cache/thumbs.db`）。音频内嵌封面 /
目录封面图由 `scanner.cover_generator` 懒生成；视频封面由前端 canvas 抽帧后经 `put()`
回写（不经过生成器）。mtime/size 失效自动重生成，不再散落 `.cache/covers`。
"""

import base64
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shell.backend.thumb_cache import ThumbCache

log = logging.getLogger(__name__)

#: 前端回写封面的体积上限（data URL 里 base64 部分的字节数，约 2MB 图片数据）。
MAX_PUT_BASE64 = 3 * 1024 * 1024

#: 封面统一尺寸：前端按 1:1 卡片显示，源图可能是任意长宽比。
COVER_SIZE = (640, 640)


class CoverStore:
    """`ThumbCache` 的插件侧门面（按 item id 取 / 存封面）。"""

    def __init__(self, paths: Any, index: Any, generator: Any,
                 workers: int = 3) -> None:
        self._paths = paths
        self._index = index
        self._generator = generator
        self._workers = workers
        self.cache = self._open()

    def _open(self) -> ThumbCache:
        return ThumbCache(self._paths.thumb_db, size=COVER_SIZE,
                          generator=self._generator, workers=self._workers)

    def rebind(self) -> None:
        """换数据根后重开封面库（由入口编排调用）。"""
        self.cache = self._open()

    # ===== 读 =====

    def get_thumb_data(self, rel_path: str) -> Optional[Tuple[bytes, str]]:
        """`/thumbs` 路由：按 item id 返回封面 (data, mime)；未索引/生成失败返回 None。"""
        item = self._index.get(rel_path)
        if item is None or not item.path:
            return None
        return self.cache.get(rel_path, Path(item.path))

    def missing(self, item_ids: Optional[List[str]] = None) -> Dict:
        """批量查询哪些条目的封面尚未缓存（供前端按浏览位置预取，避免重复抽帧）。

        单连接批量判定（ThumbCache.has_many），避免每个 id 一次 SQLite 建连。
        """
        items: List[Tuple[str, Path]] = []
        for item_id in (item_ids or [])[:200]:
            item = self._index.get(item_id)
            if item is None or not item.path:
                continue
            items.append((item_id, Path(item.path)))
        return {'missing': self.cache.has_many(items)}

    # ===== 写 =====

    def put(self, item_id: str = '', data_url: str = '') -> dict:
        """前端 canvas 抽帧回写封面（data URL → ThumbCache，不经过生成器）。

        仅接受 data:image/ 前缀，限制体积防滥用；源文件 mtime/size 一并记录，
        文件替换后条目自动失效（前端会重新抽帧）。
        """
        if not item_id or not data_url or not data_url.startswith('data:image/'):
            return {'success': False, 'error': '参数无效'}
        item = self._index.get(item_id)
        if item is None or not item.path:
            return {'success': False, 'error': '媒体不存在'}
        try:
            header, _, b64 = data_url.partition(',')
            if len(b64) > MAX_PUT_BASE64:
                return {'success': False, 'error': '数据过大'}
            data = base64.b64decode(b64, validate=True)
            if not data:
                return {'success': False, 'error': '数据为空'}
            mime = 'image/png' if 'png' in header else 'image/jpeg'
            self.cache.put(item_id, data, mime, Path(item.path))
            return {'success': True}
        except Exception as error:
            return {'success': False, 'error': f'写入失败: {error}'}

    # ===== 失效（统一刷新基建的 prune / 对账 / 重建用） =====

    def delete(self, key: str) -> None:
        self.cache.delete(key)

    def prune(self, valid_keys: Any) -> int:
        return self.cache.prune(valid_keys)

    def clear(self) -> None:
        self.cache.clear()
