"""Pixiv 同步插件内部实现（入口 backend/main.py 的组件与底层模块）。

分层：
- 入口 backend/main.py 只做装配、API 表与壳钩子的一行委派；
- 组件（自带状态，构造时显式收依赖）：paths / session / records / limits /
  workspace / library / selection / runner / maintenance / status；
- 底层模块（无插件状态，可被组件与用例直接用）：limiter / tasks / store / db /
  artist / scan_items / scan / bookmarks / download / ugoira / oauth；
- 纯数据：const（设置 schema、扩展入口声明）；
- 伴侣命令行工具：pixiv_purge_non_original / pixiv_unmark_recent，共用的落盘与
  根目录定位在 cli_support。

manifest.libs 声明了 backend 与 backend/libs，入口因此可以按真包 import 取本包与
vendored 的 pixiv_mini。
"""
