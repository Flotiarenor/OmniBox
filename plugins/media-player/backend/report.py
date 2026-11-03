"""缺失曲目清单导出：把「在线歌单里本地缺失的曲目」写成文本清单，供补档。

无状态：条目来自 `index.items`，落点由 `paths.scan_roots` 决定。

**每次整体重写**（不是追加）：清单要能反映最近一次同步后的真实状态，否则把缺的歌补齐
后旧清单还挂在那里，看着像没同步。没有缺失时也照样写一份（正文写「没有缺失」），
不留着上一次的旧内容。
"""

import re
import time
from pathlib import Path
from typing import Any, List, Optional

#: 标题拼进文件名之前的长度上限（够长，又不会撞上路径长度限制）。
TITLE_MAX = 60
#: 清单默认标题（前端不传时的全量清单名）。
DEFAULT_TITLE = '网易云缺失曲目'


class MissingReport:
    """把缺失曲目写成媒体根下的文本文件。"""

    def __init__(self, paths: Any, index: Any) -> None:
        self._paths = paths
        self._index = index

    def report_root(self) -> Path:
        """清单落点：音频最多的媒体根（补档清单要和音乐放一起）。

        「数据根」不一定是音乐根 —— 多根配置下第一行可能是视频盘。按谁的音频条目多来挑，
        挑不出来才退回数据根。
        """
        best: Optional[Path] = None
        best_count = 0
        roots = list(self._paths.scan_roots) or [self._paths.root_dir]
        for root in roots:
            count = 0
            for item in self._index.items.values():
                if item.kind != 'audio':
                    continue
                try:
                    if Path(item.path).is_relative_to(root):
                        count += 1
                except (OSError, ValueError):
                    continue
            if count > best_count:
                best, best_count = root, count
        return best or self._paths.root_dir

    def export(self, lines: Optional[List[str]] = None, title: str = DEFAULT_TITLE) -> dict:
        """写一份清单：文件名固定（标题去路径字符后当名字）、内容逐行清洗。

        只写进自己的媒体根：这是个结果导出，不该变成一个能写任意路径 / 任意内容的
        文件接口。
        """
        clean = [re.sub(r'\s+', ' ', str(line or '')).strip() for line in (lines or [])]
        clean = [line for line in clean if line]
        body = clean or ['（本次同步没有缺失曲目）']
        safe_title = re.sub(r'[\\/:*?"<>|\r\n]', '', str(title or '')).strip()[:TITLE_MAX] \
            or DEFAULT_TITLE
        target = self.report_root() / f'{safe_title}.txt'
        header = f'# {safe_title}\n# 生成时间：{time.strftime("%Y-%m-%d %H:%M:%S")}\n'
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(header + '\n'.join(body) + '\n', encoding='utf-8')
        except OSError as error:
            return {'success': False, 'error': f'写入失败: {error}'}
        return {'success': True, 'path': str(target), 'count': len(clean)}
