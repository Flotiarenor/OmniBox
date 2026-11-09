"""歌词读取：同目录同名 `.lrc` 文件。

无状态：条目来自 `index`，每次按 `item.path` 现算 `lrc` 路径。编码按 utf-8 / gbk /
gb2312 依次尝试 —— 本地歌词文件这三种都常见，读不出来就当没有，不让接口报错。
"""

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: 尝试顺序：先 UTF-8（现代文件），再中文 Windows 常见的 GBK / GB2312。
ENCODINGS = ('utf-8', 'gbk', 'gb2312')


class LyricsReader:
    """读同目录 `.lrc`。"""

    def __init__(self, index: Any) -> None:
        self._index = index

    def get(self, item_id: str) -> dict:
        item = self._index.get(item_id)
        if item is None:
            return {'lyrics': '', 'source': 'none'}
        lrc_path = Path(item.path).with_suffix('.lrc')
        if not lrc_path.exists():
            return {'lyrics': '', 'source': 'none'}
        for encoding in ENCODINGS:
            try:
                return {'lyrics': lrc_path.read_text(encoding=encoding), 'source': 'lrc'}
            except (UnicodeDecodeError, UnicodeError):
                continue
            except Exception:
                break
        return {'lyrics': '', 'source': 'none'}
