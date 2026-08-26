# Pixiv 同步插件（pixiv-sync）设计文档

> 版本：v0.2（已实装）
> 目标形态：**Companion 插件**

## 1. 定位

`pixiv-sync` 是 `image-viewer` 的伴侣插件，提供 **Pixiv 关注画师作品** 与 **当前用户收藏画作** 的同步下载能力，把画作写入宿主相册根目录后由 `image-viewer` 自动展示。

### 1.1 与宿主的关系

- 使用 `manifest.dependencies: ["image-viewer"]` 声明依赖，由 `PluginManager` 保证宿主先加载。
- 后端通过 `PluginBase.get_dependency('image-viewer')` 获取宿主实例，复用：
  - `get_data_root()`：默认下载根目录（相册根），画作写入后自动出现在相册
  - `get_file_roots()` / `/thumbs`：缩略图与文件服务体系（无需改动宿主）
- 不修改 `image-viewer` 宿主代码；通过 `get_extensions()` 注册到宿主左侧栏，以 iframe 内嵌方式打开（与 `image-cleaner` 同构）。
- `manifest.hidden: true`：不出现在 Shell 主导航，仅通过 image-viewer 左侧栏入口访问。

### 1.2 同步范围与相册语义

| 任务 | API | 范围 |
|------|-----|------|
| 同步画师 | `user_following` + `user_illusts` | 全部关注画师的**完整作品库**（逐画师翻页拉全量，含历史作品） |
| 同步喜欢 | `v1/user/bookmarks/illust` | 当前用户公开收藏，翻页拉取全部，**按作品画师归入对应画师目录**（只存收藏列表里的作品，不拉收藏画师全量） |

- **两个相册合并**：本地库是统一的 `pixiv/{画师名}/` 结构，作品**归属画师**、不再区分来源（关注/收藏）。画师同步与喜欢同步共用同一去重集合：关注画师的作品若已被画师同步下载，喜欢同步自动跳过（不重复下载）。
- **互不干扰**：同步画师只更新关注画师、同步喜欢只更新收藏，各自独立。
- **永不删除**：本地是累积库——取关、取消喜欢都不删除已下载图片。
- 多图作品（`meta_pages`）下载全部页。

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

下载落盘结构（相对下载根目录，v0.2 统一画师目录）：

```
<root>/
├── pixiv/
│   └── {画师名}/                              # 所有画师（关注+收藏）统一目录，纯名字命名
│       ├── 123456.jpg                         # 单图：直接平铺
│       └── 123456/                            # 多图：放入 {作品id}/ 子文件夹
│           ├── 123456_p0.jpg
│           └── 123456_p1.jpg
└── .cache/pixiv-sync/
    ├── downloaded_ids.json      # 已下载 illust_id 集合（去重，两同步共用）
    ├── artists.json             # 画师 id → 最新名字 缓存（改名识别）
    ├── selected_artists.txt     # 画师名单（可选：只同步指定画师）
    └── tasks.json               # 任务断点（每张一写）
```

`<root>` 默认取宿主 `get_data_root()`（相册根），可被设置项 `download_dir` 覆盖；`.cache` 隐藏目录不会进入相册扫描。

### 旧目录自动迁移（v0.1 → v0.2）

首次同步时自动执行一次迁移，无需手动操作：

- `pixiv/following/{画师}/` → `pixiv/{画师}/`（目录上移/合并）；
- `pixiv/bookmarks/` 中的文件：**与已有画师作品 id 相同的直接归位**（画师名从目录推断，零联网）；其余文件**联网 `illust_detail` 查询画师名后归位**（限速 3/s，失败保留原地，日志提示）；
- 迁移后 `following/`、`bookmarks/` 目录删除；无法识别的自定义命名文件保留在 `bookmarks/`（不删除用户文件）。

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
| `pixeval_dir` | text | 第三方客户端 pixeval 下载目录；设置后同步时自动导入（见下） |

### pixeval 目录导入（第三方客户端兼容）

设置 `pixeval_dir`（如 `G:\图库\PIXEVAL`）后，通过前端「📥 导入 Pixeval」按钮（API `import_pixeval`）**手动触发**，不随同步自动执行；纯本地文件操作，无需登录。

- 结构：`<pixeval_dir>/{画师名}/{图片}`，兼容三种命名：`{id}.png`（单图）、`{id}p{页码}.png`（pixeval 原生，单图/多图都带页码）、`{id}_p{页码}.jpg`（官方格式）、`{id}_p{页码}(1).jpg`（重复副本）。
- 目标：按画师目录移入 `pixiv/{画师名}/`，**文件名规范化**为本地规则：单图 `{id}{ext}`、多图 `{id}/{id}_p{页码}{ext}`（无 `p1+` 的文件视为单图，去掉 `p0`）。
- **重复处理**：作品 id 已在本地去重集合 → 删除 pixeval 副本（以本地为准）；目标已有同名文件 → 删除源（幂等）。
- 非图片/无法识别的文件保留原地；迁移后新 id 并入去重集合，避免随后同步重复下载。
- 复用长任务状态机（`kind: "pixeval"`），前端进度条/取消可用。

设置持久化于 `.config/plugins/pixiv-sync.json`（SettingsStore，git 忽略）。

### 画师名单（selected_artists.txt：只同步指定画师）

- 配置文件位置：`<root>/.cache/pixiv-sync/selected_artists.txt`（前端设置区「📂 画师名单文件」按钮一键打开所在文件夹）。
- 格式：每行一个画师，填**画师名字或 Pixiv 用户 id**；`#` 开头为注释、空行忽略。
- 语义：文件**存在且非空**时，同步画师只处理名单中匹配到的画师（按名字或 id 匹配；全部匹配不到则任务报错提示）；文件**不存在或为空** = 同步全部关注画师。
- 前端不做名单编辑 UI（避免复杂表单），直接编辑文本文件即可。

## 4. 后端 API 契约（pixiv-sync__*）

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `get_status` | - | `{task, root_dir, token_configured, downloaded_total, running, selected_artists, selected_file}` | 状态轮询 |
| `sync_following` | - | `{ok, data\|error}` | 启动「同步画师」任务（后台线程） |
| `sync_bookmarks` | - | `{ok, data\|error}` | 启动「同步喜欢」任务（后台线程） |
| `cancel_task` | - | `{ok}` | 请求取消当前任务（下个检查点生效） |
| `open_config` | - | `{ok, file}` | 打开画师名单配置文件所在文件夹（不存在则创建带说明的空文件） |
| `start_oauth` | - | `{ok, url}` | OAuth PKCE 第一步：生成 code_verifier 并打开 Pixiv 登录页 |
| `finish_oauth` | `code` | `{ok, user_id}` | OAuth PKCE 第二步：用授权码换 token 并自动保存 refresh_token |
| `get_settings` / `save_settings` | - | 设置读写（继承 PluginBase） | 前端设置表单使用 |

### 内置 OAuth 向导（refresh_token 失效时重新获取）

前端设置区「🔑 获取 Token」按钮，内置 Pixiv OAuth PKCE 授权码流程（RFC 7636，无需 gppt/selenium）。`start_oauth()` 生成 code_verifier（存插件内存）并返回完整登录 URL（含 code_challenge），流程：

1. 点按钮 → `webbrowser.open` 打开登录页，弹窗显示完整登录 URL（可复制）；
2. 在**已登录**的浏览器新标签页打开该 URL（Google 登录等跳转异常时手动粘贴地址栏）；
3. 页面跳转或报「协议未知，无法导航」时，从 F12 Console / Network 中抓取 `callback?…&code=XXXX` 的 code 值；
4. `finish_oauth(code)` 用内存 verifier + code 换取 token，**自动 `update_setting` 保存**并重置客户端。

关键约束：code 有效期几分钟，且**必须与当前按钮轮次的 verifier 匹配**——操作中不要重新点「获取 Token」，否则旧 code 失效。

已知限制（实测结论）：浏览器控制台 fetch 会因 app-api 响应无 CORS 头被拦截；requests/curl_cffi 带浏览器 cookie（PHPSESSID/cf_clearance）无法通过 Cloudflare TLS 指纹校验——因此自动化取 code 不可行，采用上述手动导航流程。若 Pixiv 修改认证流程，仍可用 gppt 兜底。

调用约定：
- 启动接口返回 `{ok: false, error: "已有同步任务在运行"}` 拒绝并发任务（串行约束）。
- 长任务遵循 `docs/image-tagger-design.md` §6 的状态约定：
  `queued → running → done | failed | cancelled`；重启后 `running/queued` 恢复为 `paused`（可重新同步续跑，靠去重集合天然断点续传）。

## 5. 同步逻辑

### 5.1 流程

1. `auth(refresh_token)` 换取 access_token（`oauth.secure.pixiv.net/auth/token`）。
2. **同步画师（完整作品库）**：
   - `user_following` 翻页拉取全部关注画师；
   - 并行（4 路）逐画师 `user_illusts` 翻页拉取**全部作品**（不再使用 `illust_follow` 新作流——那只会返回近期作品，历史作品会漏，实测单个画师完整作品可达数百张）；
   - 主线程预解析画师目录（名字命名 + 缓存 + 改名迁移）；
   - 汇总后**全局并行下载**（`workers` 并发，不关心画师顺序，速度优先）。
3. **同步喜欢**：翻页拉取当前用户公开收藏（全量），**按作品画师归入 `pixiv/{画师名}/`**（只存收藏列表里的作品，不拉收藏画师全量）。与画师同步共用去重集合：关注画师的作品已被画师同步下载则自动跳过；非关注画师的作品下载到其画师目录。画师目录由 `_artist_dir()` 统一解析（名字命名 + 缓存 + 改名迁移）。
4. 逐个作品：`illust_id` 已在去重集合 → `skipped`；否则提取全部页 URL 下载 → 成功后加入集合。任务计数 / 去重集合 / 断点文件写入由 `_task_lock` 保护（线程安全）。

### 5.1.1 限流保护（pixiv 429 Rate Limit）

pixiv app-api 有滑动窗口限流（约 30 req/10s，超出后 429；大量请求后冷却可能长达数小时）：

- 全局令牌桶限速器 `_RateLimiter(3.0)`：所有 app-api 请求（关注列表 / 画师列表 / 收藏翻页）统一限速 3 req/s。
- 429 时自动退避重试（5s / 10s / 15s，最多 3 次），仍失败则跳过该画师并在任务错误中提示。
- 图片下载走 `i.pximg.net`（CDN），不受 app-api 限流影响，可保持 `workers` 并发。
- 首次全量同步请求量大（154 画师 ≈ 1500+ 次列表请求 ≈ 8 分钟），请耐心等待拉取阶段（前端实时显示已拉取作品数）；**若遇 429 冷却，建议等待数小时或次日再同步**。
5. 任务状态每处理一个作品写入 `tasks.json`；去重集合在任务结束时落盘（也支持中途崩溃后按已下载文件跳过）。

### 5.2 下载

- 复用 `pixiv_mini.PixivClient.download()`：requests 流式下载，自动带 `Referer: https://app-api.pixiv.net/` 防盗链头，自动创建目标目录，已存在文件不覆盖。
- **清晰度**：默认取画师原图 `original`（完整分辨率，`meta_single_page.original_image_url` / `meta_pages[].image_urls.original`，实测可达 4000+px）；取不到时回退 1200px `large`（master1200）。设置项 `download_original` 可关闭原图回退为大图。
- 文件名：扩展名从 URL 真实提取（原图可能为 `.png`），单图 `{illust_id}{ext}`，多图 `{illust_id}_p{页码}{ext}`（pixiv 原生命名规则）。
- **存放位置**：开启 `multi_page_subfolder` 时多图作品放入 `{作品id}/` 子文件夹（与 pixiv 命名一致，浏览直观），单图直接平铺。
- 单页失败计入 `failed` 且不入去重集合（下次重试）；文件已存在（`download()` 返回 False）也视为已下载并入去重集合（幂等）。
- **旧图导入**：每次同步开始前自动扫描 `<root>/pixiv/` 下已有图片，按命名规则（`{id}.jpg` / `{id}_p0.jpg` / 子文件夹 `{id}/`）提取作品 id 并入去重集合——**用户手动放入的旧图会被识别，全量更新直接跳过，不会重复下载/检查**；文件名不符合规则的图片无法自动识别（可手动改名或删文件重下）。
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
- 不做小说 / 搜索 / 收藏写操作等 Pixiv 其他能力。
- 不做断点续传的「精确到页」恢复（去重为作品级，重复文件下载幂等跳过）。
