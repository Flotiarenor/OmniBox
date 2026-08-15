'''
Copyright 2026 flotiarenor

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

"""
backend 包 - 插件化后端核心

提供：
- PluginBase : 插件抽象基类
- PluginManager : 插件发现、依赖解析、加载与 API 注册
- create_app   : Flask 应用工厂（位于 file_server 模块）

典型用法:
    from shell.backend import PluginManager, create_app
    from shell.backend.paths import get_plugin_search_dirs

    manager = PluginManager([str(p) for p in get_plugin_search_dirs()], config=config)
    manager.load_all()
    app = create_app(config, manager)
    app.run()
"""

from .plugin_base import PluginBase
from .plugin_manager import PluginManager
from .file_server import create_app

__all__ = [
    "PluginBase",
    "PluginManager",
    "create_app",
]