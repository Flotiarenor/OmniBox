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
│   ├── main.py                 # 入口：ImageViewerPlugin（类骨架 + API 注册 + 初始化）
│   ├── common.py               # 各分片共用的常量与纯函数（无实例状态）
│   ├── namespace.py            # NamespaceMixin：多根目录 / 虚拟命名空间路径
│   ├── thumbs.py               # ThumbMixin：元数据缓存与缩略图
│   ├── listing.py              # ListingMixin：目录 / 图片列表与子目录聚合扫描
│   ├── albums.py               # AlbumMixin：相册树、封面挑选与相册缓存
│   ├── file_ops.py             # FileOpsMixin：目录 / 文件增删改与刷新
│   ├── rebuild.py              # RebuildMixin：缩略图全量重建（BackgroundTask）
│   ├── settings.py             # SettingsMixin：按目录的设置读写
│   └── filesystem.py           # 底层纯函数工具：路径安全、排序、尺寸元数据（无实例状态）
└── frontend/
    ├── index.html              # 侧边栏 + 工具栏 + 弹窗骨架
    ├── image-viewer.css
    └── js/
        ├── app.js              # 类骨架：构造函数、初始化、生命周期、扩展入口、UI 绑定
        ├── app-albums.js       # ImageViewer 分片：相册树浏览、渲染与排序
        ├── app-nav.js          # ImageViewer 分片：返回导航栈、相册右键菜单、统计与新建相册
        ├── app-grid.js         # ImageViewer 分片：图片网格、Justified 布局、幻灯与多选操作
        ├── app-refresh.js      # ImageViewer 分片：刷新与缩略图重建
        ├── app-settings.js     # ImageViewer 分片：设置读写
        ├── app-utils.js        # ImageViewer 分片：格式化与转义工具（_escapeHtml / _escapeAttr）
        └── justified-layout.js # Justified 布局计算（纯函数）
```

`ImageViewer` 的 67 个成员原本集中在一个 1500 行的 `app.js` 里，按类体里已有的分节注释
拆成上面 7 个文件。分片用 `Object.assign(ImageViewer.prototype, {...})` 扩回**同一个**
原型，因此成员与调用点没变、行为不变；**代价是 `index.html` 的 `<script>` 顺序变成硬约束**
（分片必须排在 `app.js` 之后、实例化之前），漏挂或错序会在装载期抛
`ImageViewer is not defined`。这条契约由 `tests/js/image_viewer_app_split.mjs` 把关
（共享检查器见 `tests/js/script_load_contract.mjs`）：它按 `index.html` 的声明顺序装载全部
脚本，断言无孤立脚本、无重复定义、67 个成员仍在。

另外三个 image-viewer 前端用例（`image_viewer_lifecycle` / `image_viewer_album_visibility` /
`image_viewer_roots_list`）都改成**按 `index.html` 顺序装载或扫描全部脚本**，不在文件里写死
`app.js` —— 所以再往下拆分片时它们不需要跟着改。

**职责边界**：

- `main.py` 只保留**类骨架**：`settings_schema`、类常量、`__init__`（持有全部实例状态：
  `_meta_cache` / `_list_cache` / `_album_cache` / `_album_config` / `_rebuild`）、
  `thumb_cache`（共享基建实例）、`register_api`与根目录/浏览接口；
- 78 个方法按职责拆到 7 个 mixin 分片。**拆分是纯搬移**：方法体逐字未改，状态仍由
  `main.py` 的 `__init__` 持有，分片只把方法挂到同一个类上 —— 因此调用点、缓存键、
  API 语义都没有变化；
- **mixin 必须排在 `PluginBase` 之前**（`class ImageViewerPlugin(NamespaceMixin, …,
  PluginBase)`）：`get_data_root` / `get_file_roots` / `ensure_thumb` / `get_thumb_data` /
  `get_settings` / `on_settings_changed` / `on_unload` 都是对基类的覆写，排在基类后面会被
  基类实现盖掉；
- `common.py` 承载跨分片共用的常量与纯函数（`NAMESPACE_MARKER`、`pixiv_sort` / `pick_cover` /
  `cover_rank` / `same_path`，以及从 `filesystem.py` 转出的工具）。为什么需要它：后端入口由
  PluginManager 用 importlib 直接加载，**分片之间不能互相 import `main.py`（会成环）**，
  所以共用的东西需要一个中立的落点；`main.py` 把它们重新绑定成原来的名字，既有引用与测试
  （`module._pick_cover`）都不用改；
- `filesystem.py` 全部为**无实例状态**的模块级纯函数（通过 `load_sibling` 注入），便于单测与复用；
- 缩略图缓存使用共享基建 `shell/backend/thumb_cache.py`（`ThumbCache`），重建任务使用 `shell/backend/tasks.py`（`BackgroundTask`）——见 `docs/plugin-guide.md` §3.4；
- 前端通过 `Bridge.call(...)` 调用后端，图片/缩略图通过 `/file`、`/thumbs` 路由访问（Shell 提供）。

### 2.2 与 Shell / 其他插件的关系

- **文件服务**：`/thumbs/<path>` 优先调用插件 `get_thumb_data()` 从 SQLite 返回字节；`/file` 走 Shell 通用路由（`get_data_root()` 即安全根目录）
- **Companion**：`image-cleaner` 依赖本插件，复用 `delete_files()`、`ensure_thumb()`、`get_data_root()`，通过 `get_extensions()` 挂载到左侧栏
- **设置**：通过 Shell 统一 `SettingsStore`（`.config/plugins/image-viewer.json`）持久化

### 2.3 浏览数据流

```
打开相册页 ──► list_albums ──► _list_album_dirs(全部根目录的目录枚举)
                             └─► _build_albums(增量扫描变化目录 + 自底向上聚合)
                             │  └─► albums_index.json(version 3) 持久化 + 30s TTL 内存缓存

进入文件夹 ──► list_folder_items ──► 直接图片 scandir + 尺寸并行读取
                                   ├─► _scan_album_items(子目录级并行: 封面 p0 + 计数)
                                   └─► items 排序(按生效设置) + all_images 连续序列
                                   │    └─► _list_cache(目录mtime + 排序键) 内存缓存

渲染瓦片 ──► <img src="/thumbs/..."> ──► get_thumb_data ──► SQLite 命中 / Pillow 生成回写
```

### 2.4 多根目录与虚拟路径

`root_dir` 是第一根（沿用**相对路径**，既有链接/缓存键/缩略图库都不受影响），
`extra_roots` 是额外根目录（每行一个，设置页可增删）。额外根在相册树里以
**命名空间节点**出现：

```
相册树（虚拟路径）                          物理路径
  ''（合成根）                              ─
  ├─ 卡伦/…                                <root_dir>/卡伦/…
  └─ __pixiv类/…                           D:\图库\pixiv类\…
        └─ __pixiv类/卡伦/作品A/1.jpg       D:\图库\pixiv类\卡伦\作品A\1.jpg
```

- 命名空间 token = 根目录名，重名或与第一根一级子目录冲突时按**配置顺序**加
  ` (2)`、` (3)`；token 是**派生值**（`_namespace_map()` 实时计算，不持久化），
  目录增删后序号会自动收回。路径段以 `__` 开头即视为命名空间，真实目录名不会
  这样命名（`_is_namespace_node` 只认「恰好一层且 token 有效」）。
- `_split_virtual()` / `_virtual_path()` 负责虚拟 ↔ 物理互转；所有 API 只收发
  虚拟路径，`/thumbs` 缓存键与 `/file?path=` 同样是虚拟路径。
- **`depth` 按根内相对路径计算**：额外根下的作者层与第一根的作者层同为
  depth 1，两层 Pixiv 布局（配置点 → 作者 → 作品）不会被命名空间顶掉。
- 额外根的命名空间节点 `root_scope: true`，与第一根的合成根（`path == ''`）
  同地位：都是「顶层容器」，前端把它当 Pixiv 树的配置点。
- **文件服务**：`get_file_roots()` 返回全部根；`/file` 的相对路径由本插件用
  `resolve_file_path()` 解释（`__<命名空间>/…` → 额外根下的物理路径），
  第一根以外的原图因此能直接打开 —— 在此之前 `/file` 只按 `roots[0]` 拼，
  额外根的原图 404（「网格与缩略图正常、点开是破图」）。Shell 仍对解析结果逐根
  做安全检查，并优先执行受保护路径判定。

**设置页入口**：`root_dir` 不再有独立输入框——「图片文件夹」列表是唯一入口
（第一行 = 主目录，其余 = 额外目录），保存时第一行写回 `root_dir`、其余写回
`extra_roots`。列表与目录选择器是 **Shell 共享组件** `window.FolderPicker`
（`shell/frontend/public/shell/folder-picker.js` + `folder-picker.css`，见
`docs/plugin-guide.md` §7.2）：这套 UI 原本长在本插件的 `app.js` 里，因为
media-player / manga-library / novel-reader 也要同一套「多位置文件夹」界面，
已整体搬到 Shell，本插件改为 `FolderPicker.createList()` 引用回来（类名与样式
`.iv-root-*` / `.iv-dirbrowser-*` 原样跟着搬走，视觉无变化）。选择器可一键回到
「我的电脑」层重选盘符。
「主要」是**位置**属性而非每行自带标记，所以每行都有 `x` 移除按钮、行高一致；删掉第一行
后下一行自动顶上成为主目录；列表被清空时显式写空 `root_dir`，后端回退到默认
数据目录（`./data`），与界面提示一致。

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

- 版本号 `_ALBUM_CACHE_VERSION = 4`（封面挑选规则变更必须 +1，否则目录 mtime 未变时
  增量扫描会继续复用旧封面）：
  - v4：Pixiv 树封面 = **作品号最大**的那张（画师最近的作品），同作品内取 p0
  - v3：封面 = 文件名自然序第一张（p0）
  - v2：封面 = 最新 mtime 的那张
- 内容：`{version, dirs: {rel_path: {mtime, direct_count, direct_cover, direct_mtime, pixiv, has_children, ...}}}`
- **增量**：`list_albums` 全树枚举目录 mtime，仅扫描变化目录（0.5s 容差），自底向上聚合 `image_count / cover / newest`；
  缓存命中还需 `pixiv` 标记一致：封面规则由 Pixiv 排序决定，切换排序后必须重扫该目录
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
- `_pixiv_sort`：前导数字条目按数字大小排（方向生效），无数字条目按自然名排并**始终位于最后**；
  勾选**模糊匹配**（`pixiv_fuzzy`）后，同号/无号条目之间改按整名自然序比较（数字段比数字、
  其余段比文字），于是 `2024-05-10` < `2024-05-24 日富美` 这类「先数字再文字」的条目也能排对
- **两层嵌套语义**（`get_settings` 返回 `pixiv_explicit`）：
  - 配置点（**自身**存了 `time_name` 或 `pixiv_fuzzy`，如 `pixiv/` 主文件夹、`卡伦/`）→ 显示**子相册网格**（作者卡片）
  - 继承 `time_name` 的子文件夹（作者层）→ 显示**混合瀑布流**（作品 p0 瓦片 + 圆圈数量角标）
  - 作品内部（纯图片文件夹）图片仍按文件名自然序 p0 → p1，不受方向影响
- 卡片 `use_time_name` 标记瓦片是否启用角标（后端按逐级继承算好）

#### 模糊匹配（`pixiv_fuzzy`）

Pixiv 排序原先只服务 pixiv-sync 的落盘形态（`<pid>.jpg`）。对于结构相同但
命名非数字的图库（`卡伦/2019-09-19 两边皆可~/1.jpg`，即「作者/作品名/序号.jpg」），
在作者目录上勾一次**模糊匹配**即可拿到同一套浏览效果，不必逐个子目录配置：

- 只在 `sort_by == 'time_name'` 时生效（未选 Pixiv 排序时勾选它是死设置，`_pixiv_fuzzy_mode` 返回 False）；
- 该目录因 `pixiv_explicit` 成为配置点 → 作者网格；子作品目录继承后走瀑布流；
- 排序键、封面规则、数量角标、`use_time_name` 与 Pixiv 排序完全一致（封面本就看
  叶子文件前导数字，所以序号命名的作品内部同样取号最大那张）。

### 4.3 相册封面挑选

`_scan_dir_direct` 里按目录生效的排序决定封面（`_pick_cover`）：

- **Pixiv 树**（`sort_by == 'time_name'`）：取**作品号最大**的那张 —— 画师目录下平铺着
  `<pid>.jpg` / `<pid>_p0.jpg`（pixiv-sync 的落盘规则），旧规则"自然序第一张"等于永远
  展示该画师最老的作品；同作品（同 pid）内再取文件名自然序第一张，即 p0 而非 p1/p10；
- **其他目录**：文件名自然序第一张（行为不变）。

纯容器目录的封面由 `_build_albums` 自底向上聚合，比较键是 `_cover_rank`：
Pixiv 树按作品号大者优先（老作品被重新下载、目录 mtime 更新也抢不走封面），
非 Pixiv 目录仍是 mtime 最新者优先；两种情况下 `image_count / mtime` 聚合都不受影响。

### 4.4 per-folder 设置继承

`get_settings(rel_path)` 按「当前文件夹 → 父文件夹 → … → 全局 → 硬默认」合并；
全局来源有两处：`folders.__global__`（更早的兼容位置，当前代码不再写入）与
`settings_schema` 声明的插件级键（插件的设置弹窗与 `save_folder_settings('')` 走
`PluginBase.save_settings()` 写入，是当前写入路径）。**只有真正存过值的插件级键**
才覆盖 `__global__`——用 `self.setting()` 的 schema 默认值去覆盖会让老配置里的
全局值被默认值悄悄顶掉。`folders` 的文件夹级键存在即覆盖全局。
`save_folder_settings` 支持两种调用形态（见 API 表）。

### 4.5 作者网格二次排序

Pixiv 排序下的作者卡片网格支持二次排序（更新时间 / 文件名 / 图片数量 + 方向），
配置项 `album_sort_by` / `album_sort_order`，默认「更新时间 / 倒序」：

- 只在**生效** Pixiv 排序的页面应用（`_albumPageIsPixiv()`：children 页看父目录、
  albums 页看根目录的 `use_time_name`）；其他页面保持文件名正序，不受这组设置影响；
- 设置页只在当前文件夹 `get_settings().sort_by == 'time_name'` 时显示这组选项，
  值本身是全局偏好（不写入文件夹级设置）；选项隐藏时保存不会写这两个键。

### 4.6 设置页「模糊匹配」开关

`pixiv_fuzzy` 与 `sort_by` **同作用域**：勾了「仅应用于当前文件夹」写文件夹级
（`folders[路径]`），否则写全局插件级键（并清掉当前文件夹的独立设置，与排序方式一致）。
它在设置页的显示条件同样是当前文件夹生效 `sort_by == 'time_name'`，且在下拉框切到
「Pixiv 排序支持」的当下就显示（不必保存刷新后再打开设置）；未选 Pixiv 排序保存时
不写该键，避免留下「看起来生效」的残留值。

### 4.7 可见性规则（默认折叠 + 空目录隐藏）

两条规则都在前端 `_filterVisibleAlbums()` 里实现，输入是 `list_albums` 的完整列表
（含 `readable` = 递归可读图片数）：

- **子相册默认折叠**：只有被显式「展开」过的目录才显示下级——`_isCollapsed()` 判定
  「含子目录 且 不在 `expanded` 白名单」即为折叠；「收纳子相册」写入 `collapsed`
  并撤销 `expanded`（`collapsed` 优先，兼容旧配置）。效果是「全部相册」只平铺顶层，
  点进去才看下一层；`depth <= 1` 的顶层始终显示，`promoted` 的相册可越过折叠显示。
- **空目录隐藏**：`readable == 0` 的目录（后端已含全部下级）整棵不显示；
  `visible_empty_dirs` 里的路径保留可见——「新建相册」会把新建的那层记进该标记
  （`_mark_visible`），目录被删除或换根时由 `_prune_visible_marks()` 清理。
  标记同时**向上生效**：只标记深层目录时，它的上级也一并显示，否则那层永远点不进去。
  空相册卡片带「空相册」标签（`Icons.html('icon:folder')`），右键菜单提供「不再显示此空相册」。


## 5. 视图模式与前端设计

前端是**视图状态机**：`mode ∈ {albums, children, images}` + `currentView ∈ {albums, timeline, latest}`：

| 模式 | 内容 |
| ---- | ---- |
| `albums` | 相册卡片网格（含 timeline / latest 变体）；作者网格的二次排序只在设置页配置（见 §4.5） |
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
| `list_albums` | 无 | `{albums, config, changed, cached?}` | 相册全量索引（增量扫描 + 30s TTL；`cached` 标记命中缓存）；`config` 为 `{collapsed, promoted, expanded, visible_empty_dirs}`；每条相册含 `readable`（递归可读图片数）与 `root_scope`（额外根命名空间节点 / 第一根合成根） |

### 6.2 相册管理

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `create_folder` | `rel_path` | `{success, path \| error}` | 根目录（或指定相对目录）下新建相册文件夹（`_is_safe` 校验；命名空间节点下不允许建）；新建的空目录记入 `visible_empty_dirs` 保留可见 |
| `get_album_config` | 无 | `{collapsed, promoted, expanded, visible_empty_dirs}` | 相册收纳/提升配置与空目录标记 |
| `set_album_config` | `rel_path, action` | `{success, config}` | `action ∈ collapse/expand/promote/unpromote`；expand/collapse 落进 `expanded` / `collapsed`（默认折叠）；变更后失效相册 TTL 缓存 |
| `delete_folder` | `rel_path` | `{success \| error}` | 删除**空**目录（递归无图片）并清掉其可见标记；有图片则拒绝，命名空间节点不可删 |
| `list_roots` | 无 | `[{path, label, is_primary, exists, namespace}]` | 全部根目录（第一根在前），设置页「图片文件夹」列表用 |
| `browse_dir` | `path=''` | `{path, parent, entries:[{name, path, kinds, is_image_dir}], error?}` | 目录选择器；委托共享基建 `media_catalog.list_subdirectories()`：空路径/`DRIVES_SENTINEL` 是「我的电脑」层（列盘符），`kinds` 标出含图片/视频/音乐（见 `docs/plugin-guide.md` §7.2） |

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
| `get_settings` | `rel_path=''` | `{row_height, per_page, sort_by, sort_order, pixiv_fuzzy, album_sort_by, album_sort_order, root_dir, pixiv_explicit}` | 生效设置（逐级继承 + 全局回退）；`pixiv_explicit` 为配置点标记（自身存过 `sort_by=time_name` 或 `pixiv_fuzzy`） |
| `save_settings` | 两种形态：`save_settings(settings_dict)` 或 `save_settings(rel_path, settings)` | `{success}` | 全局保存走 `super().save_settings()`（schema 过滤）；文件夹级保存进 `folders[rel_path]`（剥离 `root_dir`）并失效相册缓存 |
| `get_root_dir` | 无 | `str` | 当前数据根目录（绝对路径） |
| `clear_folder_settings` | `rel_path` | `{success}` | 删除文件夹独立设置，回退全局 |

### 6.6 Shell 集成接口（非 register_api）

| 方法 | 调用方 | 说明 |
|------|--------|------|
| `get_thumb_data(rel_path)` | Shell `/thumbs` 路由 | 经 `thumb_cache.get()`（ThumbCache 共享基建）读取/生成缩略图字节 `(data, mime)`；`_is_safe` 校验，失败返回 None → 404 |
| `ensure_thumb(rel_path)` | 兼容旧调用方（image-cleaner） | 旧版文件式入口，新路由优先走 `get_thumb_data` |
| `get_data_root()` / `get_file_roots()` | Shell 文件服务 | 数据根目录 / **全部**根目录（`root_dir` + `extra_roots`，逐根做路径校验） |
| `resolve_file_path(rel_path)` | Shell 文件服务（`/file` 相对路径） | 虚拟路径 → 物理路径（命名空间前缀由本插件解释）；答不上来返回 `None` |
| `get_extensions()` | Shell 扩展注册 | 挂载 image-cleaner 入口（在 `loadExtensions()` 渲染到左侧栏） |

## 7. 设置项

`settings_schema`（在插件自己的设置弹窗里可见，经 `<插件>__get_settings_schema` 读取）：

| key | 类型 | 默认 | 说明 |
|-----|------|------|------|
| `root_dir` | text | `./data` | 数据根目录（相对路径锚定用户数据目录） |
| `extra_roots` | textarea | 空 | 额外图片根目录，每行一个；设置页「图片文件夹」列表的第一行写回 `root_dir`、其余写回本项（列表是唯一入口） |
| `row_height` | range 100–400 | 200 | Justified 布局每行目标高度 |
| `per_page` | number 10–200 | 40 | 每页图片数 |
| `sort_by` | select | `mtime` | `mtime` / `name` / `time_name`（Pixiv 排序支持） |
| `sort_order` | select | `desc` | `desc` / `asc` |
| `pixiv_fuzzy` | checkbox | `false` | 模糊匹配（仅 `sort_by == 'time_name'` 生效）：同号/无号条目按「先数字再文字」排序，并把该文件夹视为 Pixiv 树配置点（见 §4.2） |
| `album_sort_by` | select | `mtime` | 作者网格二次排序：`mtime`（更新时间）/ `name` / `count`；当前文件夹未生效 Pixiv 排序时设置页不显示该组选项 |
| `album_sort_order` | select | `desc` | 作者网格二次排序方向：`desc` / `asc` |

**per-folder 设置**（`folders` 键，不在 schema 中，经 `update_setting` 持久化）：

- 结构：`{__global__: {...}, "pixiv": {...}, "pixiv/作者": {...}}`
- `get_settings` 逐级合并：当前文件夹 → 父级 → 全局（`__global__` 打底，**存过值**的插件级 schema 键覆盖）→ 硬默认
- 二次排序（`album_sort_by` / `album_sort_order`）是全局偏好，不写入文件夹级设置；
  设置页仅在当前文件夹 **生效** Pixiv 排序（`sort_by == 'time_name'`）时显示这组选项——
  改成 Pixiv 排序保存后页面刷新，下次打开设置即出现；隐藏时保存不会写这两个键
- 模糊匹配（`pixiv_fuzzy`）相反：与 `sort_by` 同作用域（表单里勾「仅应用于当前文件夹」
  即写文件夹级），保存页在选项可见时才写，未选 Pixiv 排序时不写
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

- **列表瀑布流仍只处理一层**：`_aggregate_children` 会递归统计出正确的图片总数
  （空目录判定与命名空间卡片依赖它），但 `list_folder_items` 的瓦片仍只展开一层；
  三层以上嵌套的卡片进入后内容与计数口径不同（`list_albums` 是全深度聚合）
- **命名空间前缀 `__` 是保留语义**：第一根里以 `__` 开头的目录名会被当成命名空间
  解析（token 不在列表里则视为未知路径，拒绝访问），不建议这样命名真实目录
- **列表缓存陈旧**：子文件夹排序设置变更后 `all_images` 序列可能沿用旧顺序（见 §3.4）
- **重建与按需缩略图并发**：全量重建期间 `/thumbs` 按需生成同写一个 DB（WAL 容忍并发；VACUUM 收缩可能因活跃连接静默失败）
- **`save_folder_settings` 不校验 `rel_path`**：任意字符串可写入 `folders` 键（含 `__global__`）；当前仅前端调用，接口层未防御
- **缩略图 URL 编码**：`Bridge.thumbUrl()` 逐段 `encodeURIComponent`（保留 `/` 分隔符）；
  修复前目录名带 `%`（如 `pixiv/29%/`）时前端发出的裸 `%` 被 nginx 判为非法转义、
  反代层直接 400，缩略图恒不显示且重建无效（原图经 `originalUrl` 编码所以正常）

## 11. 测试与调试

- 仓库测试：`python -m unittest tests.test_image_viewer_mixed`（混合瀑布流 19 项）、
  `python -m unittest tests.test_image_viewer_pixiv_cover`（画师封面 + 作者视图二次排序 11 项）、
  `python -m unittest tests.test_image_viewer_pixiv_fuzzy`（模糊匹配 + 配置点 11 项）、
  `python -m unittest tests.test_image_viewer_multi_root`（多根目录 / 空目录 / 折叠 17 项）、
  `python -m unittest tests.test_image_viewer_album_visibility_js`（前端可见性与图片文件夹列表无头用例）
- 状态调试：`python tests/debug_status_pages.py`（一键起 `--status-debug` 服务器 + 11 个 HTTP 场景触发表 + 壳内 `/status` 调试面板；含坏插件演示 iframe 404 → 壳内错误卡片链路）
- 常用验证：`refresh` API 强制重扫、`rebuild_status` 轮询查看重建进度、`G:\图库` 等大目录做性能基准

## 12. UI 现状取证（前端与壳契约对照）

审计范围：`plugins/image-viewer/frontend/`（只读取证，未改动任何文件）。
对照基线：`shell/frontend/public/shell/variables.css`（86 行）、`base.css`（432 行）、
`effects.css`（201 行）、`base.js`、`docs/plugin-guide.md` §4。

---

### 12.1 概览

#### 12.1.1 前端文件清单

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `index.html` | 205 | 唯一入口；含侧栏/工具栏/网格/3 个弹窗/扩展视图/重建进度卡的静态 DOM |
| `image-viewer.css` | 404 | 全部样式；无内部分段注释体系（顶部只说明依赖三个注入 CSS，`css:1-4`） |
| `js/justified-layout.js` | 47 | `JustifiedLayout.compute(images, containerWidth, targetHeight, gap=5)`：行高归一 + 末行不拉伸 |
| `js/app.js` | 280 | `ImageViewer` 类骨架：构造、`init()`、`_bindPluginLifecycle`、`loadExtensions/openExtensionView`、`_bindUI` |
| `js/app-albums.js` | 380 | `prototype` 分片：相册树过滤/折叠/提升、时间线、卡片渲染、二次排序、导航栈 |
| `js/app-nav.js` | 206 | `prototype` 分片：返回栈、**相册自绘右键菜单**、统计行、`FolderPicker` 接管、新建相册 |
| `js/app-grid.js` | 342 | `prototype` 分片：`loadImages`、瀑布布局渲染、搜索过滤、幻灯片、多选/移动/删除 |
| `js/app-refresh.js` | 176 | `prototype` 分片：刷新、全量/单相册重建、轮询进度、取消、重生成缩略图 |
| `js/app-settings.js` | 125 | `prototype` 分片：设置弹窗读写（per-folder 与全局两种作用域） |
| `js/app-utils.js` | 64 | `prototype` 分片：时长/月份/相对时间格式化、`_emptyHtml`、`_escapeHtml`/`_escapeAttr` |

分片机制：`app.js` 定义类，六个分片用 `Object.assign(ImageViewer.prototype, {...})` 回挂；
`index.html:191-199` 注释「顺序是硬约束」，实例化在 `:200-203`，契约由
`tests/js/image_viewer_app_split.mjs` 把关。

#### 12.1.2 页面形态

单页 + 三态视图切换（`mode`：`albums|children|images`，`currentView`：`albums|timeline|latest`），
无路由，但**内含一个 companion 内嵌视图**：`#extension-view` 用 `<iframe id="extension-frame">`
承载扩展插件（`index.html:65-73`，实现 `app.js:157-173`，由 `renderExtensions(..., {onEmbed})` 触发，
`app.js:141-144`）。三个弹窗：新建相册（`index.html:76-90`）、移动（`:92-101`）、设置（`:103-171`）。
全量重建进度卡常驻右下角（`:173-186`）。

#### 12.1.3 Shell 布局类 / 组件使用情况（用）

布局类：`view-sub-sidebar`（`index.html:11`）、`view-body`（`:32`）、`view-toolbar`（`:33`）、
`toolbar-group`（`:34,41`）、`view-content`（`:59`）、`pagination-bar`（`:63`）、`modal`/`modal-box`/`modal-body`/`modal-footer`
（`:76-171`）、`obx-nav-item`（`:21-23,27`）、`btn`/`btn-sm`/`btn-primary`/`btn-danger`（`:35,48-55,68,86-87…`）、
`hidden`、`obx-scroll`（`:19,59,106`）、`obx-glass`（`:11`）、`obx-anim-scale`（`:77,93,104`）、
`tree-container`（`:95`）、`search-input`（`:82`）、`iv-dirbrowser-list`（壳组件内部）。

Shell 组件：`createLightbox`（`app.js:54`）、`createPagination`（`:55-57`）、`createContextMenu`（`:58-66`）、
`createTree`（`:266-270`）、`confirmDialog`（4 处：`app-refresh.js:43,49,164`；`app-grid.js:301`）、
`Toast`（约 30 处）、`Utils.debounce/escapeHtml`（`app.js:213`、`app-utils.js:49-51`）、
`renderExtensions`（`app.js:141`）、`window.FolderPicker`（`app-nav.js:158`）、
`window.PluginLifecycle` 三钩子（`app.js:92-119`）。

---

### 12.2 布局骨架

#### 12.2.1 顶层容器树

```
body
└─ #app                        display:flex; height:100vh; overflow:hidden; 背景两层渐变  (css:13-20)
   ├─ aside.view-sub-sidebar.iv-sidebar.obx-glass        (index.html:11)
   │  ├─ .iv-brand              padding 18px 16px 14px; gap 12px  (css:31)
   │  │  ├─ .iv-brand-icon      40×40; radius 12px; 渐变底  (css:32-37)
   │  │  └─ .iv-brand-text → .iv-brand-title(15px/700) / .iv-brand-sub(11px)
   │  ├─ nav.iv-nav.obx-scroll   flex:1; overflow-y:auto; padding 8px 10px; gap 3px  (css:41)
   │  │  ├─ .iv-nav-label       padding 12px 10px 5px; 11px/700; uppercase  (css:204-208)
   │  │  ├─ button.obx-nav-item.iv-nav-item ×3（全部相册/时间线/最近添加）  (index.html:21-23)
   │  │  └─ #iv-extensions      renderExtensions 容器  (index.html:24)
   │  ├─ .iv-nav-footer         padding 8px 10px; border-top  (css:209-210)
   │  │  └─ button.obx-nav-item.iv-nav-item（新建相册入口 <svg class="obx-icon"><use href="#plus"></use></svg>新建相册，width:100%）  (index.html:27)
   │  └─ .iv-sidebar-footer#iv-stats   padding 11px 14px; 11px  (css:50)
   └─ .view-body                (index.html:32)  ← 插件重定义：position:relative (css:368-370)
      ├─ .view-toolbar.iv-toolbar      48px（壳）; gap:12px  (css:53)
      │  ├─ .toolbar-group.iv-view-heading   ←返回 / 标题(15px/700) / 副标题(11px)
      │  └─ .toolbar-group[style=margin-left:auto]   行内样式  (index.html:41)
      │     └─ .iv-search + #iv-selection-count + 7 个 .btn（幻灯片/多选/删除/移动/更新缩略图/刷新/全量重建/设置）
      ├─ .view-content.iv-content.obx-scroll#iv-content   padding 16px; background:transparent  (css:70)
      │  ├─ #iv-albums             相册网格容器（.iv-grid 由 JS 包一层，css:72-76）
      │  └─ #image-grid.iv-image-grid   position:relative; 子元素全 absolute  (css:119)
      ├─ #pagination.pagination-bar  壳组件，高 48px（base.css:81-86）
      └─ #extension-view.extension-view     absolute; inset:0; z-index:20  (css:371-381)
         ├─ .extension-view-header   padding 8px 14px; border-bottom；标题 15px/700、
         │                           按钮 13px/28px；内含 #extension-view-actions ——
         │                           内嵌插件挂按钮的容器（HostChannel.serve 的 containers）
         └─ .extension-view-body → iframe 100%×100%

   （.modal ×3 与 .rebuild-progress-card 挂在 #app 内、.view-body 外）  (index.html:76-186)
```

#### 12.2.2 关键尺寸

| 区域 | 值 | 来源 |
| --- | --- | --- |
| 侧栏宽 | `240px`（`css:24`）显式写死，**未用** `var(--sub-sidebar-width)`；`max-width:860px` 时降到 `200px`（`css:198-201`） | 插件 |
| 工具栏高 | 48px（壳 `base.css:240`）；插件只加 `gap:12px`（`css:53`） | 壳 |
| 内容区 padding | `16px`（`css:70`） | 插件（覆盖壳 `.view-content` 的 16px，值相同） |
| 相册网格 | `minmax(170px,1fr)`；gap 16px（`css:72-76`）；窄屏 `minmax(140px,1fr)`（`css:200`） | 插件 |
| 相册卡 | `aspect-ratio:1` 封面（`css:94`）；radius `var(--iv-radius)`=14px；`contain-intrinsic-size:260px` | 插件 |
| 图片瓦片 | 绝对定位，尺寸由 `JustifiedLayout.compute` 算出：目标行高默认 `200px`（`app.js:27`，设置项 100–400），gap 固定 `5`（`app-grid.js:72`） | 插件 |
| 重建进度卡 | `position:fixed; right:16px; bottom:16px; width:320px`；进度条高 `6px`（`css:284-297,330-335`） | 插件 |
| 扩展视图头 | `padding:8px 14px`（`css:382-390`） | 插件 |

#### 12.2.3 滚动方式

- 唯一滚动容器是壳的 `.view-content#iv-content`（`overflow-y:auto`），并显式挂 `.obx-scroll`（`index.html:59`）。
- 相册网格与图片网格**不各自滚动**：`#image-grid` 由 JS 设 `style.height = totalHeight`（`app-grid.js:74`），
  滚动全部交给外层；离开相册时把高度重置为 `0`（`app-albums.js:50`）。
- 侧栏 `nav.iv-nav`（`css:41`）与设置弹窗正文 `.iv-settings-body`（`css:179`）各自滚动，均挂 `.obx-scroll`。
- 从列表进入图片后返回会恢复 `#iv-content.scrollTop`（`app-albums.js:356-371`，`requestAnimationFrame` 内恢复）。

---

### 12.3 设计 token 使用

#### 12.3.1 实际引用的壳 token（`var(--...)` 共 95 处引用 / 404 行 CSS）

背景：`--bg-app`(19,377,403)、`--bg-surface`(82,122,158,163,217,291,387)、`--bg-hover`(94,99,226,281,317,333)。
文本：`--text-primary`(38,55,154,158,163,226,278,296,318,328)、`--text-secondary`(39,50,56,116,169,174,309,350)、
`--text-muted`(181,196,207,221,224,236)、`--text-on-accent`(35)（仅 `.iv-brand-icon`）。
边框：`--border`(27,50,83,91,157,162,175,209,216,270,292,388)。
强调：`--accent`(9,35,36,63,65,91,155,170,220)。
阴影/动效：`--shadow-sm`(122)、`--shadow-md`(125)、`--transition-fast`(123,132,218,279)。
`effects.css` token：`--obx-shadow-2`(92,271,294)。
**未引用的壳 token**：`--bg-sub-sidebar`（靠壳类生效）、`--bg-active`、`--bg-overlay(-strong)`、`--border-dark`、
`--danger`/`--danger-hover`/`--danger-soft`/`--success`/`--warning`、`--nav-width`、`--radius`/`--radius-sm`/`--radius-lg`、
`--transition-normal`。

#### 12.3.2 自有变量与 `:root`

**插件直接改写 `:root`**（`css:6-11`），这是壳 `variables.css:4-52` 的同名层：

```css
:root { --iv-radius: 14px; --iv-radius-sm: 10px; --iv-gradient: …; --iv-accent-soft: …; }
```

`--iv-radius`(14px)/`--iv-radius-sm`(10px) 与 `effects.css:14-15` 的 `--obx-radius:14px` / `--obx-radius-sm:10px`
数值完全相同，属重复声明。另有 3 处元素级内联样式由 JS 写：卡片 `left/top/width/height`（`app-grid.js:99`）、
进度条 `width`（`app-refresh.js:115-119`）、相册菜单 `left/top`（`app-nav.js:99-100`）。

#### 12.3.3 硬编码颜色字面量

grep（限 `image-viewer.css`）：
- 正则 `#[0-9a-fA-F]{3,8}\b` → **9 处**
- 正则 `rgba?\(` → **11 处**
- 正则 `hsla?\(` → **0 处**
- 三者合计命中行数：**17 行 / 404 行（4.2%）**
（`index.html` 内 0 处；插件 JS 内也无颜色字面量）

典型 5 例：

| 位置 | 字面量 | 说明 |
| --- | --- | --- |
| `css:103,143,260` | `rgba(0,0,0,0.55)` / `rgba(0,0,0,0.62)` | 相册数量徽标、圆圈数量角标、相册标签底（三种黑度并存） |
| `css:142-146` | `#fff` + `rgba(255,255,255,0.9)` 边框 + `rgba(0,0,0,0.35)` 阴影 | `.iv-count-badge` 单条规则里叠了 4 种字面量 |
| `css:241-242,259-263` | `#fff` + `rgba(0,0,0,0.25)` | `.iv-time-badge` / `.iv-album-tag`（同一套渐变底 + 黑阴影重复两遍） |
| `css:249,254` | `rgba(0,0,0,0.45)` → hover `rgba(0,0,0,0.65)` | `.iv-album-menu` 悬浮按钮及其 hover |
| `css:113` | `#ffd166` | 相册星标激活色（唯一的实色强调，与 `--warning:#ffc107` 不同源） |
| `css:359` | `var(--text-danger, #e5484d)` | 引用了**不存在的 token 名** `--text-danger`（壳只有 `--danger`），永远走兜底值 |

#### 12.3.4 `data-theme` 处理

插件侧 **零处理**：`grep -r "data-theme" plugins/image-viewer/frontend` 无命中。
主题由壳注入的引导脚本同步到 iframe 的 `<html>`（`shell/backend/file_server.py:70-76`，
含 `MutationObserver` 跟随父窗口变化），自定义颜色同样以 CSS 变量写到 iframe `documentElement`
（`:77-84`）。插件盲依赖这些 token，**未出现** `prefers-color-scheme` 或主题分支。

---

### 12.4 组件与命名约定

#### 12.4.1 自有类名前缀清单

主前缀 `iv-`，另有 4 个 `extension-view*`、6 个不再带前缀的 `rebuild-progress*`：

- 侧栏/品牌：`.iv-sidebar .iv-brand .iv-brand-icon .iv-brand-text .iv-brand-title .iv-brand-sub .iv-nav .iv-nav-label .iv-nav-item .iv-nav-footer .iv-sidebar-footer`
- 工具栏：`.iv-toolbar .iv-view-heading .iv-view-title .iv-view-sub .iv-search .iv-search-ico .iv-search-clear .iv-selection-count`
- 卡片/网格：`.iv-grid .iv-album .iv-album-cover .iv-cover-fallback .iv-album-badge .iv-album-star .iv-album-info .iv-album-name .iv-album-count .iv-album-menu .iv-album-tag .iv-album-tag-hot .iv-count-badge .iv-image-grid .iv-image-card`
- 时间线：`.iv-timeline-section .iv-time-label .iv-time-badge`
- 菜单：`.iv-context-menu`（自绘）
- 设置：`.iv-setting-item .iv-setting-section .iv-setting-section-first .iv-setting-note .iv-setting-note-after-input .iv-settings-body .iv-check .iv-field .iv-range`
- 空态：`.iv-empty .iv-empty-icon .iv-empty-text .iv-empty-hint`
- 进度：`.rebuild-progress-card .rebuild-progress-header .rebuild-progress-hide .rebuild-progress-body .rebuild-progress-text .rebuild-progress .rebuild-progress-bar .rebuild-progress-current .rebuild-progress-speed .rebuild-errors` + 关键帧 `obxRebuildSlide`（`css:343-346`，**借用壳 `obx` 命名空间**）
- 扩展视图：`.extension-view .extension-view-header .extension-view-body`
- 徽标：`.iv-album-badge`（张数）、`.iv-count-badge`（圆圈数字）、`.iv-album-tag(-hot)`（已收纳/已提升/空相册）、`.iv-time-badge`、`.iv-album-star`
- Toast **无自有实现**，全量用壳 `Toast.*`；骨架屏**无**（`.obx-skeleton` 未用）

#### 12.4.2 「壳已提供但插件又实现一遍」

| 能力 | 壳实现 | 插件实现 | 差异 |
| --- | --- | --- | --- |
| 右键菜单（图片） | `createContextMenu`，4 项一次传入（`app.js:58-66`） | **已用**，条目固定不重建；右键时只改变选中集再 `contextMenu.show(x, y, {url})`（`app.js:254-264`） | 符合契约（`document-reader` 是重建 `<li>` 列表的写法，两者不同） |
| 右键菜单（相册） | 同上 | **完全自绘** `.iv-context-menu`（`css:266-281` + `app-nav.js:96-134`）：`position:fixed` 但**无 z-index 之外的定位修正**，手算 `Math.min(e.clientX, innerWidth-160)`；条目 `<button>` 而非壳的 `<li>` | 视觉不同：壳 `radius:var(--radius)`=6px / `padding:4px 0` / 仅 `box-shadow`；插件 `radius:12px` / `padding:5px` / `backdrop-filter:blur(18px)` / 有 `obxPopIn` 入场动画。行为不同：插件菜单**不随滚动或窗口尺寸变化重定位**，且只有 `document.click` 关闭（`app.js:278`），无 `Escape` |
| 设置表单 | `createSettingsForm` + `.field*`（`base.js:419-563`） | 自绘 `.iv-setting-item`（flex + `justify-content:space-between`，`css:150-171`）+ `.iv-check` + `.iv-setting-section` 分隔标题 | 布局逻辑不同（壳是 label 在上、纵向 flex；插件是 label 左值右）；壳的 range 数值显示（`.field-range-value`）由插件改为 label 内联 `<span id="setting-row-height-val">`（`index.html:114`） |
| 统一设置弹窗 | `openSettingsModal`（自带保存 + 刷新 + Toast） | 自绘 `#settings-modal` + `openSettingsModal()/closeSettingsModal()/saveSettings()`（`app-settings.js:15-124`） | **命名冲突风险**：插件方法名与壳全局函数 `openSettingsModal` 同名（`base.js:566`）；插件保存后自行 `Toast.success('设置已保存')` + 条件 `Bridge.call('refresh')` + 重载视图（`app-settings.js:112-120`），未走壳的整页刷新 |
| 相册卡片网格 | `createCardGrid` | 未用；自绘 `.iv-album` + `_renderAlbumCards`（`app-albums.js:126-182`） | 卡片结构差异大（封面 `aspect-ratio:1` + 悬浮 ⋯ 按钮 + 三种标签 + 可选星标） |
| 空状态 | `.empty-state`（`base.css:428-431`） | `.iv-empty`（`css:190-196`）：`min-height:260px` + 44px 图标（`obxFloat` 无限浮动）+ 三行文案 | 多出图标与提示行；4 处调用（`app-albums.js:93`、`app-grid.js:50,62,200`） |
| 加载态 | `.obx-skeleton` | 复用壳 `.loading`（`app-grid.js:18` 写 `<div class="loading">图片加载中…</div>`） | 符合契约；但无骨架屏 |
| 分页 | `createPagination` + `.pagination-bar` | **已用**（`app.js:55-57`，`app-grid.js:60`） | 符合契约 |
| 灯箱 | `createLightbox` | **已用**（`app.js:54`，`app-grid.js:143,160,218,288`） | 符合契约 |
| 目录树 | `createTree` | **已用**（移动弹窗，`app.js:266-270`） | 符合契约；但容器 `#move-tree` 用 `.tree-container` 且行内 `min-height:200px`（`index.html:95`） |
| 目录选择器 | `FolderPicker`（`shell/folder-picker.js`） | **已迁走**（`app-nav.js:150-165`），设置弹窗只保留写回 `root_dir/extra_roots` | 符合契约（`css:185-188` 注释明确不再保留副本） |
| 工具栏右侧靠右 | `.toolbar-group` 自身无 `margin-left:auto` | 用行内样式 `style="margin-left:auto;"`（`index.html:41`） | 与 document-reader 的 `.nr-toolbar-right{margin-left:auto}`（CSS 类）做法不一致 |
| 滚动条 | `.obx-scroll` | 已挂 3 处（`index.html:19,59,106`） | 符合契约 |

---

### 12.5 交互约定

#### 12.5.1 设置入口与保存反馈

单入口：工具栏设置入口（静态 HTML `<svg class="obx-icon"><use href="#settings"></use></svg> 设置`，`index.html:55`）→ 自绘弹窗（`app.js:202` → `app-settings.js:15`）。
保存按钮 `保存并刷新`（`index.html:168`）→ `saveSettings()`：

- 作用域分支：勾选「仅应用于当前文件夹」时写 `Bridge.call('save_settings', this.currentPath, {...})`
  并单独写全局的二次排序；否则写全局并 `clear_folder_settings(currentPath)`（`app-settings.js:96-105`）。
- 反馈：`Toast.success('设置已保存')`（`:113`），随后若非文件夹级则 `await Bridge.call('refresh')`
  作废后端索引并 `loadAlbums()`，最后按当前模式 `loadImages`/`showAlbums`（`:114-120`）。
- 失败：`Toast.error('保存设置失败')`（`:122`）。
- 「图片文件夹」列表由 `FolderPicker` 持有，保存时 `roots[0]` → `root_dir`、其余 → `extra_roots`；
  列表为空时**显式写空串**以清掉旧值（`app-settings.js:88-95`）。

#### 12.5.2 错误提示方式

全量 Toast + 两处内联兜底，无错误页：

- 删除/移动部分失败：`Toast.error(\`部分删除失败: ${result.errors.join('; ')}\`)`（`app-grid.js:305`）、
  `部分移动失败`（`:333`）、`部分失败`（`app-refresh.js:168`）。
- 重建失败：`Toast.error(\`${label}失败：${e.message || e}\`)`（`app-refresh.js:80`），错误摘要同时写进
  `.rebuild-errors`（`css:355-362`，`max-height:60px; overflow-y:auto`，色 `var(--text-danger, #e5484d)`）。
- 列表/设置加载失败**静默**：`loadAlbums()` 与 `loadSettings()` 的 catch 均为空（`app-albums.js:20-22`、
  `app.js:126`），`openSettingsModal` 的 catch 也是空（`app-settings.js:61`）——失败时弹窗停在默认值。
- 图片加载失败有**内联重试**：`onerror` 先加 `?r=<时间戳>` 重试一次，再失败替换为
  `.iv-cover-fallback`（相册卡 `app-albums.js:157` 的行内 `onerror`；图片瓦片 `app-grid.js:111-120`）。

#### 12.5.3 选择模型

- **单图多选（点选）**：`isMultiSelectMode` + `selectedImages: Set`（`app.js:34-35`），
  卡片加 `.selected` 类（`css:126` `outline:2px solid var(--accent); outline-offset:-2px`），
  工具栏显示 `已选 N 项` 胶囊 `.iv-selection-count`（`app-grid.js:259-264`）。
- **右键即选**：右键未选中的图会先清空并选中它（`app.js:259-262`）。
- **无框选、无长按、无 shift 范围选**（grep `mousedown/mousemove/touchstart` 仅命中弹窗遮罩的
  `pointerdown`，`app.js:273-277`）。
- 多选模式开关改变按钮文案为 `退出多选`（`app-grid.js:250`）。

#### 12.5.4 右键菜单

两套并存，互不复用：

1. 图片（壳组件）：`#image-grid` 的 `contextmenu` 委托到 `.iv-image-card[data-url]`，
   4 项 —— 查看原图 / 多选此图 / 移动到此... / 删除（`danger`）（`app.js:59-64,254-264`）。
   相册卡片不参与（`:256` 因无 `dataset.url` 直接返回）。
2. 相册（自绘）：卡片右上角 `⋯` 按钮（悬浮才 `opacity:1`，`css:246-253`）→ `showAlbumMenu`，
   条目按条件组装（展开/收纳、提升/收回、不再显示此空相册、重建此相册缩略图），点击后 `_closeAlbumMenu()`（`app-nav.js:61-135`）。

#### 12.5.5 键盘快捷键

**无任何键盘处理**：`grep -n "keydown|keyup|keypress|keyCode|Escape|ArrowLeft"` 在
`plugins/image-viewer/frontend/js/*.js` 零命中。幻灯片只能靠工具栏按钮与灯箱内的箭头
（后者由壳的 `createLightbox` 提供，`base.js:806` `onKey`）。多选模式也**没有 Escape 退出**。

#### 12.5.6 长任务进度与取消

- 全量重建：`_startRebuildTask`（`app-refresh.js:54-85`）显示右下角 `#rebuild-progress` 卡，
  `_waitRebuildDone()` **每 500ms 轮询** `Bridge.call('rebuild_status')`（`:87-104`）。
- 卡片内容：`N / M` 计数、进度条（有 total 时按百分比并把 `animation` 置 `none`，无 total 时
  退回 40% 宽 + `obxRebuildSlide` 不确定动画，`app-refresh.js:112-121`）、当前项、速度与剩余时间
  （`张/秒 · 剩余约 …`，`:134`）、错误摘要。
- 取消：`#rebuild-progress-cancel` → `Bridge.call('rebuild_cancel')` + `Toast.info('正在取消全量重建…')`
  （`:152-159`）；另有 `—` 隐藏按钮 `hideRebuildProgress()`（`:147-150`，隐藏**不取消**）。
- 结果：完成 `Toast.success(\`${label}完成\`)`；取消 `Toast.warning(\`${label}已取消\`)`（`:64,78`）。
- 单相册重建、重生成缩略图走同一条 `_startRebuildTask`（`:48-52`，与 `rebuildFolder`/`refreshSelectedThumbs`）。

#### 12.5.7 空态 / 加载 / 错误态文案与样式类

| 态 | 样式类 | 文案 |
| --- | --- | --- |
| 无相册 | `.iv-empty`（`app-albums.js:93`） | `icon:images` + 「暂无相册」；提示行「点击左侧「新建相册」开始整理」/「换个关键词试试」 |
| 相册无图片 | `.iv-empty`（`app-grid.js:50`） | `icon:images` + 「此相册暂无图片」（无 hint） |
| 加载失败 | `.iv-empty`（`app-grid.js:62`） | `icon:triangle-alert` + 「图片加载失败」 |
| 搜索无果 | `.iv-empty`（`app-grid.js:200`） | `icon:search-x` + 「没有匹配的图片」；提示行「换个关键词试试」 |
| 加载中 | 壳 `.loading`（`app-grid.js:18`） | `图片加载中…` |
| 重建中 | `.rebuild-progress-*` | `正在扫描并生成缩略图，请稍候…` / `正在处理：<文件名>`（`app-refresh.js:125`） |
| 侧栏统计加载 | `.iv-sidebar-footer` | `正在读取…`（`index.html:29`），完成后 `N 个相册 · M 张图片`（`app-nav.js:147`） |
| 序列截断警告 | 壳 Toast | `相册较大，连续浏览序列已截断（前 5000 张）`（`app-grid.js:38`） |

---

### 12.6 特色设计（值得其他插件吸收）

1. **Justified 行式瀑布布局（`JustifiedLayout.compute`）而非 CSS 列瀑布**：
   先按 `ratio*targetHeight` 累加宽度切行，再按容器宽度反算该行统一高度，**末行不拉伸**
   （`justified-layout.js:29-33`），grid 容器统一给 `height: totalHeight`（`app-grid.js:74`）。
   解决的问题：CSS `columns` 瀑布的排列顺序是列优先（阅读顺序错乱）、且无法保证末行整齐；
   这里顺序即数据顺序，行高可随设置项（100–400px）实时变。
2. **「连续浏览序列」把分页/搜索/子文件夹统合成一个灯箱索引空间**：
   `currentAllImages` + `currentAllOffset` + `filteredSeqIndexes`（`app.js:22-24`），
   每个瓦片记录它在完整序列中的起点（`app-grid.js:81-90`），点击即 `lightbox.show(currentAllImages, seqIndex)`。
   解决的问题：第 2+ 页的瓦片、被搜索过滤后的瓦片、以及「子文件夹 = 该文件夹 p0 + 后续 p1…」三种偏移，
   用同一个序号空间表达，否则灯箱会错位打开到序列开头（注释 `app-grid.js:76-80`）。
3. **相册树折叠/提升是「视图配置」而非移动文件**：`albumConfig{collapsed,promoted,expanded,visible_empty_dirs}`
   （`app.js:29`），`_filterVisibleAlbums` 用 `readable`（递归可读数）+ 祖先折叠判定可见性
   （`app-albums.js:193-245`）。解决的问题：用户想「只看某个子树」「把深层作者目录拉到顶层」，
   但目录结构本身由磁盘决定、不能改；空目录还能在保留磁盘目录的前提下从视图隐藏（`app-nav.js:84-90`）。
4. **`_rememberScroll/_restoreScroll` + `navStack` 双重返回栈**：前者恢复滚动位置（`app-albums.js:356-371`，
   在 `requestAnimationFrame` 里写 `scrollTop` 以避开渲染重置），后者恢复「混合瀑布流逐层点入」的路径状态
   （`app.js:25`，`app-nav.js:11-59`）。解决的问题：相册树的深层钻取返回后不应回到顶部或根目录。
5. **生命周期钩子把「幻灯片」当作可暂停的视觉工作而非播放**：`onHide` 记住当前张数并停定时器、
   必要时收起灯箱；`onShow` 只在用户没主动停止（`_slideshowWanted`）时**原地续播**
   （`app.js:88-119`，`app-grid.js:206-238`）。解决的问题：常驻插件切到后台后页面不可见，
   继续 `setInterval` 换图纯属浪费；但用户切回来又希望接着看。
6. **缩略图失败双重兜底**：先 `?r=<时间戳>` 重试一次（绕过浏览器缓存与后端负缓存），
   再失败才换成 `Icons.html('icon:image-off')` 占位（`app-albums.js:157`、`app-grid.js:111-120`），并配 `dataset.r` 防重入。
   解决的问题：下载中断/替换后残留的坏缩略图会让整块区域永久空白。
7. **重建进度卡「隐藏 ≠ 取消」**：`—` 只隐藏卡片、任务继续；取消走独立按钮并要求确认
   （`app-refresh.js:42-52,147-159`）。解决的问题：长任务需要一个「先让开界面」的出口，
   而又不能让误点直接中断几分钟的批处理。

---

### 12.7 与 Shell 契约的偏差

逐条给证据与影响面（按影响从大到小）：

1. **插件改写 `:root`。**
   证据：`css:6-11` 在顶层 `:root` 定义 `--iv-radius:14px`、`--iv-radius-sm:10px`、`--iv-gradient`、`--iv-accent-soft`。
   影响面：1 个选择器 / 4 个变量；与壳 `variables.css:4-52` 落在同一元素，虽变量前缀不同不至于覆盖壳 token，
   但契约（§4.1「不重复定义壳提供的东西」）要求插件变量收在插件容器上——`document-reader` 就是定义在 `#app`（`css:18-26`），
   两个插件做法不一致。`--iv-radius/-sm` 的值与 `effects.css:14-15` 的 `--obx-radius/-sm` 完全相同，属重复真源。
2. **`.view-body` 被重定义。**
   证据：`css:368-370` `.view-body{position:relative}`（对应 `index.html:32`）。壳 `base.css:237` 已定义同类。
   影响面：1 条规则；`document-reader.css:704-706` 有一模一样的补充，说明这是「弹窗/覆盖层定位」共性需求，
   而两个插件各写一遍且都定义在通用类上。
3. **相册右键菜单完全自绘，绕过壳的 `.context-menu` 与 `createContextMenu`。**
   证据：`app-nav.js:96-101` 建 `div.iv-context-menu` 挂到 `document.body`；`css:266-281` 自定圆角 12px /
   毛玻璃 18px / `obxPopIn` 动画 / `<button>` 子元素。
   影响面：1 个菜单、最多 4 个条目；同插件内出现两套右键菜单视觉（图片项走壳的 `<li>` 列表）。
   行为缺口：不监听 `scroll`/`resize` 重新定位（`position:fixed` + 一次算好的坐标），
   窗口尺寸变化或页面滚动后菜单会脱离卡片；也不响应 `Escape`。
4. **`--text-danger` 是不存在的 token。**
   证据：`css:359` `color: var(--text-danger, #e5484d)`；壳只有 `--danger`（`variables.css:30`）与 `--danger-hover`。
   影响面：1 处（`.rebuild-errors` 错误文字）；永远落到硬编码兜底 `#e5484d`，
   暗色主题下 `--danger` 应为 `#f85149`，此处**不会**跟随主题，且用户自定义危险色也无效。
5. **设置弹窗自绘，未用 `createSettingsForm`/`openSettingsModal`，且方法与壳全局函数同名。**
   证据：`app-settings.js:15` 定义了 `openSettingsModal()` 方法，壳 `base.js:566` 也定义全局 `openSettingsModal`。
   影响面：1 个弹窗 / 约 12 个控件；同名导致阅读代码时无法判断调用的是哪一个（插件内 `this.openSettingsModal()`
   在 `app.js:202`）；壳的字段样式、`per-folder` 语义约定（`plugin-guide §8.5`）与保存后刷新逻辑都不生效——
   插件改为自己调 `Bridge.call('refresh')`。
6. **硬编码颜色集中在「图上叠加层」，暗色/自定义色下不跟随。**
   证据：`css:103,109,113,130,142-146,241-242,249,254,259-263` 共 17 行含字面量，占全文 4.2%
   （对比 `document-reader.css` 25 行 / 2.4%）；`rgba(0,0,0,0.55)` 出现 3 次不同黑度（0.45/0.55/0.62/0.65）。
   影响面：6 个叠加态元素（相册数量徽标、圆圈角标、时间徽标、相册标签、⋯ 按钮、卡片文件名渐变底）
   + 1 个星标色；这些一律叠在缩略图之上，因此「黑底白字」在浅色主题下尚可接受，
   但同一插件内出现 4 档黑度、星标 `#ffd166` 与 `--warning:#ffc107` 两个真源。
7. **工具栏右侧靠右用行内样式。**
   证据：`index.html:41` `<div class="toolbar-group" style="margin-left:auto;">`；
   `document-reader` 用类 `.nr-toolbar-right{margin-left:auto}`（`document-reader.css:146-151`）。
   影响面：1 处；行内样式无法被主题或统一 CSS 覆盖，且同仓库两种写法并存。
8. **空态未用壳 `.empty-state`，且 4 处模板与 `document-reader` 的 `.nr-empty` 结构重复。**
   证据：`app-utils.js:38-44` 的 `_emptyHtml(icon,text,hint)` 与 `document-reader` 的
   `app-shelf.js:139-145` 同构（仅类名前缀 `iv-empty` / `nr-empty` 不同）。
   影响面：4 个调用点 + 2 组 CSS 规则（`css:190-196` / `document-reader.css:813-836`）。
9. **`.iv-sidebar` 宽度写死 240px，未用 `var(--sub-sidebar-width)`。**
   证据：`css:23-29`，另 `css:198-201` 在 `max-width:860px` 时降为 200px。
   影响面：1 条规则；壳若调整 `--sub-sidebar-width`（现 240px），本插件侧栏不会跟随
   （`document-reader` 未写宽度，直接吃壳的变量，行为不同）。
10. **相册菜单缺少退出路径与重定位。**
    证据：仅 `document.addEventListener('click', () => this._closeAlbumMenu())`（`app.js:278`）
    与菜单内点击（`app-nav.js:105`）；无 `Escape`（见 12.5.5）、无 `scroll`/`resize` 监听。
    影响面：1 个菜单；键盘用户无法关闭，滚动后菜单悬空在错误位置。另 `⋯` 按钮靠 CSS `opacity:0`
    悬浮才出现（`css:250-253`），触屏设备上无法发现该入口。

## 13. 版本记录

| 版本 | 说明 |
|------|------|
| v2.3.0 | 混合瀑布流 + 「时间+文件名」排序标准 |
| v2.4.0 | Pixiv 排序两层嵌套 + 缩略图更新/全局刷新 |
| v2.4.1 | 作者页排序栏（文件名/更新时间/图片数量 + 正倒序） |
| v2.4.2 | SQLite 缩略图缓存（58bf8d2）→ 全量重建后台任务/进度/取消（a2044f4–5f0af50）→ 并行生成与指定文件夹重建 → PyInstaller sqlite3 显式打包（827d76b）→ 性能优化（TTL/并行/脏标记，58d6ea4） |
| v2.4.3 | 缩略图缓存与重建任务迁移到 Shell 共享基建（ThumbCache / BackgroundTask，d7c8620），API 与行为不变 |
| 未发布 | Pixiv 树封面改为作品号最大的一张（画师最近的作品）→ 相册索引缓存 v4；作者网格二次排序从显示页排序栏移入设置页并落库（`album_sort_by` / `album_sort_order`，默认更新时间/倒序，仅生效 Pixiv 排序时显示）；修复全局设置保存后读不回的缺陷，并让历史位置 `__global__` 只作补齐、不压住新保存的值 |
| 未发布 | 新增「模糊匹配」（`pixiv_fuzzy`）：在 Pixiv 排序下让「作者/作品名/序号.jpg」这类非数字命名的图库套用同一套排序与两层浏览效果；`pixiv_explicit`（配置点）语义扩展为自己存过 `sort_by=time_name` 或 `pixiv_fuzzy` |
| 未发布 | 三个功能增强：① **多根目录**（`extra_roots`，第二根起以 `__<目录名>` 命名空间节点作为顶层，`depth` 按根内相对路径计算，`get_file_roots()` 返回全部根，设置页新增「图片文件夹」列表与目录选择器）；② **空目录隐藏**（递归无可读图片的目录不下发显示，「新建相册」记入 `visible_empty_dirs` 保留可见，新增 `delete_folder` 删除空目录）；③ **子相册默认折叠**（只有显式 `expanded` 的目录显示下级，`collapsed` 优先） |
| 未发布 | 图片文件夹交互收敛：设置弹窗正文改为可滚动（与媒体播放器一致），「图片文件夹」移到最前、删除多余的「数据根目录」输入框（列表即唯一入口）；目录选择器改用共享基建 `media_catalog.list_subdirectories()`，标注含图片/视频/音乐并支持一键回到「我的电脑」层 |
| 未发布 | 「图片文件夹」列表与目录选择器**整体搬到 Shell 共享组件** `window.FolderPicker`（`shell/frontend/public/shell/folder-picker.{js,css}`）：media-player / manga-library / novel-reader 的文件夹位置改用同一套实现（`settings_schema` 的 `type:"directory"`），本插件改为引用回来，不再各自实现一份；类名与数值原样搬迁 |
