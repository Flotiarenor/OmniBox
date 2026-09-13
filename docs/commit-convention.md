# 提交信息规范

面向所有往本仓库提交的人与工具（含 DSH 等 agent）。摘要见仓库根的
[AGENTS.md](../AGENTS.md)，本文件是细则与示例。

---

## 1. 为什么要有规范

- **`git log` 是长期资产**：半年后排查"这个行为当初为什么改"，第一眼看的是提交信息，
  不是 diff。
- **发布说明从这里取**：本项目按 tag 发版，Release notes 与 changelog 直接从提交
  信息生成；含糊的 subject 会原样出现在发布页上。
- **信息要能自证**：正文里的断言（"修好了""通过了"）必须能被复核，否则读者只能
  重跑一遍。
- **面向工具**：仓库根的 `AGENTS.md` 会被 agent 工具注入上下文，规范写在那里面，
  后续的自动提交才会跟着走。

---

## 2. 格式

```
<type>(<scope>): <subject>

<正文：背景 / 改动 / 验证 / 影响，可省>

<footer：BREAKING CHANGE / Refs，可省>
```

---

## 3. `type` 取值

沿用仓库既有写法（近 120 条非 merge 提交的分布列在最后）：

| type | 用途 | 例 |
| --- | --- | --- |
| `feat` | 新增用户可见能力 | `feat(media-player): 视频封面支持系统 ffmpeg 后端抽取` |
| `fix` | 修缺陷 | `fix(file-server): /thumbs 越界返回 403 而不是 400` |
| `refactor` | 不改行为的重构 | `refactor: 抽取媒体插件共享基建 BackgroundTask 与 ThumbCache` |
| `docs` | 文档 | `docs(plugin-guide): 补共享基建章节` |
| `test` | 测试 | `test(file-server): 覆盖越界与正常读取两种路径` |
| `ci` | 工作流与门禁 | `ci(release): tag 触发构建并创建草稿 Release` |
| `build` | 构建脚本、依赖打包 | `build: 构建脚本自动同步 requirements.txt` |
| `chore` | 杂项（版本号、忽略规则、依赖清单） | `chore(deps): requirements 分层为运行时与 dev` |
| `perf` | 性能 | `perf(media-player): 音量改用非线性指数映射` |
| `revert` | 回滚某次提交 | `revert: 回滚 9680f87 的索引快照改动` |

分布参考：`feat` 41、`fix` 33、`refactor` 14、`docs` 11、`ci` 7、`chore` 6、
`test` 4、`build` 3、`perf` 1。

---

## 4. `scope` 取值

- 模块名或插件名，**小写、连字符分隔**：`shell`、`file-server`、`media-player`、
  `pixiv-sync`、`image-viewer`、`release`、`packaging`、`security`、`deps`、`logging`。
- 跨模块或全局改动**省略 scope**（历史里 65/120 条省略），不要为了凑格式硬写。
- 不用中文 scope，不自造缩写（`mp`、`iv` 之类）。

---

## 5. `subject`

- 中文，**≤ 30 字**，硬上限 40 字（历史平均 31.6 字）。
- 说明**这次改了什么**。不是现象（"装好后只剩两个插件"），不是过程（"查了半天"），
  不是自我评价（"完善了一下"）。
- 结尾不加句号；不用 `!!!`、破折号长句、书名号。
- 括号只用于限定条件，最多一处，例如 `（9 → 7 个 job）`。
- 不出现"我"。

---

## 6. 正文

按需写（历史 101/120 条带正文）。建议固定四段，没有内容的那段直接省略：

| 段 | 写什么 | 要求 |
| --- | --- | --- |
| 背景 | 问题与影响 | 能给数字就给数字："3 失败 / 6 错误"优于"一堆报错" |
| 改动 | 具体做了什么 | 涉及多个文件/模块时用列表；只列结论，不列试错过程 |
| 验证 | 怎么确认修好了 | 命令 + 结果："`unittest` 129 OK (skipped=4)"优于"实测通过" |
| 影响 | 兼容性、迁移、待人工确认 | 破坏性变更必须出现在这里并在 footer 标注 |

**不要写**：

- 排查流水账（"我先试 A，又试 B，最后发现 C"）——那是 PR 或 issue 的内容；
- 情绪与修辞（"炸了""翻车""踩坑""顺手""其实""不许""裸奔"）；
- 未量化的夸张（"彻底解决""大幅提升""完全避免"）——能量化就量化，
  不能量化就写清作用范围与限制；
- 不确定的断言（"应该没问题""可能是这里"）——不确定就写"未验证"或"待确认"；
- "我"作为主语。

**项目既有术语除外**：仓库里已经这么叫的词（例如 `fix(plugin)` 提交里的"幽灵 API"）
沿用原词比换一个自造说法更清楚。

---

## 7. 对照：不合格 → 合格

以下"不合格"一列取自 2026-09-13 那次 DSH 会话的真实提交（用于对照，不追改历史）：

| 不合格 | 问题 | 合格写法 |
| --- | --- | --- |
| `fix(packaging): 补回遗漏的 hidden import，修"装好后只剩漫画和小说"` | subject 写现象，带口语引号 | `fix(packaging): 补齐 HIDDEN_IMPORTS 缺失的 shell.backend.tasks 与 thumb_cache` |
| `ci: Linux 构建改用发行版 Python，修 CI 产物启动即失败` | 一行塞两件事，后半句是现象 | `ci(build): Linux 构建改用发行版 Python 以修复产物无法启动` |
| `fix(release): 本地打包脚本不许把开发机的用户数据打进发行包` | "不许"是情绪不是事实 | `fix(release): 本地打包脚本在压缩前拦截用户数据目录` |
| `ci(release): 发布改为半自动——tag 触发全量构建并建草稿 Release，Publish 才公开` | 破折号长句 + 口语"才公开"，超长 | `ci(release): tag 触发构建并创建草稿 Release，公开改为人工确认` |
| `test: 修正只在 Windows 成立的测试，让 ubuntu test job 转绿` | "转绿"是口语，没说明改了什么 | `test: 按平台修正依赖 Windows 语义的断言` |
| `fix(release): Linux 产物校验用重命名后的 OmniBox，修复首次发布就挂的 bug` | "就挂的 bug"口语 | `fix(release): Linux 产物校验改用重命名后的可执行文件名` |

正文对照：

```
# 不合格：现象式开头、口语、把顺带改动说成"顺手"
现象：装好的包只显示漫画和小说两个插件……
顺手把 chardet 也补上。

# 合格：客观、可复核、分段
背景：image-viewer 与 media-player 在冻结产物中加载失败，插件清单只剩
manga-library 与 novel-reader；原因是这两个插件依赖的 shell.backend.tasks 与
shell.backend.thumb_cache 未列入 HIDDEN_IMPORTS。
改动：HIDDEN_IMPORTS 补入上述两个模块；requirements.txt 补 chardet。
验证：解析产物 exe 的 PYZ 确认三个模块已进入；Linux 产物实跑，7 个插件全部加载。
```

---

## 8. 提交粒度

- **一条提交一件事**：能独立回滚，能独立用一个 subject 说清。
- 不把格式化、重命名与新功能混在一起；格式化若确实必要，单独一条提交。
- 大改动拆成多个"每一步都能跑通"的提交，而不是最后一次性提交。
- 文档与代码的同步改动可以放在同一条提交里（否则会留下"文档与实现不一致"的中间态）。

---

## 9. 提交前检查清单

- [ ] `subject` 单独看能读懂，且 ≤ 30 字
- [ ] 正文里的每条断言都能被复核（命令、文件、数字）
- [ ] 没有情绪化、不确定、未量化的措辞
- [ ] `git status` 中没有构建产物、`data/`、`.config/`、日志
- [ ] 相关门禁已跑（清单见 [AGENTS.md](../AGENTS.md)）
- [ ] 破坏性变更已用 `BREAKING CHANGE:` 标注

---

## 10. 目前没有自动校验

本文件是**约定**，不是门禁：没有 `commit-msg` 钩子，CI 也不检查提交信息。
若要收紧，可加一个 `tools/check_commit_msg.py`（校验 type 白名单、subject 长度、
措辞黑名单）+ `commit-msg` 钩子，并对既有的 merge 提交与中文正文做白名单处理。
