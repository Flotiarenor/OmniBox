# 图片相册插件（image-viewer）设计文档

> 版本：v2.4.3（当前实现）
> 形态：独立宿主插件；`image-cleaner`（相册清理）为其 Companion 插件
> 适用范围：后端 `plugins/image-viewer/backend/`、前端 `plugins/image-viewer/frontend/`

## 1. 定位与功能总览

`image-viewer` 是 OmniBox 的本地图片浏览插件：把磁盘上的图片目录组织成**相册**，提供嵌套相册网格、混合瀑布流、Pixiv 作品排序、时间线、幻灯片与多选管理等能力。

核心功能：

- **相册体系**：递归扫描数据根目录，目录即相册；支持嵌套、收纳（collapse）、提升（promote）
- **混合瀑布流**：一个文件夹内"直接图片 + 直接子相册 p0 瓦片"混合展示，支持连续浏览序列（灯箱向右翻完全部子相册图片）
- **Pixiv 排序支持（time_name）**：作品按前导数字（作品 ID）排序，作品内部按文件名自然序 p0 → p1；两级嵌套语义（配置点 → 作者 → 作品）
- **按需缩略图**：SQLite（`thumbs.db`）缓存 300px 缩略图，mtime/size 失效校验，支持全量/单相册重建
- **缓存体系**：尺寸元数据、相册索引、列表缓存三层缓存，目标是大图库（10 万+ 文件）流畅浏览

## 2. 架构总览

### 2.1 模块划分

```
plugins/image-viewer/
├── manifest.json               # 声明依赖、权限（filesystem:read/write）、路由 /image-viewer
├── backend/
│   ├── main.py                 # ImageViewerPlugin：API 入口、缓存调度、重建任务（BackgroundTask）、设置
│   └── filesystem.py           # 纯函数工具：路径安全、排序、尺寸元数据（无实例状态）
└── frontend/
    ├── index.html              # 侧边栏 + 工具栏 + 弹窗骨架
    ├── image-viewer.css
    └── js/
        ├── app.js              # 主应用（视图状态机、瀑布流、灯箱、多选、设置）
        └── justified-layout.js # Justified 布局计算（纯函数）
```

**职责边界**：

- `main.py` 持有插件实例状态（`_meta_cache` / `_list_cache` / `_album_cache` / `_album_config` / `_rebuild`）、`thumb_cache`（共享基建实例），负责 API 语义与任务调度；
- `filesystem.py` 全部为**无实例状态**的模块级纯函数（通过 `load_sibling` 注入），便于单测与复用；
- 缩略图缓存使用共享基建 `shell/backend/thumb_cache.py`（`ThumbCache`），重建任务使用 `shell/backend/tasks.py`（`BackgroundTask`）——见 `docs/plugin-guide.md` §3.4；
- 前端通过 `Bridge.call(...)` 调用后端，图片/缩略图通过 `/file`、`/thumbs` 路由访问（Shell 提供）。

### 2.2 与 Shell / 其他插件的关系

- **文件服务**：`/thumbs/<path>` 优先调用插件 `get_thumb_data()` 从 SQLite 返回字节；`/file` 走 Shell 通用路由（`get_data_root()` 即安全根目录）
- **Companion**：`image-cleaner` 依赖本插件，复用 `delete_files()`、`ensure_thumb()`、`get_data_root()`，通过 `get_extensions()` 挂载到左侧栏
- **设置**：通过 Shell 统一 `SettingsStore`（`.config/plugins/image-viewer.json`）持久化

### 2.3 浏览数据流

```
打开相册页 ──► list_albums ──► _list_album_dirs(全树目录枚举)
                                  └─► _build_albums(增量扫描变化目录 + 自底向上聚合)
                                       └─► albums_index.json(version 3) 持久化 + 30s TTL 内存缓存

进入文件夹 ──► list_folder_items ──► 直接图片 scandir + 尺寸并行读取
                                      ├─► _scan_album_items(子目录级并行: 封面 p0 + 计数)
                                      └─► items 排序(按生效设置) + all_images 连续序列
                                           └─► _list_cache(目录mtime + 排序键) 内存缓存

渲染瓦片 ──► <img src="/thumbs/..."> ──► get_thumb_data ──► SQLite 命中 / Pillow 生成回写
```

## 3. 缓存体系（核心设计）

### 3.1 尺寸元数据 `image_meta.json`

- 键：`md5(绝对路径)`；值：`{mtime, width, height}`；mtime 不一致即失效重读
- **脏标记**：`_get_image_size()` 包装检测 `len(meta_cache)` 变化置 `_meta_dirty`，仅在有新增条目时落盘（`_flush_meta_if_dirty()`），避免每次翻页全量写盘
- **原子写**：先写 `.json.tmp` 再 `os.replace`，防中途崩溃损坏
- **清理**：`delete_files` / `move_files` / `regenerate_thumbs` 同步 `drop_image_meta`；读取失败**不写缓存**（临时不可读文件不会被永久缓存成 0×0）

### 3.2 缩略图 SQLite `thumbs.db`（共享基建 ThumbCache）

缩略图缓存使用 Shell 共享基建 `shell/backend/thumb_cache.py`（`ThumbCache`，见 `docs/plugin-guide.md` §3.4），插件持有实例 `self.thumb_cache`（`root_dir` 变更时重建）：

- 路径：`<数据根>/.cache/thumbs.db`；表 `thumbs(path PK, source_mtime, source_size, mime, data, created_at)`
- 连接参数：`WAL` + `synchronous=NORMAL` + `timeout=15`
- **失效校验**：`source_mtime`（0.5s 容差）+ `source_size` 双条件，文件替换后自动重生成
- **生成**：Pillow `thumbnail((300,300))`；JPEG quality=85 optimize、WEBP quality=82 method=4；失败返回 None（**不缓存假缩略图**）
- **按需生成**：`/thumbs` 请求未命中才生成回写（`thumb_cache.get()`）；全量重建走 `generate_bulk()`（并行批量，单连接写入）
- **收缩**：`clear()` 先 `wal_checkpoint(TRUNCATE)` 再 `VACUUM`，进程级锁保护（VACUUM 需独占）

### 3.3 相册索引 `albums_index.json`

- 版本号 `version: 3`（封面 = 文件名自然序第一张 p0；旧版按最新 mtime 取封面，作废重扫）
- 内容：`{version, dirs: {rel_path: {mtime, direct_count, direct_cover, has_children, ...}}}`
- **增量**：`list_albums` 全树枚举目录 mtime，仅扫描变化目录（0.5s 容差），自底向上聚合 `image_count / cover / newest`
- **TTL**：`list_albums` 结果 30 秒内存缓存（`_ALBUMS_TTL`），`refresh()` / `rebuild_all` / 相册配置 / 文件夹设置变更时失效（`_invalidate_albums_cache()`）

### 3.4 内存列表缓存 `_list_cache`

- 键：`('items', rel_path, sort_by, sort_order)` 或 `(rel_path, sort_by, sort_order)`；值：`(目录mtime, items, [all_images])`
- 目录 mtime 未变直接命中；上限 `_MAX_LIST_CACHE = 200` 条，超出淘汰最旧一半
- 已知限制：缓存键不含子文件夹设置，子文件夹排序设置变更后 `all_images` 序列可能陈旧（直到父目录 mtime 变化）

## 4. 排序体系

### 4.1 自然排序 `natural_sort_key`

优先使用 `natsort`（venv 依赖），缺失时回退内置 `(\d+)` 分段实现（数字段转 int 比较，其余 lower）。文件 `p0 < p1 < p2 < ... < p10`。

### 4.2 Pixiv 排序 `time_name`

- `pixiv_number(name)`：提取名称**前导数字**（作品 ID / 图片编号），无前导数字返回 None
- `_pixiv_sort`：前导数字条目按数字大小排（方向生效），无数字条目按自然名排并**始终位于最后**
- **两层嵌套语义**（`get_settings` 返回 `pixiv_explicit`）：
  - 配置点（自身显式设置 `time_name`，如 `pixiv/` 主文件夹）→ 显示**子相册网格**（作者卡片）
  - 继承 `time_name` 的子文件夹（作者层）→ 显示**混合瀑布流**（作品 p0 瓦片 + 圆圈数量角标）
  - 作品内部（纯图片文件夹）图片仍按文件名自然序 p0 → p1，不受方向影响
- 卡片 `use_time_name` 标记瓦片是否启用角标（后端按逐级继承算好）

### 4.3 per-folder 设置继承

`get_settings(rel_path)` 按「当前文件夹 → 父文件夹 → … → 全局（`folders.__global__`）→ 硬默认」合并；
`folders` 键存在即覆盖。`save_folder_settings` 支持两种调用形态（见 API 表）。

## 5. 视图模式与前端设计

前端是**视图状态机**：`mode ∈ {albums, children, images}` + `currentView ∈ {albums, timeline, latest}`：

| 模式 | 内容 |
| ---- | ---- |
| `albums` | 相册卡片网格（含 timeline / latest 变体），作者页排序栏（name/mtime/count + 正倒序，localStorage 记忆） |
| `children` | 子相册网格（进入纯容器文件夹，非 Pixiv 或配置点） |
| `images` | 混合瀑布流（`list_folder_items` 渲染） |

关键交互：

- **Justified 瀑布流**：`JustifiedLayout.compute()` 纯函数计算瓦片位置（行高 `row_height` 设置、gap 5px），`seqIndex` 映射瓦片 → 连续序列位置（分页对齐 `all_offset`）
- **灯箱连续浏览**：`all_images` 为按瀑布流顺序展开的完整序列（子文件夹内部按**自己生效的设置**排序），点击任意瓦片从对应位置向右翻看；搜索过滤时用 `filteredSeqIndexes` 保持定位
- **连续序列截断**：后端 `_MAX_ALL_IMAGES = 5000` 截断并返回 `all_truncated`，前端 Toast 提示一次
- **多选**：`selectedImages` Set + 右键菜单（查看原图/多选/移动/删除）+ 批量操作（`delete_files` / `move_files` / `regenerate_thumbs`）
- **全量重建**：右下角非阻塞进度卡（处理数/总数/当前文件/速度/剩余时间/失败数），`rebuild_cancel` 可取消，取消后已生成保留；`rebuild_folder` 单相册增量补齐
- **返回栈**：`navStack` / `scrollStack` 记录多级进入与滚动位置

## 6. 详细 API

后端 `register_api()` 注册的全部方法（前端统一 `Bridge.call('<method>', ...args)` 调用）：

### 6.1 列表与浏览

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `list_images` | `rel_path='', page=1, per_page=40, sort_by='mtime', sort_order='desc'` | `{images, page, total, has_next, has_prev, settings}` | 单目录纯图片列表（尺寸缓存 + 列表缓存；`per_page` 后端封顶 200） |
| `list_folder_items` | `rel_path='', page=1, per_page=40, sort_by='name', sort_order='asc'` | `{items, all_images, all_truncated, all_offset, page, total, image_total, has_next, has_prev, settings}` | 混合瀑布流列表（见 §2.3）；`items` 为「子相册卡片 + 单图」混合；`all_images` 连续浏览序列（截断上限 5000）；`all_offset` 分页对齐偏移 |
| `list_dir` | `rel_path=''` | `[{name, path, mtime}]` | 子目录列表（移动弹窗目录树用，仅目录） |
| `list_albums` | 无 | `{albums, config, changed, cached?}` | 相册全量索引（增量扫描 + 30s TTL；`cached` 标记命中缓存）；`config` 为 `{collapsed, promoted}` |

### 6.2 相册管理

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `create_folder` | `rel_path` | `{success, path \| error}` | 根目录（或指定相对目录）下新建相册文件夹（`_is_safe` 校验） |
| `get_album_config` | 无 | `{collapsed, promoted}` | 相册收纳/提升配置 |
| `set_album_config` | `rel_path, action` | `{success, config}` | `action ∈ collapse/expand/promote/unpromote`；变更后失效相册 TTL 缓存 |

### 6.3 文件操作

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `get_image_info` | `rel_path` | `{success, rel_path, size, width, height \| error}` | 单图存储大小与分辨率（全屏查看器右侧信息面板） |
| `delete_files` | `rel_paths[]` | `{deleted, errors}` | 批量删除 + 清理缩略图缓存与尺寸元数据 |
| `move_files` | `rel_paths[], dest_rel` | `{moved, errors}` | 批量移动（重名自动 `name_1.ext` 递增）+ 清理旧路径缓存 |
| `regenerate_thumbs` | `rel_paths[]` | `{regenerated, errors}` | 删除并重建缩略图（修复黑图/空图）+ 清尺寸元数据 |

### 6.4 缓存与重建

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `refresh` | 无 | `{success}` | 清空内存/相册缓存并作废索引（新增/替换图片立即生效） |
| `rebuild_all` | `rel_path='', force=True` | `{started, running, total \| error}` | 全量/指定文件夹重建（基于共享基建 `BackgroundTask`）：空路径 + force 清空全部缓存后重建；空路径 + force=False 全库增量；非空路径只重建该文件夹 |
| `rebuild_folder` | `rel_path` | 同 `rebuild_all(force=False)` | 单相册增量补齐（相册菜单入口） |
| `rebuild_status` | 无 | `{running, done, success, cancelled, rebuild_path, total, processed, current, error_count, errors}` | 后台任务进度（`task.status()` 封装，前端 500ms 轮询）；错误保留最近 200 条 |
| `rebuild_cancel` | 无 | `{success \| error}` | 请求取消（`task.cancel()` 置 Event；已生成保留） |

### 6.5 设置

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `get_settings` | `rel_path=''` | `{row_height, per_page, sort_by, sort_order, root_dir, pixiv_explicit}` | 生效设置（逐级继承 + 全局回退）；`pixiv_explicit` 为配置点标记 |
| `save_settings` | 两种形态：`save_settings(settings_dict)` 或 `save_settings(rel_path, settings)` | `{success}` | 全局保存走 `super().save_settings()`（schema 过滤）；文件夹级保存进 `folders[rel_path]`（剥离 `root_dir`）并失效相册缓存 |
| `get_root_dir` | 无 | `str` | 当前数据根目录（绝对路径） |
| `clear_folder_settings` | `rel_path` | `{success}` | 删除文件夹独立设置，回退全局 |

### 6.6 Shell 集成接口（非 register_api）

| 方法 | 调用方 | 说明 |
|------|--------|------|
| `get_thumb_data(rel_path)` | Shell `/thumbs` 路由 | 经 `thumb_cache.get()`（ThumbCache 共享基建）读取/生成缩略图字节 `(data, mime)`；`_is_safe` 校验，失败返回 None → 404 |
| `ensure_thumb(rel_path)` | 兼容旧调用方（image-cleaner） | 旧版文件式入口，新路由优先走 `get_thumb_data` |
| `get_data_root()` / `get_file_roots()` | Shell 文件服务 | 安全根目录 = 数据根目录 |
| `get_extensions()` | Shell 扩展注册 | 挂载 image-cleaner 入口（在 `loadExtensions()` 渲染到左侧栏） |

## 7. 设置项

`settings_schema`（全局，集中设置面板可见）：

| key | 类型 | 默认 | 说明 |
|-----|------|------|------|
| `root_dir` | text | `./data` | 数据根目录（相对路径锚定用户数据目录） |
| `row_height` | range 100–400 | 200 | Justified 布局每行目标高度 |
| `per_page` | number 10–200 | 40 | 每页图片数 |
| `sort_by` | select | `mtime` | `mtime` / `name` / `time_name`（Pixiv 排序支持） |
| `sort_order` | select | `desc` | `desc` / `asc` |

**per-folder 设置**（`folders` 键，不在 schema 中，经 `update_setting` 持久化）：

- 结构：`{__global__: {...}, "pixiv": {...}, "pixiv/作者": {...}}`
- `get_settings` 逐级合并：当前文件夹 → 父级 → `__global__` → 硬默认
- 保存全局时前端会 `clear_folder_settings(当前文件夹)`（注意：会清掉该文件夹独立设置）

## 8. 文件服务集成

```
/thumbs/<rel_path>?plugin=image-viewer
  └─► get_thumb_data(rel_path) ──► thumb_cache.get()（ThumbCache）──► SQLite 命中 / 生成回写
       （rel_path 经 is_safe_path 校验，失败 404）

/file?path=<rel_path>&plugin=image-viewer
  └─► Shell serve_media_file：以 get_data_root() 为根做路径安全检查
```

> 鉴权：`/api`、`/file`、`/thumbs` 均为 Shell 令牌保护路由（见 `docs/plugin-guide.md` §7 与 readme 访问令牌说明）。插件 iframe 内同源请求自动携带 Cookie，无需额外处理。

## 9. 性能设计要点

| 机制 | 参数 | 说明 |
|------|------|------|
| 尺寸读取并行 | `_SCAN_WORKERS = 8`（ThreadPoolExecutor） | 首次扫描大文件夹 Pillow 读尺寸并行化（4.2 万文件 4.7s → 1.75s） |
| 子目录扫描并行 | 同上（`_scan_album_items` 目录级） | 每个子目录独立线程 scandir + 尺寸 |
| 相册列表 TTL | `_ALBUMS_TTL = 30s` | 避免每次进相册页全树 walk（10.7 万文件 3.5s → 0.05s） |
| 列表缓存上限 | `_MAX_LIST_CACHE = 200` | FIFO 淘汰最旧一半，防长时间使用内存膨胀 |
| 连续序列截断 | `_MAX_ALL_IMAGES = 5000` | 防超大相册全量下发 |
| per_page 封顶 | 200 | 后端强制（前端 10–200） |
| 元数据落盘 | 脏标记 + 原子写 | 仅新增条目时写盘 |
| 缩略图批量 | `workers = min(8, cpu_count)`（ThumbCache 默认） | 全量重建并行生成（`generate_bulk`）；取消时已排队任务取消、运行中任务跑完（Pillow 无取消点） |

## 10. 已知限制与注意事项

- **嵌套只处理一层**：混合瀑布流的 `_aggregate_children` 递归仅一层；三层以上嵌套的卡片计数与进入后内容可能不一致（`list_albums` 是全深度聚合，口径不同）
- **列表缓存陈旧**：子文件夹排序设置变更后 `all_images` 序列可能沿用旧顺序（见 §3.4）
- **重建与按需缩略图并发**：全量重建期间 `/thumbs` 按需生成同写一个 DB（WAL 容忍并发；VACUUM 收缩可能因活跃连接静默失败）
- **`save_folder_settings` 不校验 `rel_path`**：任意字符串可写入 `folders` 键（含 `__global__`）；当前仅前端调用，接口层未防御
- **缩略图 URL 未编码**：文件名含 `#` / `%` 等字符时缩略图可能加载失败（原图经 `originalUrl` 编码正常）

## 11. 测试与调试

- 仓库测试：`python -m unittest tests.test_image_viewer_mixed`（混合瀑布流 19 项）
- 状态调试：`python tests/debug_status_pages.py`（一键起 `--status-debug` 服务器 + 11 个 HTTP 场景触发表 + 壳内 `/status` 调试面板；含坏插件演示 iframe 404 → 壳内错误卡片链路）
- 常用验证：`refresh` API 强制重扫、`rebuild_status` 轮询查看重建进度、`G:\图库` 等大目录做性能基准

## 12. 版本记录

| 版本 | 说明 |
|------|------|
| v2.3.0 | 混合瀑布流 + 「时间+文件名」排序标准 |
| v2.4.0 | Pixiv 排序两层嵌套 + 缩略图更新/全局刷新 |
| v2.4.1 | 作者页排序栏（文件名/更新时间/图片数量 + 正倒序） |
| v2.4.2 | SQLite 缩略图缓存（58bf8d2）→ 全量重建后台任务/进度/取消（a2044f4–5f0af50）→ 并行生成与指定文件夹重建 → PyInstaller sqlite3 显式打包（827d76b）→ 性能优化（TTL/并行/脏标记，58d6ea4） |
| v2.4.3 | 缩略图缓存与重建任务迁移到 Shell 共享基建（ThumbCache / BackgroundTask，d7c8620），API 与行为不变 |
