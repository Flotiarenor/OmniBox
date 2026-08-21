"""下载任务状态持久化。"""

import json
import os
from typing import Dict


def save_tasks(tasks: Dict[str, object], state_file: str, logger=None) -> None:
    """把任务字典写入隐藏状态目录。"""
    try:
        state = {tid: task.to_state_dict() for tid, task in tasks.items()}
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        if logger:
            logger.error(f'保存下载状态失败: {exc}')


def load_tasks(state_file: str, task_cls, logger=None) -> Dict[str, object]:
    """从隐藏状态目录恢复任务；文件不存在或损坏时返回空字典。"""
    if not os.path.exists(state_file):
        return {}
    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return {}
        tasks = {}
        for tid, data in state.items():
            if not isinstance(data, dict) or 'id' not in data or 'album_id' not in data:
                continue
            try:
                tasks[tid] = task_cls.from_state_dict(data)
            except Exception:
                continue
        return tasks
    except Exception as exc:
        if logger:
            logger.error(f'加载下载状态失败: {exc}')
        return {}
