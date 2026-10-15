# shell/backend

OmniBox 的 Python 后端：Flask 应用（`create_app()`）与插件契约。
根 [AGENTS.md](../../AGENTS.md) 的全局约定仍然适用。

## 模块地图

按"一次请求从壳到插件再回来"的顺序：

| 模块 | 职责 | 关键符号 |
| --- | --- | --- |
| `paths.py` | 用户数据 / 配置 / 插件搜索目录的唯一解析处 | `is_frozen` `get_user_data_dir` `get_config_dir` `get_plugin_search_dirs` `resolve_data_root` |
| `plugin_manager.py` | 发现并加载插件，收集受保护路径 | `PluginManager` `collect_protected_paths` |
| `plugin_base.py` | 插件基类与设置模式下的密钥处理 | `PluginBase` `secret_keys` `admin_only_keys` `mask_secrets` |
| `plugin_utils.py` | 插件同目录分片模块的加载 | `load_sibling` |
| `file_server.py` | Flask 路由、静态资源、状态页 | `create_app` `guard_admin_methods` `serve_shell_assets` `_status_response` |
| `media_catalog.py` | 统一媒体目录浏览契约（路径安全、面包屑、扩展名分类） | `is_safe_path` `resolve_safe_path` `classify_file` `list_directory` `list_system_roots` |
| `freshness.py` | 同步 / 校验引擎：插件声明规格，壳侧驱动 | `FreshnessSpec` `FreshnessStore` `FreshnessEngine` `FsSource` `engine_for` |
| `thumb_cache.py` | 缩略图缓存与维护 | `ThumbCache` `cache_stats` `clear_all_caches` |
| `tasks.py` | 后台任务 | `BackgroundTask` |
| `settings_store.py` | 每插件一个 JSON 的设置存储 | `SettingsStore` |
| `shell_prefs.py` `shell_info.py` | 壳的外观偏好与运行期信息 | `ShellPrefs` `ShellInfo` `configure_webview_environment` |
| `auth.py` `principal.py` | 访问令牌与主体权限 | `get_or_create_token` `PrincipalStore` `require_admin` `PrincipalScope` |
| `protected_paths.py` | 受保护路径的归一化与匹配 | `normalize` `matches` `is_protected` |
| `app_logging.py` | 日志与轮转 | `SafeRotatingFileHandler` `setup_logging` |
| `window_state.py` | 窗口状态（全屏等） | `get_state` `set_fullscreen` |
| `webview_gpu.py` | 注入 WebView2 的 GPU 偏好 | `apply_gpu_preference` `install` |
| `file_dialog.py` | 原生文件 / 目录选择 | `pick_path` |

入口 `main.py` 在仓库根目录，负责启动与装配，不在本目录。

## 规则

- **动态导入必须登记**：插件后端由运行时 `importlib` 加载，它 import 的每个 `shell.backend.*` 与第三方模块都要写进 `docs/Releases/spec_common.py` 的 `HIDDEN_IMPORTS`，第三方包同时写进 `requirements.txt`。判定与门禁见 [docs/Releases/AGENTS.md](../../docs/Releases/AGENTS.md)。
- **用户数据只写在可执行文件所在目录**（由 `paths.py` 决定）：在产物目录运行过程序后，不要直接压缩发布。
- **设置存储只走原子写入**：`SettingsStore` 用 `tempfile` + `fsync` + `os.replace`，不要改回 `open(w)` 直接覆写。
- **静态资源的解析顺序属前端**：见 [shell/frontend/AGENTS.md](../frontend/AGENTS.md)。

## 先读

改某个模块前，先读它顶部的 docstring：它为什么存在写在代码里，不是写在文档里。
