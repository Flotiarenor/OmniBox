# Pixiv 同步插件（pixiv-sync）设计文档

> 版本：v0.3（已实装，移除了旧兼容/迁移与多余开关）
> 目标形态：**Companion 插件**

## 1. 定位

`pixiv-sync` 是 `image-viewer` 的伴侣插件，提供 **Pixiv 关注画师作品** 与 **当前用户收藏画作** 的同步下载能力，把画作写入宿主相册根目录后由 `image-viewer` 自动展示。

### 1.1 与宿主的关系

- 使用 `manifest.dependencies: ["image-viewer"]` 声明依赖，由 `PluginManager` 保证宿主先加载。
- 后端通过 `PluginBase.get_dependency('image-viewer')` 获取宿主实例，复用：
  - `get_data_root()`：默认下载根目录（相册根），画作写入后自动出现在相册
  - `get_file_roots()` / `/thumbs`：缩略图与文件服务体系（无需改动宿主）
- 不修改 `image-viewer` 宿主代码；通过 `get_extensions()` 注册到宿主左侧栏，以 iframe 内嵌方式打开（与 `image-cleaner` 同构；每个扩展有独立 `section` 分区标题）。
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
    ├── manifest.json            # dependencies:["image-viewer"], hidden:true, libs:["backend/libs","backend"]
    ├── backend/
    │   ├── main.py              # PixivSyncPlugin（PluginBase 子类，入口，薄）
    │   ├── libs/
    │   │   └── pixiv_mini.py    # 精简 Pixiv 客户端（纯 requests，第三方 vendored，来自 upbit/pixivpy）
    │   └── pixiv_sync/          # 自身实现包（自写代码，不放 libs）
    │       ├── __init__.py
    │       ├── limiter.py       # _RateLimiter（令牌桶限速 + 429 转 RateLimitError）
    │       ├── tasks.py         # 任务状态机：创建 / 落盘 tasks.json / 重启恢复 paused
    │       ├── store.py         # downloaded_ids.json / failed_ids.json 读写 + 已有图片扫描
    │       ├── db.py            # 待下载清单 SQLite（works.db：works/tags/work_tags/meta）
    │       ├── artist.py        # 画师目录解析：名字命名 / id→名字缓存 / 改名迁移
    │       ├── scan.py          # 刷新清单：关注（画师级断点 done_uids）/ 收藏（断点 next_qs=max_bookmark_id）
    │       ├── download.py      # 按清单并行下载 + 404 永久跳过 + 固定下载行为
    │       ├── oauth.py         # 内置 OAuth PKCE 向导
    │       ├── pixiv_purge_non_original.py   # 伴侣工具：清理 1200px 非原图（交互/参数两模式）
    │       └── pixiv_unmark_recent.py        # 伴侣工具：把已下载记录改回未下载（只改记录）
    └── frontend/
        └── index.html           # 极简单页：状态 / 按钮 / 进度条 / 设置
```

下载落盘结构（相对下载根目录）：

```
<root>/
├── pixiv/
│   └── {画师名}/                              # 所有画师（关注+收藏）统一目录，纯名字命名
│       ├── 123456.jpg                         # 单图：直接平铺
│       └── 123456/                            # 多图：放入 {作品id}/ 子文件夹（固定行为）
│           ├── 123456_p0.jpg
│           └── 123456_p1.jpg
└── .cache/pixiv-sync/
    ├── downloaded_ids.json      # 已下载 illust_id 集合（去重，两同步共用）
    ├── failed_ids.json          # 404/已删除、永久跳过的 id 集合
    ├── artists.json             # 画师 id → 最新名字 缓存（改名识别）
    ├── selected_artists.txt     # 画师名单（可选：只同步指定画师）
    ├── works.db                 # 待下载清单（SQLite，含扫描断点与 done 标记）
    └── tasks.json               # 任务断点（每张一写）
```

> 说明：早期版本有 `download_original` / `multi_page_subfolder` 两个前端开关，并存在
> `following/`+`bookmarks/` 旧目录迁移与 pixeval 第三方导入等兼容逻辑。**v0.3 已全部移除**：
> 下载原图、多图子文件夹为**固定默认行为（无开关）**，旧目录/pixeval 不再导入（纯本地自写代码，
> libs 目录只保留第三方 pixiv_mini）。老版本留下的目录与记录不会自动迁移，如需整理可手动处理。

### 画师目录规则

- 文件夹使用**画师当前名字**（sanitize 后），不加 id 前缀，保证可读性。
- 画师 id → 名字 记入本地缓存 `artists.json`（缓存 key 按 id 区分）。两个不同 id 但
  sanitize 后同名的**新画师**会消歧为 `名字 (uid)`；历史版本已经共用的同名目录保持不动，
  避免迁移时误搬另一位画师的文件。
- 画师改名后：新作品进入新名字目录，同时自动把**旧名字目录迁移合并**到新名字目录（不覆盖同名文件），避免历史作品“分家”。

## 3. 设置项（settings_schema）

| key | 类型 | 说明 |
|-----|------|------|
| `refresh_token` | text | Pixiv OAuth refresh token（密码登录已废弃）；`central: false` 不出现在集中设置面板 |
| `proxy` | text | HTTP 代理，如 `http://127.0.0.1:7890`；留空 = 直连 |
| `download_dir` | text | 下载根目录；留空 = 宿主相册根目录 |
| `workers` | number | 并发下载数（1-8，默认 4）：画师间/作品间并行下载；机械盘建议 1-2，SSD 可 4-8 |
| `max_download` | number | 单次同步上限（默认 100，0=不限）：每次「同步画师/同步喜欢」最多下载条数，下完再点同步继续（分批推进） |
| `max_artists` | number | 单次刷新画师数上限（默认 30，0=不限）：每次「刷新关注名单」最多扫描的画师数；实测约 30 个画师可能触发 429，建议保持默认 |
| `rate_limit` | number | API 请求速率次/秒（默认 3，1-10 可调）：间隔带随机抖动（-20%~+40%）；pixiv 阈值约 3/s，调高有 429 风险 |
| `scan_workers` | number | 刷新名单的并行拉取画师数（滑动窗口，默认 4，1-8）：同时最多 N 个画师在拉，完成一个补充一个；总速率仍受 `rate_limit` 限制 |

> **固定行为（v0.3，无开关，前端不再提供选项）**：
> - **下载原图（original，完整分辨率）默认开启**，取不到时回退 1200px 大图（master1200）。
> - **多图作品始终放入 `{作品id}/` 子文件夹**（与 pixiv 命名一致，浏览直观），单图直接平铺。
> 这两项是代码层固定默认，不暴露设置项，避免误触把历史作品降为 1200px 大图或打散目录。
> 如需调整，直接修改 `backend/pixiv_sync/download.py`：`all_image_urls` 的 `want_original`
> 与 `process_illust` 的 `subfolder` 判定。

设置持久化于 `.config/plugins/pixiv-sync.json`（SettingsStore，git 忽略）。

### 画师名单（selected_artists.txt：只同步指定画师）

- 配置文件位置：`<root>/.cache/pixiv-sync/selected_artists.txt`（前端设置区「📂 画师名单文件」按钮一键打开所在文件夹）。
- 格式：每行一个画师，填**画师名字或 Pixiv 用户 id**；`#` 开头为注释、空行忽略。
- 语义：文件**存在且非空**时，同步画师只处理名单中匹配到的画师（按名字或 id 匹配；全部匹配不到则任务报错提示）；文件**不存在或为空** = 同步全部关注画师。
- 前端不做名单编辑 UI（避免复杂表单），直接编辑文本文件即可。

## 4. 后端 API 契约（pixiv-sync__*）

| API | 参数 | 返回 | 说明 |
|-----|------|------|------|
| `get_status` | - | `{task, root_dir, token_configured, downloaded_total, running, selected_artists, selected_file, ...统计}` | 状态轮询 |
| `sync_following` | - | `{ok, data\|error}` | 启动「同步画师」任务（按清单下载，含 404 永久跳过） |
| `sync_bookmarks` | - | `{ok, data\|error}` | 启动「同步喜欢」任务（按清单下载，与画师共用去重） |
| `refresh_following_lists` | - | `{ok, data\|error}` | **刷新关注画师作品名单**：首次/未完成时全量扫描；完成后改为增量扫描（每个画师只拉到上一轮已入库的尾巴），旧→新排序 |
| `refresh_bookmarks_lists` | - | `{ok, data\|error}` | **刷新喜欢画作名单**：首次/未完成时全量扫描；完成后改为增量扫描（只拉到上一轮已入库的收藏尾巴） |
| `refresh_downloaded` | - | `{ok, total, zero_removed, stale_removed, failed_cleared}` | **刷新已下载记录**：扫描本地重建 ids（手动删的移除、手动加的导入、0 字节清理），重置消失作品的 done 快照，并清除本地已有文件对应的失败跳过记录 |
| `verify_downloaded` | - | `{ok, stale_removed, zero_removed, failed_cleared, total}` | **校验已下载内容**：移除记录中本地无有效文件的失效 id 并重置清单 done 快照（下次同步重下），不导入新增 |
| `retry_failed` | - | `{ok, cleared, kind}` | **一键重试失败作品**：清除 failed_ids.json（404 永久跳过记录）并立即按最近任务来源重新同步一次 |
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

调用约定：
- 启动接口返回 `{ok: false, error: "已有同步任务在运行"}` 拒绝并发任务（串行约束）。
- 长任务遵循 `docs/image-tagger-design.md` §6 的状态约定：
  `queued → running → done | failed | cancelled`；重启后 `running/queued` 恢复为 `paused`（可重新同步续跑，靠去重集合天然断点续传）。

## 5. 同步逻辑

### 5.1 流程

1. `auth(refresh_token)` 换取 access_token（`oauth.secure.pixiv.net/auth/token`）。
2. **刷新画师（生成清单）**：
   - `user_following` 翻页拉取全部关注画师；
   - 首次或上次未完成时，并行（`scan_workers` 路滑动窗口）逐画师 `user_illusts` 翻页拉取**全部作品**（不用 `illust_follow` 新作流——那只会返回近期作品，历史作品会漏）；
   - 上一轮完整扫描完成后，下一轮进入**增量模式**：逐画师从最新作品往回翻，遇到上一轮已入库的尾巴就停，只拉最新部分；
   - 画师级断点 `scan.done_uids`：本轮未扫完的画师下次继续；
   - 清单写入 SQLite `works.db`（含 done 标记与扫描断点），旧→新排序。
3. **刷新喜欢（生成清单）**：首次/未完成时全量翻页拉取当前用户公开收藏；上一轮完成后改为增量模式，从最新收藏往回翻，遇到上一轮已入库的收藏尾巴就停。断点保存完整翻页参数 `scan.next_qs`（Pixiv 该接口使用 `max_bookmark_id`）。
4. **同步**：从清单取「作品 id 不在去重集合且不在失败跳过集合」的作品，`workers` 并发下载。
   全部页下载成功 → 作品 id 入去重集合，清单标记 done；**失败页全部是 404/作品已删除 → 作品 id 入 failed_ids.json 永久跳过**（避免每次重试同一已删除页）；存在非 404 单页失败时下次继续重试（不会把作品整体标记为已下载）。
5. 任务状态每处理一个作品写入 `tasks.json`；去重集合在任务结束时落盘（也支持中途崩溃后按已下载文件跳过）。

### 5.1.1 限流保护（pixiv 429 Rate Limit）

pixiv app-api 有滑动窗口限流（约 30 req/10s，超出后 429；大量请求后冷却可能长达数小时）：
- 全局令牌桶限速器 `_RateLimiter(3.0)`：所有 app-api 请求（关注列表 / 画师列表 / 收藏翻页）统一限速 3 req/s，间隔带随机抖动（-20%~+40%）。
- 429 时自动抛 `RateLimitError` 停止当前任务（不再继续请求加剧限流），等待冷却后重试。
- 触发 429 后前端备注区会显示一个 **10 分钟冷却倒计时** 作为参考；建议倒计时归零后再刷新。
- 图片下载走 `i.pximg.net`（CDN），不受 app-api 限流影响，可保持 `workers` 并发。

### 5.2 下载

- 复用 `pixiv_mini` 流式下载：requests 流式 + 自动带 `Referer: https://app-api.pixiv.net/` 防盗链头 + 自动创建目标目录 + 已存在文件不覆盖。
- **清晰度**：**固定默认下载画师原图 `original`**（完整分辨率，`meta_single_page.original_image_url` / `meta_pages[].image_urls.original`，实测可达 4000+px）；取不到时回退 1200px `large`（master1200）。无前端开关；如需全局改 1200px，改 `download.py` 中 `all_image_urls` 的 `want_original = True`。
- 文件名：扩展名从 URL 真实提取（原图可能为 `.png`），单图 `{illust_id}{ext}`，多图 `{illust_id}_p{页码}{ext}`（pixiv 原生命名规则）。
- **存放位置**：**固定**将多图作品放入 `{作品id}/` 子文件夹（与 pixiv 命名一致，浏览直观），单图直接平铺；无前端开关（如需全部平铺，改 `download.py` 中 `process_illust` 的 `subfolder` 判定）。
- 存在非 404 单页失败时不会把作品写入去重集合（下次同步重试）；**失败页全部是 404 时把作品 id 进 `failed_ids.json` 永久跳过**，可点「重试失败作品」一键清除后重下。
- 已存在的 0 字节文件会在下载前删除并重新下载，不会误判为“已下载”。
- 文件已存在（`download()` 返回 False）也视为已下载并入去重集合（幂等）。
- **旧图导入**：每次同步开始前自动扫描 `<root>/pixiv/` 下已有图片，按命名规则（`{id}.jpg` / `{id}_p0.jpg` / `{id}p0.png` / 子文件夹 `{id}/`）提取作品 id 并入去重集合——**用户手动放入的旧图会被识别，全量更新直接跳过，不会重复下载/检查**；文件名不符合规则的图片无法自动识别（可手动改名或删文件重下）。
- 注意：已下载的旧图（历史 1200px 版）不会自动升级，需删除对应文件、并从 `downloaded_ids.json` 移出 id（可用伴侣工具 `pixiv_purge_non_original.py` 或 `pixiv_unmark_recent.py` 处理），之后同步会按固定默认行为重下原图。

### 5.3 依赖

仅 `requests`（宿主 venv 已内置 2.34.2），放在 `backend/libs/` 随插件分发（PluginManager 加载时自动加入 `sys.path`）。自身实现包 `backend/pixiv_sync/` 由 `manifest.libs: ["backend/libs", "backend"]` 声明，一并加入 `sys.path`。无重依赖，不需要 `runtime` / 独立 venv / stdio-worker。

## 6. 前端

`/plugins/pixiv-sync/frontend/index.html` 作为内嵌页面，通过 `get_extensions()` 在 image-viewer 左侧栏挂载「Pixiv 同步」入口；点击后由 image-viewer 用 iframe 加载。页面包含：

- 状态栏：Token 是否配置 / 下载根目录 / 已下载总数 / 关注·喜欢·其他清单统计 / 失败跳过数 / 上次任务结果
- 操作：🔄 同步画师、❤️ 同步喜欢、🔄 刷新关注名单、🔄 刷新喜欢名单、📋 刷新记录、🔍 校验内容、🗑 重试失败作品
- **进度条**：`done/total` 百分比（流式累加）+ 计数明细（下载/跳过/失败）+ 当前处理作品；每 1.5s 轮询 `get_status`
- 设置表单：refresh_token（密码框）/ 代理 / 下载目录 / 并发数 / 单次上限 / 刷新上限 / 限速 / 并行画师数
- **固定行为说明**：页面明确标注「下载原图」「多图子文件夹」为固定默认行为、无开关，避免误触。

布局复用 Shell 的 `.view-body` / `.view-toolbar` / `.view-content` 类与主题变量。

## 7. 伴侣工具（独立命令行脚本，随插件分发）

### 7.1 pixiv_purge_non_original.py —— 清理非原图（重下原图）

- 用于历史上「下载原图」被误关闭时期留下的大量 1200px 大图。
- 判断规则（按 Pixiv 缩放规则）：
  - 作品任一页长边 `>1200px` → 已是原图（master1200 最大只能到 1200），保留；
  - 作品最大边 `==1200px` → 这些 1200 页判定为历史缩放图，删除重下；小于 1200 的页保留；
  - 作品全部页 `<1200px` → 原图本来就小于 1200，重下结果相同 → 不处理。
- 操作：删除目标作品的 `==1200px` 文件，并从 `downloaded_ids.json` / `failed_ids.json` 移除 id，且把 `works.db` 中这些作品的 `done` 重置为 0。
- 支持 `--hours N` 时间窗口（只扫描最近 N 小时内的文件，窗口外只读一次 mtime 即跳过，扫描快）；`--dry-run` 预览；`--yes` 直接执行。
- 交互模式：不带参数运行，按提示输入根目录、时间范围并确认。

### 7.2 pixiv_unmark_recent.py —— 把已下载作品改回未下载（只改记录，不删文件）

- 适用：手动删除了本地图片后，想让插件按清单重新下载。
- 两种目标判定：
  - `--hours N` 时间窗口：按文件修改时间筛出最近作品（图片还在时用）；
  - `--missing`：对照 works.db 清单与本地文件，凡是 `done=1` 但本地文件已不存在的作品 → 改回未下载（图片已删时最准）。
- 操作（不动任何本地文件）：`downloaded_ids.json` / `failed_ids.json` 移出 id，`works.db` 的 `done` 改回 0。
- 执行后直接点「同步画师/同步喜欢」即可重下（无需先刷新名单）。

## 8. 非目标（第一版不做）

- 不做定时自动同步（仅手动按钮触发）。
- 不做小说 / 搜索 / 收藏写操作等 Pixiv 其他能力。
- 不做断点续传的「精确到页」恢复（去重为作品级，重复文件下载幂等跳过）。
- 不做 pixeval / 第三方客户端导入与旧目录迁移（v0.3 已移除，纯自写代码，libs 仅留第三方 pixiv_mini）。