# Pixiv 同步插件（pixiv-sync）设计文档

> 版本：v0.1（已实装）
> 目标形态：**Companion 插件**

## 1. 定位

`pixiv-sync` 是 `image-viewer` 的伴侣插件，提供 **Pixiv 关注画师新作** 与 **当前用户收藏画作** 的同步下载能力，把画作写入宿主相册根目录后由 `image-viewer` 自动展示。

### 1.1 与宿主的关系

- 使用 `manifest.dependencies: ["image-viewer"]` 声明依赖，由 `PluginManager` 保证宿主先加载。
- 后端通过 `PluginBase.get_dependency('image-viewer')` 获取宿主实例，复用：
  - `get_data_root()`：默认下载根目录（相册根），画作写入后自动出现在相册
  - `get_file_roots()` / `/thumbs`：缩略图与文件服务体系（无需改动宿主）
- 不修改 `image-viewer` 宿主代码；通过 `get_extensions()` 注册到宿主左侧栏，以 iframe 内嵌方式打开（与 `image-cleaner` 同构）。
- `manifest.hidden: true`：不出现在 Shell 主导航，仅通过 image-viewer 左侧栏入口访问。

### 1.2 同步范围

| 任务 | API | 范围 |
|------|-----|------|
| 同步画师 | `v2/illust/follow` | 当前用户关注画师的新作流，翻页拉取全部 |
| 同步喜欢 | `v1/user/bookmarks/illust` | 当前用户公开收藏，翻页拉取全部 |

- 多图作品（`meta_pages`）下载全部页。
- 去重：以 Pixiv 作品 ID（`illust_id`）为准，全局唯一且稳定；画师同步与喜欢同步共用同一去重集合，同一作品只在先到来源下载一次。

## 2. 目录结构

```
plugins/
└── pixiv-sync/
    ├── manifest.json            # dependencies:["image-viewer"], hidden:true
    ├── backend/
    │   ├── main.py              # PixivSyncPlugin（PluginBase 子类）
    │   └── libs/
    │       └── pixiv_mini.py    # 精简 Pixiv 客户端（纯 requests，放 libs 随插件分发）
    └── frontend/
        └── index.html           # 极简单页：状态 / 按钮 / 进度条 / 设置
```

下载落盘结构（相对下载根目录）：

```
<root>/
├── pixiv/
│   ├── following/{画师名}/                       # 按画师分目录，纯名字命名（非 id_名字）
│   │   ├── 123456.jpg                            # 单图：直接平铺
│   │   └── 123456/                               # 多图：放入 {作品id}/ 子文件夹
│   │       ├── 123456_p0.jpg
│   │       └── 123456_p1.jpg
│   └── bookmarks/
│       ├── 123456.jpg
│       └── 123456/
│           ├── 123456_p0.jpg
│           └── 123456_p1.jpg
└── .cache/pixiv-sync/
    ├── downloaded_ids.json      # 已下载 illust_id 集合（去重）
    ├── artists.json             # 画师 id → 最新名字 缓存（改名识别）
    └── tasks.json               # 任务断点（每张一写）
```

`<root>` 默认取宿主 `get_data_root()`（相册根），可被设置项 `download_dir` 覆盖；`.cache` 隐藏目录不会进入相册扫描。

### 画师目录规则

- 文件夹使用**画师当前名字**（sanitize 后），不加 id 前缀，保证可读性。
- 画师 id → 名字 记入本地缓存 `artists.json`；同名画师以 id 区分（缓存 key）。
- 画师改名后：新作品进入新名字目录，同时自动把**旧名字目录迁移合并**到新名字目录（不覆盖同名文件），避免历史作品"分家"。

## 3. 设置项（settings_schema）

| key | 类型 | 说明 |
|-----|------|------|
| `refresh_token` | text | Pixiv OAuth refresh token（密码登录已废弃）；`central: false` 不出现在集中设置面板 |
| `proxy` | text | HTTP 代理，如 `http://127.0.0.1:7890`；留空 = 直连 |
| `download_dir` | text | 下载根目录；留空 = 宿主相册根目录 |
| `download_original` | checkbox | 默认开启：下载画师原图（original，完整分辨率）；关闭 = 下载 1200px 大图（master1200） |
| `multi_page_subfolder` | checkbox | 默认开启：多图作品放入 `{作品id}/` 子文件夹，单图直接平铺；关闭 = 全部平铺 |
| `workers` | number | 并发下载数（1-8，默认 4）：画师间/作品间并行下载；机械盘建议 1-2，SSD 可 4-8 |

设置持久化于 `.config/plugins/pixiv-sync.json`（SettingsStore，git 忽略）。

## 4. 后端 API 契约（pixiv-sync__*）

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `get_status` | - | `{task, root_dir, token_configured, downloaded_total, running}` | 状态轮询 |
| `sync_following` | - | `{ok, data\|error}` | 启动「同步画师」任务（后台线程） |
| `sync_bookmarks` | - | `{ok, data\|error}` | 启动「同步喜欢」任务（后台线程） |
| `cancel_task` | - | `{ok}` | 请求取消当前任务（下个检查点生效） |
| `get_settings` / `save_settings` | - | 设置读写（继承 PluginBase） | 前端设置表单使用 |

调用约定：
- 启动接口返回 `{ok: false, error: "已有同步任务在运行"}` 拒绝并发任务（串行约束）。
- 长任务遵循 `docs/image-tagger-design.md` §6 的状态约定：
  `queued → running → done | failed | cancelled`；重启后 `running/queued` 恢复为 `paused`（可重新同步续跑，靠去重集合天然断点续传）。

## 5. 同步逻辑

### 5.1 流程

1. `auth(refresh_token)` 换取 access_token（`oauth.secure.pixiv.net/auth/token`）。
2. **同步画师**：先翻页拉取全部关注新作（确定 `total`），按画师分组（保持新作流出现顺序），主线程预解析画师目录（名字命名 + 缓存 + 改名迁移），然后**画师级并行下载**（`ThreadPoolExecutor`，并发数 = `workers`）：多个画师同时下载，单个画师内部串行连续处理，保证单画师完整性。
3. **同步喜欢**：翻页拉取当前用户公开收藏（全量），统一并行下载到 `bookmarks/` 目录。
4. 逐个作品：`illust_id` 已在去重集合 → `skipped`；否则提取全部页 URL 下载 → 成功后加入集合。任务计数 / 去重集合 / 断点文件写入由 `_task_lock` 保护（线程安全）。
5. 任务状态每处理一个作品写入 `tasks.json`；去重集合在任务结束时落盘（也支持中途崩溃后按已下载文件跳过）。

### 5.2 下载

- 复用 `pixiv_mini.PixivClient.download()`：requests 流式下载，自动带 `Referer: https://app-api.pixiv.net/` 防盗链头，自动创建目标目录，已存在文件不覆盖。
- **清晰度**：默认取画师原图 `original`（完整分辨率，`meta_single_page.original_image_url` / `meta_pages[].image_urls.original`，实测可达 4000+px）；取不到时回退 1200px `large`（master1200）。设置项 `download_original` 可关闭原图回退为大图。
- 文件名：扩展名从 URL 真实提取（原图可能为 `.png`），单图 `{illust_id}{ext}`，多图 `{illust_id}_p{页码}{ext}`（pixiv 原生命名规则）。
- **存放位置**：开启 `multi_page_subfolder` 时多图作品放入 `{作品id}/` 子文件夹（与 pixiv 命名一致，浏览直观），单图直接平铺。
- 单页失败计入 `failed` 且不入去重集合（下次重试）；部分页成功即视为已下载（重复文件自动跳过，幂等）。
- 注意：已下载的旧图（如关闭原图时存的 1200px 版）不会自动升级，需删除对应文件或清空 `downloaded_ids.json` 后重新同步。

### 5.3 依赖

仅 `requests`（宿主 venv 已内置 2.34.2），放在 `backend/libs/` 随插件分发（PluginManager 加载时自动加入 `sys.path`）。无重依赖，不需要 `runtime` / 独立 venv / stdio-worker。

## 6. 前端

`/plugins/pixiv-sync/frontend/index.html` 作为内嵌页面，通过 `get_extensions()` 在 image-viewer 左侧栏挂载「Pixiv 同步」入口；点击后由 image-viewer 用 iframe 加载。页面包含：

- 状态栏：Token 是否配置 / 下载根目录 / 已下载总数 / 上次任务结果
- 操作：🔄 同步画师、❤️ 同步喜欢 两个按钮；运行中禁用并显示「取消」
- **进度条**：`done/total` 百分比（流式累加）+ 计数明细（下载/跳过/失败）+ 当前处理作品；每 1.5s 轮询 `get_status`
- 设置表单：refresh_token（密码框）/ 代理 / 下载目录 + 保存

布局复用 Shell 的 `.view-body` / `.view-toolbar` / `.view-content` 类与主题变量。

## 7. 非目标（第一版不做）

- 不做定时自动同步（仅手动按钮触发）。
- 不做单画师全量补档（`user_illusts` 已具备接口能力，留待后续）。
- 不做小说 / 搜索 / 收藏写操作等 Pixiv 其他能力。
- 不做断点续传的「精确到页」恢复（去重为作品级，重复文件下载幂等跳过）。
