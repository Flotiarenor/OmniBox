# 说明

> 目标分支默认为 **`develop`**（日常改动都先进 develop）。只有发布/热修才把 `develop`
> 合到 `main`。开 PR 时请在页面顶部确认 base 分支，别默认落在 `main` 上。

## 背景

<!-- 为什么改？现象是什么？尽量给可复核的证据：报错原文、命令输出、日志行。
     "修了个 bug"这类空话不算背景 —— 评审需要能独立判断"这个改动是否对症"。 -->

## 改动

<!-- 这次改了什么（按文件或按模块列点）。如果有取舍/权衡，把结论写在这里：
     为什么选 A 不选 B，被排除的方案是什么。 -->

## 验证

<!-- 列真实跑过的命令与结果，不要写"应该是好的"。
     ```
     python -m unittest discover -s tests      # 178 例通过
     python tools/check_plugins.py             # 退出码 0
     ```
     涉及界面/交互的改动，请写清"在哪看、看到什么"（必要时附截图或日志片段）。 -->

## 评审重点

<!-- 希望评审者特别看哪一部分？哪里你自己不确定？ -->
- [ ] 逻辑正确性
- [ ] 是否影响既有插件 / 既有数据
- [ ] 类型与门禁

## 自查清单

- [ ] `git status` 干净：没有把 `data/`、`.config/`、`logs/`、构建产物（`dist/`、`__pycache__/`）带进提交
- [ ] 与改动相关的门禁跑过：`ruff` / `check_version` / `check_plugins` / `check_packaging` / `unittest`
      （改到打包相关文件时，另跑一次真实构建 + `tools/check_build_tree.py`）
- [ ] 提交信息符合 [提交信息规范](../docs/commit-convention.md)：`<type>(<scope>): <subject>`，正文写背景/改动/验证
- [ ] **涉及前端界面改动的，用真实浏览器看过**（`python main.py` 起应用，别只看单测）：
      有一类缺陷是"接口数据正常、界面静默不更新且控制台无报错"，单测与桩都抓不到
      （本地端到端用例：`venv/Scripts/python -m unittest tests.test_shell_browser_e2e -v`）
- [ ] **只在单一平台成立的测试/代码已排除**（Windows 与 POSIX 对路径分隔符、大小写、文件锁的行为不同）
- [ ] 新增/改动的 `manifest` 字段已在 `tools/check_plugins.py` 的登记表里归类
- [ ] 改动到的契约（基类成员、前端注入 API、消息协议）已同步 `docs/` 文档

## 关联

<!-- 例如：Refs #12、docs/core-contract-fixes.md §3 -->
