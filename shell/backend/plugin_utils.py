"""插件后端共享工具。"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_sibling(module_file: str, name: str, namespace: str) -> ModuleType:
    """加载与 module_file 同目录的 Python 模块。

    插件入口通常由 PluginManager 通过 importlib 直接加载，普通相对导入
    在该场景下不可靠。此函数统一使用 importlib 显式加载 sibling 模块。

    参数：
        module_file: 调用方模块文件路径，通常传 __file__。
        name: sibling 模块名（不含 .py）。
        namespace: 模块命名空间，用于避免不同插件的 sibling 重名。
    """
    sibling = Path(module_file).with_name(f'{name}.py')
    if not sibling.is_file():
        raise ImportError(f'插件 sibling 模块不存在: {sibling}')

    module_name = f'{namespace}_{name}'
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached

    spec = importlib.util.spec_from_file_location(module_name, str(sibling))
    if spec is None or spec.loader is None:
        raise ImportError(f'无法为插件 sibling 模块创建加载器: {sibling}')

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module
