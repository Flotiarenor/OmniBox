"""Pixiv 同步插件内部实现（由 backend/main.py 拆分而来，放在 backend/pixiv_sync/ 包）。

各模块：limiter/tasks/store/db/artist/scan/download/oauth。
插件入口 backend/main.py 内的 PixivSyncPlugin 负责线程编排、设置与 API 挂载，
具体逻辑分散在本包各模块中。manifest.libs 已声明本目录，可被入口 import。
"""