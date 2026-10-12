# 提交规范（工具可执行版）

这个文件只做一件事：把 [提交信息规范](./commit-convention.md) 的规则写成**键值**，由 DSH 的
`git_commit` 工具在提交前逐条校验（不合规**直接拒绝**，不修改、不猜测）。

- 键怎么写、有哪些取值、拒绝码是什么：
  https://github.com/Flotiarenor/dsh-tool-git/blob/main/README.zh.md#conventions-keys
- 规则本身、措辞排除表与对照例子：[AGENTS.md](../AGENTS.md) 与
  [docs/commit-convention.md](./commit-convention.md)——本文件不重复第三遍。

取值依据取自仓库既有写法：近 150 条非 merge 提交的 subject 平均 48.5 显示列、最长 80 列（中文按 2 列计），
故硬上限取 80 列；正文普遍是"一个自然段一行"并直接贴命令输出，故行宽放到 400 列。

types: feat, fix, refactor, docs, test, ci, build, chore, perf, revert
subjectMaxColumns: 80
bodyMaxColumns: 400
bodySoftChars: 400
bodyConfirmChars: 600
language: zh
requireBody: false
allowEmoji: false
banned: 我, 炸了, 搞定, 顺手, 翻车, 踩坑, 不许, 才公开, 转绿, 全绿, 单跑, 写全了, 到底, 其实, 裸奔, 彻底解决, 大幅提升, 完全避免, 应该没问题, 可能是这里

## 五条说明

- **正文默认从简**：只写 diff 无法体现的内容——原因、影响范围、验证方式；无内容则整段省略。不要为
  格式补"背景"段，也不要复述改动本身（diff 已说明）。多数提交 3–5 行、100–300 字；一目了然的改动
  可以只写 subject。
- **用书面语陈述**：不用口语动词与含糊名词（"搞""弄""压低""东西"），专有名词按代码或界面中的写法。
  陈述事实与结果，不写判断与评价（"没有意义""效果不错"）。
- 正文长度分级，不是硬上限：≤ 400 字静默通过；超过 400 字仅**告警**；超过 600 字**拒绝一次**，要求
  压缩，或带 `confirm_length: true` 重新调用（确认放行的那次会留一条 `body.length-confirmed` 告警）。
  需要确认的正文，通常是过程叙述与实现细节过多。
- `banned` 里的 `我` 对 subject 与正文**都**生效；引述用户原话时改为转述（"我看不到封面" →
  "用户看不到封面"）。
- `subjectMaxColumns: 80` 是**硬上限**（≈ 40 字，超过拒绝）；文档中"≤ 30 字"是写作目标，工具不拦。
  正文中每条断言要能被复核（命令、文件、数字）——这条工具不校验，靠自觉。
