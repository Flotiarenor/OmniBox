# Pixiv 同步插件（pixiv-sync）设计文档

> 版本：v0.5（在 v0.4 基础上：刷新关注改为「新画师全量优先 + illust_follow 聚合增量 + 兜底」状态机；
> 下载增加 Content-Type/魔数内容真实性校验）
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

| 任务     | API                                   | 范围                                                                                                           |
| -------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 同步画师 | `user_following` + `user_illusts` | 全部关注画师的**完整作品库**（逐画师翻页拉全量，含历史作品）                                             |
| 同步喜欢 | `v1/user/bookmarks/illust`          | 当前用户公开收藏，翻页拉取全部，**按作品画师归入对应画师目录**（只存收藏列表里的作品，不拉收藏画师全量） |

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
    │       ├── download.py      # 按清单并行下载 + 404 永久跳过 + 固定下载行为 + ugoira 分流
    │       ├── ugoira.py        # ugoira 动图：zip 帧序列 → 动画 WebP（帧时序解析 + 防御上限）
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
│       ├── 123456.webp                        # ugoira 动图转出的动画 WebP（平铺，v0.4）
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

| key               | 类型   | 说明                                                                                                                                                                                        |
| ----------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `refresh_token` | text   | Pixiv OAuth refresh token（密码登录已废弃）；`secret: true`，由插件的设置弹窗维护                                                                                                          |
| `proxy`         | text   | HTTP 代理，如`http://127.0.0.1:7890`；留空 = 直连                                                                                                                                         |
| `download_dir`  | text   | 下载根目录；留空 = 宿主相册根目录                                                                                                                                                           |
| `workers`       | number | 并发下载数（1-8，默认 4）：画师间/作品间并行下载；机械盘建议 1-2，SSD 可 4-8                                                                                                                |
| `max_download`  | number | 单次同步上限（默认 100，0=不限）：每次「同步画师/同步喜欢」最多下载条数，下完再点同步继续（分批推进）                                                                                       |
| `max_artists`   | number | 单次全量扫描画师数上限（默认 30，0=不限）：只约束「全量扫描」（首次入库/新关注画师拉全部历史）的画师数——这类每画师翻页多、是 429 主因；仅需增量尾巴更新的画师不受此限制、单次任务一次扫完 |
| `rate_limit`    | number | API 请求速率次/秒（默认 3，1-10 可调）：间隔带随机抖动（-20%~+40%）；pixiv 阈值约 3/s，调高有 429 风险                                                                                      |
| `scan_workers`  | number | 刷新名单的并行拉取画师数（滑动窗口，默认 4，1-8）：同时最多 N 个画师在拉，完成一个补充一个；总速率仍受`rate_limit` 限制                                                                   |

> **固定行为（v0.3，无开关，前端不再提供选项）**：
>
> - **下载原图（original，完整分辨率）默认开启**，取不到时回退 1200px 大图（master1200）。
> - **多图作品始终放入 `{作品id}/` 子文件夹**（与 pixiv 命名一致，浏览直观），单图直接平铺。
>   这两项是代码层固定默认，不暴露设置项，避免误触把历史作品降为 1200px 大图或打散目录。
>   如需调整，直接修改 `backend/pixiv_sync/download.py`：`all_image_urls` 的 `want_original`
>   与 `process_illust` 的 `subfolder` 判定。

设置持久化于 `.config/plugins/pixiv-sync.json`（SettingsStore，git 忽略）。

### 画师名单（selected_artists.txt：只同步指定画师）

- 配置文件位置：`<root>/.cache/pixiv-sync/selected_artists.txt`；前端设置区「画师名单文件」按钮（`Icons.html('icon:folder-open')`）可一键打开所在文件夹。
- 格式：每行一个画师，填**画师名字或 Pixiv 用户 id**；`#` 开头为注释、空行忽略。
- 语义：文件**存在且非空**时，同步画师只处理名单中匹配到的画师（按名字或 id 匹配；全部匹配不到则任务报错提示）；文件**不存在或为空** = 同步全部关注画师。
- 前端不做名单编辑 UI（避免复杂表单），直接编辑文本文件即可。

## 4. 后端 API 契约（pixiv-sync__*）

| API                                  | 参数     | 返回                                                                                                        | 说明                                                                                                                                                 |
| ------------------------------------ | -------- | ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `get_status`                       | -        | `{task, root_dir, token_configured, downloaded_total, running, selected_artists, selected_file, ...统计}` | 状态轮询                                                                                                                                             |
| `sync_following`                   | -        | `{ok, data\|error}`                                                                                        | 启动「同步画师」任务（**一步完成**：内部先自动刷新关注清单——新画师全量 + follow 流聚合增量 + 兜底——再按清单下载，404 永久跳过）            |
| `sync_bookmarks`                   | -        | `{ok, data\|error}`                                                                                        | 启动「同步喜欢」任务（**一步完成**：内部先自动刷新收藏清单，再按清单下载，与画师共用去重）                                                     |
| `refresh_following_lists`          | -        | `{ok, data\|error}`                                                                                        | **仅刷新关注清单（不下载）**：铺底/断点补扫 + 增量；「同步画师」已内置自动刷新，此入口用于只想更新清单看统计或手动控制首次大批量铺底时机       |
| `refresh_bookmarks_lists`          | -        | `{ok, data\|error}`                                                                                        | **仅刷新喜欢清单（不下载）**：首次/未完成全量、完成后增量翻页；「同步喜欢」已内置自动刷新                                                      |
| `refresh_downloaded`               | -        | `{ok, total, zero_removed, stale_removed, failed_cleared}`                                                | **刷新已下载记录**：扫描本地重建 ids（手动删的移除、手动加的导入、0 字节清理），重置消失作品的 done 快照，并清除本地已有文件对应的失败跳过记录 |
| `verify_downloaded`                | -        | `{ok, stale_removed, zero_removed, failed_cleared, total}`                                                | **校验已下载内容**：移除记录中本地无有效文件的失效 id 并重置清单 done 快照（下次同步重下），不导入新增                                         |
| `retry_failed`                     | -        | `{ok, cleared, kind}`                                                                                     | **一键重试失败作品**：清除 failed_ids.json（404 永久跳过记录）并立即按最近任务来源重新同步一次                                                 |
| `cancel_task`                      | -        | `{ok}`                                                                                                    | 请求取消当前任务（下个检查点生效）                                                                                                                   |
| `open_config`                      | -        | `{ok, file}`                                                                                              | 打开画师名单配置文件所在文件夹（不存在则创建带说明的空文件）                                                                                         |
| `start_oauth`                      | -        | `{ok, url}`                                                                                               | OAuth PKCE 第一步：生成 code_verifier 并打开 Pixiv 登录页                                                                                            |
| `finish_oauth`                     | `code` | `{ok, user_id}`                                                                                           | OAuth PKCE 第二步：用授权码换 token 并自动保存 refresh_token                                                                                         |
| `get_settings` / `save_settings` | -        | 设置读写（继承 PluginBase）                                                                                 | 前端设置表单使用                                                                                                                                     |

### 内置 OAuth 向导（refresh_token 失效时重新获取）

前端设置区「获取 Token」按钮（`Icons.html('icon:key-round')`），内置 Pixiv OAuth PKCE 授权码流程（RFC 7636，无需 gppt/selenium）。`start_oauth()` 生成 code_verifier（存插件内存）并返回完整登录 URL（含 code_challenge），流程：

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

**同步 = 一步任务**：`sync_following` / `sync_bookmarks` 内部依次执行
「刷新清单（生成/更新 `works.db`，断点续跑）→ 按清单下载」，用户点一次即可拿到新图；
独立 `refresh_*`（仅刷新不下载）复用同一刷新逻辑，供只想更新清单看统计、
或手动控制首次大批量铺底的时机。所有任务共用一个串行线程（同一时间只有一个任务）。

**同步画师（sync_following）内部**：

1. `auth(refresh_token)` 换取 access_token（`oauth.secure.pixiv.net/auth/token`），
   自动回写 Pixiv 轮换的 refresh_token；并扫描本地已有图片并入去重集合（旧图导入）。
2. **刷新关注清单（v0.5 状态机）**：
   - `user_following` 翻页拉取全部关注画师（每轮必做，用于识别新关注的画师）；
   - **铺底/断点态**（`scan.complete=False` 或存在未入库画师）：只补扫 `scan.done_uids` 之外
     的画师，并分为两类——DB 已有一轮记录的画师 → `user_illusts` 从最新往回翻、遇到已入库
     作品就停（**增量尾巴：每画师平均 1 次请求，不受上限、单次任务一次扫完**）；全新画师 →
     翻到底全量（每画师翻页多，受 `max_artists` 分批防 429）。未完成的画师会移出
     done_uids，下轮继续（不会重复扫已完成画师，断点实时落盘）；
   - **增量态**（`complete=True` 且所有画师已有记录）：老画师新作改用 **`illust_follow`
     聚合新作流**一次覆盖——从最新往回翻，遇到「整页（约 30 条）作品全部已入库」即停。
     依据：对任意画师，「未入库新作」必然比它已入库的作品更新，因此一旦翻到连续整页
     已入库的时间带，其后（更旧）不可能再有未入库新作。常规刷新 1~3 次请求即完成，
     **不再逐画师请求**；只有长时间未同步导致积压超出 follow 时间流窗口
     （`MAX_FOLLOW_PAGES=40` 页仍未见到整页已入库）时才转入逐画师兜底补齐，保证不漏；
   - 画师级断点 `scan.done_uids` / `scan.complete`：中断/429 后下次续跑；
   - 清单写入 SQLite `works.db`（含 done 标记与扫描断点），旧→新排序。
3. **按清单下载**：从清单取「作品 id 不在去重集合且不在失败跳过集合」的作品，
   `workers` 并发下载。全部页下载成功 → 作品 id 入去重集合，清单标记 done；
   **失败页全部是 404/作品已删除 → 作品 id 入 failed_ids.json 永久跳过**（避免每次
   重试同一已删除页）；存在非 404 单页失败时下次继续重试（不会把作品整体标记为已下载）。
4. 任务状态每处理一个作品写入 `tasks.json`；去重集合在任务结束时落盘（也支持中途崩溃后按已下载文件跳过）。

**同步喜欢（sync_bookmarks）内部**：与上面相同，只是第 2 步换成刷新收藏清单——
首次/未完成时全量翻页拉取当前用户公开收藏；上一轮完成后改为增量模式，从最新收藏往回翻，
遇到上一轮已入库的收藏尾巴就停。断点保存完整翻页参数 `scan.next_qs`
（Pixiv 该接口使用 `max_bookmark_id`）。

### 5.1.1 限流保护（pixiv 429 Rate Limit）

pixiv app-api 有滑动窗口限流（约 30 req/10s，超出后 429）：

- 全局令牌桶限速器 `_RateLimiter(3.0)`：所有 app-api 请求（关注列表 / 画师列表 / 聚合新作流 / 收藏翻页 / ugoira 元数据）统一限速 3 req/s，间隔带随机抖动（-20%~+40%）。
- 429 时自动抛 `RateLimitError` 停止当前任务（不再继续请求加剧限流），等待冷却后重试。
- 触发 429 后前端备注区会显示一个 **10 分钟冷却倒计时** 作为参考；建议倒计时归零后再刷新。
- 图片下载走 `i.pximg.net`（CDN），不受 app-api 限流影响，可保持 `workers` 并发。
- **v0.5 后增量刷新请求量大幅下降**：常规「刷新关注」≈ 关注列表几页 + follow 新作流 1~3 页
  （原本是每个画师至少 1 次，关注 300 人 ≈ 300+ 次），429 触发概率与刷新耗时同时显著降低。

### 5.2 下载

- 复用 `pixiv_mini` 流式下载：requests 流式 + 自动带 `Referer: https://app-api.pixiv.net/` 防盗链头 + 自动创建目标目录 + 已存在文件不覆盖。
- **清晰度**：**固定默认下载画师原图 `original`**（完整分辨率，`meta_single_page.original_image_url` / `meta_pages[].image_urls.original`，实测可达 4000+px）；取不到时回退 1200px `large`（master1200）。无前端开关；如需全局改 1200px，改 `download.py` 中 `all_image_urls` 的 `want_original = True`。
- 文件名：扩展名从 URL 真实提取（原图可能为 `.png`），单图 `{illust_id}{ext}`，多图 `{illust_id}_p{页码}{ext}`（pixiv 原生命名规则）。
- **存放位置**：**固定**将多图作品放入 `{作品id}/` 子文件夹（与 pixiv 命名一致，浏览直观），单图直接平铺；无前端开关（如需全部平铺，改 `download.py` 中 `process_illust` 的 `subfolder` 判定）。
- 存在非 404 单页失败时不会把作品写入去重集合（下次同步重试）；**失败页全部是 404 时把作品 id 进 `failed_ids.json` 永久跳过**，可点「重试失败作品」一键清除后重下。
- 已存在的 0 字节文件会在下载前删除并重新下载，不会误判为“已下载”。
- 文件已存在（`download()` 返回 False）也视为已下载并入去重集合（幂等）。
- **内容真实性校验（v0.5）**：`pixiv_mini.download()` 默认 `verify_image=True`——流下载完成后校验
  响应 `Content-Type` 以 `image/` 开头，且文件头魔数为已知图片格式（JPEG/PNG/GIF/WebP），
  任一不通过则删除残片并抛 `PixivError`。代理拦截页/异常 200（HTML/文本）不会再被当成
  图片保存进入去重集合导致永久漏下；ugoira 帧 zip 等非图片下载显式传 `verify_image=False`。
- **ugoira 动图（v0.4）**：`type == "ugoira"` 的作品不再下载 zip 存成坏 jpg，改由 `process_ugoira` 独立处理（见 `ugoira.py`）：
  - 先调 `ugoira_metadata`（`v1/ugoira/metadata`，app-api，走全局限速）取**每帧时长** `frames[].delay`（帧时序不在 zip 里）；
  - 再流式下载帧 zip（i.pximg.net CDN，`verify_image=False`），按帧清单解码并 `Pillow` 拼帧存为**动画 WebP** `{作品id}.webp`（`save_all`，逐帧 duration，无限循环；缩略图由 image-viewer 取首帧静态）；
  - 目标 `.webp` 已存在（非 0 字节）即幂等跳过（不发任何网络请求）；`.webp` 也会被旧图导入规则（命名含 webp）识别进去重集合；
  - 失败语义与普通作品一致：**404 → `failed_ids.json` 永久跳过**；帧时序/zip/转换失败（非 404）→ 只计数失败、不入去重集合，**下次同步重试**；元数据请求触发 429 → 抛 `RateLimitError` 停止整个任务；
  - 转换有防御上限（帧数 ≤1000、单帧 ≤64MB、累计像素 ≤4 亿），超限按失败重试处理，不会因异常 zip 打爆内存。
  - 注意：v0.3 及更早版本曾把 ugoira 的 `.zip` 下载成坏 `.jpg` 并记入去重，存量坏文件不会自动修复；
    可删除对应文件后点「校验内容」/用 `pixiv_unmark_recent.py --missing` 重置记录，之后同步会重下为动图 WebP。
- **旧图导入**：每次同步开始前自动扫描 `<root>/pixiv/` 下已有图片，按命名规则（`{id}.jpg` / `{id}_p0.jpg` / `{id}p0.png` / `{id}.webp` / 子文件夹 `{id}/`）提取作品 id 并入去重集合——**用户手动放入的旧图会被识别，全量更新直接跳过，不会重复下载/检查**；文件名不符合规则的图片无法自动识别（可手动改名或删文件重下）。
- 注意：已下载的旧图（历史 1200px 版）不会自动升级，需删除对应文件、并从 `downloaded_ids.json` 移出 id（可用伴侣工具 `pixiv_purge_non_original.py` 或 `pixiv_unmark_recent.py` 处理），之后同步会按固定默认行为重下原图。

### 5.3 依赖

仅 `requests`（宿主 venv 已内置 2.34.2），放在 `backend/libs/` 随插件分发（PluginManager 加载时自动加入 `sys.path`）。自身实现包 `backend/pixiv_sync/` 由 `manifest.libs: ["backend/libs", "backend"]` 声明，一并加入 `sys.path`。无重依赖，不需要 `runtime` / 独立 venv / stdio-worker。

> v0.4 的 ugoira 动图转换需要 **Pillow**（运行时懒加载于 `ugoira.py`，仅动图作品用到）。宿主 image-viewer 依赖 Pillow（requirements 内置 12.x），Companion 场景必然可用；静态图/1200px 下载不依赖 Pillow，缺失时只有动图转换报错。

## 6. 前端

`/plugins/pixiv-sync/frontend/index.html` 作为内嵌页面，通过 `get_extensions()` 在 image-viewer 左侧栏挂载「Pixiv 同步」入口；点击后由 image-viewer 用 iframe 加载。页面包含：

- 状态栏：Token 是否配置 / 下载根目录 / 已下载总数 / 关注·喜欢·其他清单统计 / 失败跳过数 / 上次任务结果
- 操作：同步画师（`Icons.html('icon:download')`）、同步喜欢（`Icons.html('icon:heart')`），两者都**一步完成：自动先刷新清单再下载**；刷新关注名单 / 刷新喜欢名单（`Icons.html('icon:refresh-cw')`，仅更新清单不下载，备用）；刷新记录（`Icons.html('icon:list-checks')`）、校验内容（`Icons.html('icon:search')`）、重试失败作品（`Icons.html('icon:trash-2')`）
- **进度条**：`done/total` 百分比（流式累加）+ 计数明细（下载/跳过/失败）+ 当前处理作品；每 1.5s 轮询 `get_status`
- 设置表单：refresh_token（密码框）/ 代理 / 下载目录 / 并发数 / 单次上限 / 刷新上限 / 限速 / 并行画师数
- **固定行为说明**：页面明确标注「下载原图」「多图子文件夹」「动图转动画 WebP」为固定默认行为、无开关，避免误触。

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
## 8. UI 现状取证（前端与壳契约对照）

> 只读审计。契约基准：`shell/frontend/public/shell/{variables.css, base.css, effects.css, base.js}`
> 与 `docs/plugin-guide.md` §4.1 / §4.3 / §4.4。所有结论附 `文件路径:行号`。

### 8.1 概览

**前端文件清单**

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `plugins/pixiv-sync/frontend/index.html` | 461（24716 B） | 唯一文件：`<style>` 1 块（`index.html:7-60`，54 行）+ 内联 `<script>` 1 块（`index.html:198-459`，262 行）+ 静态骨架 |
| `plugins/pixiv-sync/frontend/*.css` | — | **不存在**：全部样式写在 `index.html:7-60` 的单个 `<style>` 里 |
| `plugins/pixiv-sync/frontend/**/*.js` | — | **不存在**：全部逻辑内联在 `index.html:198-459`，1 个 IIFE，无模块拆分 |

- **页面形态**：单页单视图。页面只有一个「状态栏 + 按钮组 + 进度条 + 设置面板」的控制面板，没有视图切换、没有作品列表、没有分页（`index.html:63-172` 是全部结构）。
- **嵌入形态（二级 iframe）**：插件通过 `get_extensions()` 的 `embedUrl` 挂到 image-viewer 侧栏（`plugins/pixiv-sync/backend/main.py:736-750`，`embedUrl` 在 `:746`，`host: image-viewer` 在 `:740`）；宿主把 `ext.embedUrl` 塞进自己的 `#extension-frame`（`plugins/image-viewer/frontend/js/app.js:157-165`），该 iframe 满尺寸（`plugins/image-viewer/frontend/image-viewer.css:395-404`，`.extension-view-body{flex:1;min-height:0}` + iframe `width/height:100%`），关闭时 `frame.src='about:blank'` 销毁文档（`image-viewer/frontend/js/app.js:168-173`）。
- **manifest 事实**：`plugins/pixiv-sync/manifest.json:27` `"hidden": true`、`:23-26` `frontend.entry/route`、**未声明 `keepAlive`**（无该键）。
- **Shell 资源注入**：壳把 `variables.css + base.css + folder-picker.css + effects.css + base.js + folder-picker.js + motion.js` 注入到 `</head>` 之前（`shell/backend/file_server.py:59-66`、`:771-798`）。因此本插件的 `<style>`（7-60）**排在壳样式之前**，同优先级规则壳胜；插件要覆盖只能靠更高的选择器特异性（它确实这么做了，见 §8.2）。
- **是否使用 Shell 布局类**：用了 `.view-body`（63）、`.view-toolbar`（66、73）、`.toolbar-group`（67、70）、`.view-content`（75）、`.obx-scroll`（75）、`.btn/.btn-sm/.btn-danger`（71、92-102、160-162、184、192-193）；未用 `.view-sub-sidebar` / `.sub-sidebar-header` / `.sub-sidebar-footer` / `.obx-nav-item` / `.modal` / `.toast` / `.empty-state` / `.pagination-bar` / `.settings-form`。

### 8.2 布局骨架

顶层结构树（`index.html:62-196`）：

- `div#app.view-body.psync-main`（`index.html:63`；`.psync-main` 见 `index.html:10`）
  - `div.view-toolbar`（`index.html:66`，来自 `base.css:239-248`）
    - `div.toolbar-group`（`:67`）→ `span.psync-title[style=font-weight:600]`（`:68`，内联）
    - `div.toolbar-group[style=margin-left:auto]`（`:70`，内联）→ `button#btn-cancel.btn.btn-sm[style=display:none]`（`:71`，内联）
  - `div.view-content.obx-scroll`（`index.html:75`，来自 `base.css:277-282`）
    - `div.psync-status` ×2（`:77`、`:83`）→ 状态点 `.dot` ×1 + 文本 span ×N（`:78-87`）
    - `div.psync-btns` ×3（`:91`、`:95`、`:99`）→ 同步/刷新/校验按钮共 7 个（`:92-102`）
    - `div.psync-progress#progress-box[style=display:none]`（`:106`）→ `div.psync-bar > div#bar`（`:107`）+ `div.meta`（`:108-111`）
    - `div.psync-current#p-current`（`:113`）
    - `div.psync-settings`（`:116`）→ `h4` + 8 个 `label>input`（`:118-155`）+ `.psync-options`（`:128-156`）+ `.save-row`（`:157-164`）
    - `div.psync-tip` ×2（`:166`、`:170`）
- `div#oauth-modal.psync-modal > div.box`（`index.html:175-196`）与 `#app` **平级**，直接挂在 `body` 下

**尺寸与滚动来源**

| 部位 | 数值 | 来源 |
| --- | --- | --- |
| 高度链（二级 iframe 必需） | `html, body { height: 100%; margin: 0 }` | 插件自定义（`index.html:9`），注释在 `:8` 写明「iframe 内 html/body 必须有高度，滚动才生效」 |
| 主容器 | `display:flex; flex-direction:column; height:100%; min-height:0` | 插件自定义 `.psync-main`（`index.html:10`）**补足** `base.css:237` 的 `.view-body`（壳版没有 `height`） |
| 工具栏高度 | `var(--toolbar-height, 48px)` | `base.css:240`，插件未覆盖 |
| 内容区滚动 | `flex:1 1 auto; min-height:0; overflow-y:auto` | 插件自定义 `.psync-main .view-content`（`index.html:11`）**重写** `base.css:277-282`（壳版是 `flex:1`、无 `min-height`）；`padding:16px` 沿用壳 |
| 内容区滚动条 | `.obx-scroll` | `effects.css:140-183`（悬停渐显 6px 窄条）——本插件唯一使用的壳动效/工具类 |
| 导航侧栏宽度 | 无侧栏 | 未使用 `--sub-sidebar-width` / `--nav-width` |
| 状态栏 | `padding:8px 16px; font-size:12px; gap:12px; flex-wrap:wrap` | 插件自定义 `.psync-status`（`index.html:12-13`） |
| 状态点 | `width/height:8px; border-radius:50%` | 插件自定义 `.psync-status .dot`（`index.html:14`） |
| 按钮区 | `gap:10px; padding:10px 16px`，`.btn{flex:1}` | 插件自定义 `.psync-btns`（`index.html:18-19`）——把壳的 `.btn` 拉成等宽（`base.css:14-24` 无 `flex`） |
| 进度条 | 槽 `height:10px; border-radius:5px`；填充 `linear-gradient(90deg,#4caf50,#8bc34a)`；`transition:width .3s ease` | 插件自定义 `.psync-bar`（`index.html:21-24`） |
| 设置卡 | `margin:8px 16px; padding:12px; border:1px solid …; border-radius:8px; gap:10px` | 插件自定义 `.psync-settings`（`index.html:29-30`） |
| 选项组 | `border:1px dashed …; border-radius:6px; padding:10px; gap:8px` | 插件自定义 `.psync-options`（`index.html:38-39`） |
| 弹窗 | `width:min(560px,92vw); max-height:86vh; border-radius:10px; padding:16px 18px` | 插件自定义 `.psync-modal .box`（`index.html:47-48`） |
| 弹窗 textarea | `min-height:120px; font-size:11px; font-family:Consolas,monospace` | 插件自定义（`index.html:53`）；`min-height:120px` 与壳 `.field textarea`（`base.css:201`）数值相同但各写一份 |

### 8.3 设计 token 使用

- **实际用到的 `--*` 变量（全部清单）**：只有 4 个名字，共 27 处（执行正则 `var\((--[a-z0-9-]+)` 后按名字计数）：
  `--color-text` ×17、`--color-border` ×6、`--color-bg-input` ×3、`--color-bg` ×1。
- **这 4 个名字在 `variables.css` 里一个都不存在**（`variables.css:4-52` 浅色、`:55-86` 深色，只有 `--bg-* / --text-* / --border / --accent* / --danger* / --success / --warning / --nav-width / --sub-sidebar-width / --toolbar-height / --radius* / --shadow-* / --transition-*`）。同一正则的结果里**没有任何一个真实 token**：本插件对 Shell 主题 token 的使用次数 = **0**。
- 后果：每个 `var()` 都取 fallback，浅/深两套 token 对它完全无效。深色主题下正文字仍是 `#666`/`#999`（`index.html:13/26/27/32/41/42`），压在 `--bg-app` 深色底（`variables.css:56` `#0d1117`）上。
- **覆盖 `:root` / 自有 `--xxx`**：0 处（grep `:root` 与 `^\s*--[a-z-]+\s*:` 均无命中）。它的「默认值」全部写在 `var()` 的第二个参数里，等于给每个位置内联了一份字面色。
- **`data-theme`**：插件 0 处处理（grep `data-theme` = 0）。主题同步完全由壳注入的脚本完成——读父窗口 `documentElement` 的 `data-theme` 写到本 iframe `<html>`，并用 MutationObserver 跟随（`shell/backend/file_server.py:69-76`）。属性同步是好的，但 token 名错，同步了没有效果。
- **硬编码颜色字面量**（在 `index.html` 上执行）：
  - 正则 `#[0-9a-fA-F]{3,8}\b` → **34 处 / 13 个不同值**：`#999`×9、`#333`×4、`#ddd`×4、`#666`×3、`#fff`×3、`#4caf50`×2、`#d33`×2、`#e0e0e0`×2、`#888`、`#8bc34a`、`#9e9e9e`、`#f44336`、`#fafafa`。
  - 正则 `rgba?\(` → **2 处**：`rgba(0,0,0,.45)`（`:45` 弹窗遮罩）、`rgba(0,0,0,.25)`（`:49` 弹窗阴影）。
  - 内联 `style="` → **18 处**（行号 68,70,71,83,95,99,106,130,138,142,146,150,154,157,158,159,170,178），其中 12 处是纯排版（`padding-top:0` / `font-size:11px` / `display:flex;gap:8px`），5 处重复写 `color:var(--color-text,#999)`。
  - 最典型的 5 例：`:13` `color: var(--color-text, #666)`（状态栏）；`:15-17` `.dot.ok{#4caf50}` / `.bad{#f44336}` / `.idle{#9e9e9e}`；`:24` 进度填充 `linear-gradient(90deg,#4caf50,#8bc34a)`（固定绿，与 `--accent`/`--success` 无关）；`:45` 遮罩 `rgba(0,0,0,.45)`（浅色 `--bg-overlay` 的同值，深色下仍是 0.45 黑）；`:170` 内联 `color:#d33`（限流冷却提示，未用 `--danger`）。

### 8.4 组件与命名约定

- **自有类名前缀**：`psync-`（12 个类，定义行见下），另有 3 个无前缀短名 `.dot`（`:14`）、`.ok/.bad/.idle`（`:15-17`）、`.box/.row`（`:47`、`:59`，弹窗内部，与壳 `.modal-box` 不同名但语义重叠）。
- **清单（类名 → 定义行 → 用途）**：
  - `.psync-main` `:10` 主容器；`.psync-status` `:12` 状态栏；`.psync-btns` `:18` 按钮行；`.psync-progress` `:20` 进度区；`.psync-bar` `:21` 进度槽/填充；`.psync-current` `:27` 当前处理项；`.psync-settings` `:29` 设置卡；`.psync-options` `:38` 选项组；`.psync-tip` `:41` 提示文案；`.psync-empty` `:42` 空态（**死类**，见下）；`.psync-modal` `:45` 弹窗。
  - 按钮：不自建，直接用壳的 `.btn` / `.btn-sm` / `.btn-danger`（`:71`、`:92-102`、`:160-162`、`:184`、`:192-193`）——**这点合规**。
- **Shell 已提供但插件又自己实现了一遍**：

| # | 能力 | Shell 提供 | 插件实现 | 差异 |
| --- | --- | --- | --- | --- |
| 1 | 设置表单 | `.settings-form/.field/.field-label/.field-help`（`base.css:181-206`）+ `createSettingsForm`（`base.js:419-563`）+ `openSettingsModal`（`base.js:566-628`） | `index.html:116-165` 静态 HTML + `loadSettings/saveSettings`（`index.html:324-369`） | 壳版按插件声明的 `settings_schema` 渲染（本插件已声明 8 项，见 `backend/main.py:38-114`）；插件版手写 label>input、无 `.field-help`、无 `required` 标记、数字范围只靠 `min/max` 属性 + `intSetting()` 夹取（`:339-345`）；壳版保存成功后会强制重载页面（`base.js:622`），插件版只 `alert('设置已保存')`（`:363`） |
| 2 | 弹窗 | `.modal/.modal-box/.modal-body/.modal-footer`（`base.css:159-179`），`z-index:1500`、`width:400px`、`radius:var(--radius)`=6px，遮罩 `pointerdown` 关闭（`base.js:602`） | `.psync-modal/.box`（`index.html:45-59`） | `z-index:999`（低于壳弹窗 1500、Toast 3000，`base.css:162/210`）、`width:min(560px,92vw)`、`radius:10px`、`max-height:86vh`、无遮罩点击关闭、无 Esc 关闭；显隐靠内联 `style.display`（`:388`、`:406`、`:414`） |
| 3 | 空状态 | `.empty-state`（`base.css:428-431`：flex 居中、`min-height:300px`、16px） | `.psync-empty`（`index.html:42-43`：12px、`padding:20px 0`） | 插件类是**死代码**——grep `psync-empty` 只命中定义行 `:42`，JS 从未使用；页面「无数据」实际显示为 `已下载 0 张` / `画师名单: 全部` 之类的文本 |
| 4 | Toast 通知 | `.toast*`（`base.css:208-234`）+ `Toast`（`base.js:362-391`，4 种类型、2.6s 自动消失） | 0 处使用；改用原生 `alert()` **22 处**（`:289,295,315,319,361,363,367,379,384,390,402,405,410,422,423,428,433,434,439,444,445,448`） | 原生弹窗阻塞、无类型区分、无自动消失、样式由 OS 决定、切走/最小化时提示丢失 |
| 5 | 确认对话框 | `confirmDialog(message, options)`（`base.js:394-417`，可 `danger` 样式） | 0 处使用 | 破坏性操作 `#btn-retry-failed`（`:102` `icon:trash-2` + 「重试失败作品」）点击即执行（`:441-449`），无二次确认 |
| 6 | 进度条 | 无对应组件（`base.css`/`effects.css` 都没有） | `.psync-bar`（`:20-24`） | 自建属合理；但颜色/圆角未 token 化，也没有用 `.obx-skeleton`（`effects.css:118-131`）做加载骨架 |
| 7 | 徽标/状态点 | 无 badge 组件 | `.dot`（`:14-17`） | 自建属合理；三色硬编码 |
| 8 | 未使用的壳能力 | `createCardGrid`（`base.js:915`）、`createPagination`（`:845`）、`createContextMenu`（`:886`）、`createTree`（`:631`）、`createLightbox`（`:708`）、`Utils.escapeHtml/debounce/formatFileSize`（`base.js:321-359`）、`Motion`（`motion.js`） | — | 全部 0 次引用；`effects.css` 里只有 `.obx-scroll` 被用到，`.obx-anim-*`/`.obx-glass`/`.obx-card-lift`/`.obx-stagger` 全部未使用 |

### 8.5 交互约定

- **设置入口**：页面内嵌面板（`index.html:116-165`），不是弹窗、不是路由、不是壳的 `openSettingsModal`。保存按钮 `#btn-save`（`:162`）→ `saveSettings()`（`:347-369`）→ `Bridge.call('save_settings', values)`（`:359`）；失败 `alert(r.error || '保存失败')`（`:361`），成功 `alert('设置已保存')` + `refreshStatus()`（`:363-364`）。数字项统一走 `intSetting()` 夹取（`:339-345`）。
- **其它设置入口**：`icon:folder-open` + 「画师名单文件」（`:161`）→ `Bridge.call('open_config')`（`:378-380`，后端 `backend/main.py:542` 打开系统文件管理器）；`icon:key-round` + 「获取 Token」（`:160`）→ `start_oauth`（`:383`）后自建 OAuth 引导弹窗（`:381-415`），完成走 `finish_oauth`（`:403`）。
- **保存后的反馈**：仅 `alert`（`:363`、`:405`），不重载、不 Toast。
- **错误提示方式**：22 处 `alert`；桥不可用时把 `#st-last` 文本改成「Bridge 不可用」（`:245`），无错误样式类；任务错误 `$('st-last').textContent = '错误: ' + task.error`（`:273`）；限流冷却用 `#psync-cooldown`（`:170`，内联 `color:#d33`）+ `Icons.html('icon:loader')` + 「Pixiv 限流冷却参考：mm:ss」（`:213`）。
- **选择模型**：页面内没有任何多选/复选框（grep `type="checkbox"` = 0）；「要同步哪些画师」由外部文本文件决定（后端 `backend/main.py:495-541`），前端只显示 `画师名单: N 位/全部`（`:158`、`:240`）。
- **右键菜单**：无（grep `contextmenu` = 0）。
- **键盘快捷键**：**无**（grep `keydown` = 0）。OAuth 弹窗既不能 Esc 关闭，也不能回车提交 code（`:400-412` 只监听按钮 click）。
- **长任务进度与取消**：`startSync`/`startRefresh`（`:285-321`）→ 轮询 `tick()` 每 1500ms（`:304`）；另有常态 `setInterval(refreshStatus, 10000)`（`:453`）与 `setInterval(renderCooldown, 1000)`（`:454`）。取消按钮仅在 `running/queued` 时显示（`:280`），点击 `Bridge.call('cancel_task')`（`:376`）。进度宽度 `$('bar').style.width = pct + '%'`（`:265`），状态文案 6 态映射：`queued/running/done/failed/cancelled/paused`（`:258-261`）。
- **空态/加载态/错误态**：无加载态（首屏直接显示 0 值与「…」，无骨架）；空态类 `.psync-empty` 未被使用；错误态只有文本，无 `--danger` 配色（除内联 `#d33`）。
- **生命周期**：0 处 `onShow`/`onHide`/`onDispose`（grep 三者均 0），但有两个常驻定时器（`:453`、`:454`）——见 §8.7。
- **桥调用点**：`Bridge.call` 共 12 处（`:223,287,314,326,359,376,379,383,403,421,432,443`）。

### 8.6 特色设计（值得其他插件吸收）

1. **状态栏双行 + 语义分组**：第一行凭据/根目录/已下载总量/上次结束时间（`index.html:77-82`），第二行「关注 / 喜欢 / 其他 / 失败跳过」四组「共 · 待下 · 已下」（`:83-88`），每个 span 都带 `title` 解释口径（`:84-87`）。解决「同步类插件只给一条进度条，用户不知道总量、待办量和失败量」。
2. **429 冷却做成显式倒计时**：后端返回剩余冷却秒数（`backend/main.py:715`），前端 `cooldownUntil` + 1s 定时器渲染 `mm:ss`（`index.html:204-218`、`:225`），并在文案里直接给建议（`:213`）。解决限流后用户继续点按钮、把冷却越拖越长。
3. **「其他」桶让统计自洽**：`other_done = max(0, len(ids) - following_done - bookmarks_done)`（`backend/main.py:711`），前端单独一行显示（`:86`）。解决旧图导入/手动放置导致的「下载总量比名单加起来还多」。
4. **破坏性修复操作独立成组、后果写进 title**：`刷新记录 / 校验内容 / 重试失败作品`（`index.html:99-103`），每组 `title` 说明做什么（`:100-102`），执行后用一次 `alert` 汇总增删条数（`:423-426`、`:434-437`、`:445`）。解决「同步记录与磁盘不一致时用户无从下手」。
5. **OAuth 向导内嵌在插件里**：`start_oauth` 返回可直接导航的完整 URL（`:385` 注释说明为何不能在控制台 fetch——会被 CORS 拦），textarea 展示 + 复制按钮先 `navigator.clipboard` 后 `execCommand('copy')` 兜底（`:393-399`），并前置「不要再点一次获取 Token，否则验证码失效」的警告（`:178`）。解决 token 过期后必须跳出应用、装 gppt 的断链。
6. **常态轮询与任务轮询分离**：常态 10s 一次（`:453`），需要进度时切到 1.5s（`:303-308`），任务结束自动降频（`:306-307`）。解决长任务期间把桥调用打满。

### 8.7 与 Shell 契约的偏差

1. **主题 token 名不存在（27 处）**：`--color-text/--color-border/--color-bg-input/--color-bg` 全部不在 `variables.css` 中（`variables.css:4-86`），只有 fallback 生效。影响面：13 条 `psync-*` 规则 + 18 处内联样式中的 5 处 → 状态栏文字、设置卡边框与输入框、进度槽、OAuth 弹窗、提示文案，即**整页配色**；深色主题下文字对比度不足。
2. **颜色字面量 34 hex + 2 rgba + 18 内联 style**：`#4caf50/#8bc34a`（进度）、`#f44336/#4caf50/#9e9e9e`（状态点）、`#d33`（冷却）在深浅两套主题下都不变；影响换肤能力与 `--danger/--success/--accent` 的统一。
3. **设置双轨**：`backend/main.py:38-114` 声明 8 项 `settings_schema`（其中 `refresh_token` 是 `"secret": True`，`:48`），`index.html:116-165` 又复刻同样 8 项 → schema 任何变更都要改两处。附带影响：`get_settings` 对 secret 键返回掩码 `********`（`shell/backend/plugin_base.py:377`、`:70-87`），插件把它回填进密码框（`index.html:327`），用户在框里看到一串星号、无法判断「已配置 / 未配置」（掩码回传不会覆盖真值，由 `plugin_base.py:395-397` 兜住，因此只是 UX 问题）；另外这里的保存不触发壳的整页重载（壳版见 `base.js:622`）。
4. **22 处原生 `alert` 取代 Toast / confirmDialog**：涉及 12 个桥调用点的全部用户反馈路径（`Bridge.call` 12 处），以及 7 个按钮（`:92-102`、`:160-162`）；破坏性操作无确认。
5. **弹窗未走 `.modal`**：`z-index:999` 低于壳弹窗 1500 与 Toast 3000（`base.css:162`、`:210`）；OAuth 弹窗与壳弹窗同时出现时会被压在下面；缺遮罩点击关闭（壳语义见 `base.js:601-602`）与 Esc 关闭。
6. **生命周期契约未接（有实际代价）**：插件 0 处 `onShow/onHide/onDispose`，却注册了 2 个常驻定时器（`index.html:453`、`:454`）。宿主 image-viewer 声明了 `keepAlive`，切走时只 `v-show` 隐藏 iframe（`shell/frontend/src/App.vue:293-307`），插件 iframe 不会被卸载（只有点「返回相册」才 `src='about:blank'` 销毁，`image-viewer/frontend/js/app.js:168-173`）→ 用户切到别的插件后，pixiv 的 10s `refreshStatus` 与 1s `renderCooldown` 仍在持续打后端。对应 `docs/plugin-guide.md:740-744` 的 `onShow`/`onHide` 用途表。
7. **空态/加载态未落地**：`.psync-empty` 仅定义（`index.html:42`）、0 处使用；全页没有 `.empty-state`（`base.css:428`）、`.loading`（`base.css:427`）或 `.obx-skeleton`（`effects.css:118`）；首屏是「0 值 + …」而不是加载指示。
8. **二级 iframe 的高度链要插件自己补**：壳的 `.view-body` 没有 `height`（`base.css:237`），嵌入型插件必须自写 `html,body{height:100%}` + `min-height:0`（`index.html:9-11`）才能滚动；同类嵌入面板（image-viewer 扩展面板，`image-viewer.css:395-404`）都在各自重复这条，没有共享的容器契约。
