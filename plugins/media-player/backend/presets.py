"""均衡器预设的读写（`plugins/media-player/eq-presets/*.json`）。

无状态：预设目录由入口按插件位置算好注入，这里只负责列出与保存。

写文件名要把用户输入清洗成安全名字（只留字母数字与 `_- `，空名退回 `custom`）：
预设目录在插件自身目录下，一个 `../` 就能把"保存预设"变成任意路径写文件。
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

#: 预设文件名的长度上限（够区分自定义预设，又不至于撞上路径长度限制）。
NAME_MAX = 40
#: 清洗后为空时的兜底文件名。
FALLBACK_NAME = 'custom'


class EqPresets:
    """`eq-presets` 目录的列出与保存。"""

    def __init__(self, presets_dir: Path) -> None:
        self._dir = presets_dir

    def list_all(self) -> List[Dict]:
        if not self._dir.exists():
            return []
        presets = []
        for path in sorted(self._dir.glob('*.json')):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                data['id'] = path.stem
                presets.append(data)
            except Exception:
                pass
        return presets

    def save(self, name: str = '', bands: Optional[list] = None) -> dict:
        if not name or not bands:
            return {'success': False, 'error': '缺少参数'}
        self._dir.mkdir(parents=True, exist_ok=True)
        safe_name = ''.join(char for char in name
                            if char.isalnum() or char in '_- ')[:NAME_MAX].strip().replace(' ', '_')
        if not safe_name:
            safe_name = FALLBACK_NAME
        target = self._dir / f'{safe_name}.json'
        target.write_text(json.dumps({'name': name, 'bands': bands},
                                     ensure_ascii=False, indent=2), encoding='utf-8')
        return {'success': True, 'id': safe_name, 'name': name, 'bands': bands}
