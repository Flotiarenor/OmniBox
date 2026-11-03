# tests

根 [AGENTS.md](../AGENTS.md) 的全局约定仍然适用。

## 运行

```bash
venv/bin/python -m unittest discover -s tests                  # 全量（本地）
venv/bin/python -m unittest tests.test_file_server_paths -v     # 单例
venv/bin/python tools/plugin_ci.py --core                       # CI 的共享测试
venv/bin/python tools/plugin_ci.py --plugin image-viewer        # CI 的单插件测试
venv/bin/python tools/shell_e2e_smoke.py                        # 壳的真实浏览器冒烟（CI 的 shell-e2e job）
```

CI 不再直接跑全量 `discover`：`tools/plugin_ci.py` 按 `test_<插件名下划线>_*` 前缀把
插件测试拆到矩阵 job，其余测试进 `--core`；两份选择器由 `tests/test_plugin_ci.py` 与
`tests/test_gate_coverage_review.py` 断言互斥、覆盖全部测试文件，并锁定"选择器 == test_owner"
与插件前缀两两不重叠。新增插件测试按该前缀命名即可，不需要改 workflow。

## 四类文件

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| `test_*.py` | 133 | `unittest` 用例，被 `discover` 收集；其中 9 个是下表的 `*_e2e.py`，38 个是 `test_*_js.py`（调用 `tests/js/*.mjs` 的包装器） |
| `*_e2e.py` | 9 | 真实浏览器端到端，需要 `requirements-e2e.txt`（selenium）与 Chrome；其中壳整体链路与分隔条两条进 CI |
| `tests/js/*.mjs` | 45 | 41 个可独立运行的用例文件（由相关的 `test_*_js.py` 用 node 驱动，**不是一一对应**：一个包装器可以跑多个 mjs，例如 image-viewer 的契约用例遍历多份分片脚本）+ 4 个共享夹具（`dom_stub.mjs` `script_load_contract.mjs` `shell_scripts.mjs` `manga_library_harness.mjs`，只被别的 `.mjs` import、自己不跑断言） |

`tests/harness/` 是多实例用例的共享工具（`app_instance.py` `browser_binary.py` `support.py`），不是被测代码，也不被收集。

手工调试脚本**不在这个目录**：它们统一放 `tools/debug/`（见 [tools/AGENTS.md](../tools/AGENTS.md)）。
`tests/` 只留被 `unittest` 收集的用例、e2e 与 `harness/` —— 一份文件是不是测试只靠文件名判断，
把"现场观察用的小工具"放在这里，只会让"被收集但什么都不测"的坑重新长回来。

## 规则

- **跑测试前先构建前端**：壳首页由 `shell/frontend/dist/index.html` 提供，而 `dist/` 不在仓库里；否则 `test_file_server_shell_state.py` 的用例拿不到首页。CI 在 `unittest` 之前先执行 `npm --prefix shell/frontend run build`。
- **node 缺失时 `tests/js/*.mjs` 静默跳过**：驱动都带 `@unittest.skipUnless(shutil.which('node'), …)`，"全绿"不代表前端逻辑被测过。
- **e2e 需要单装依赖**：`venv/Scripts/pip install -r requirements-e2e.txt`（selenium）；未安装时其余 7 个 e2e 自动跳过。壳整体链路（`test_shell_browser_e2e.py`）与分隔条（`test_splitter_browser_e2e.py`）是例外：`tools/shell_e2e_smoke.py` 在 CI 的 shell-e2e job 里跑它们，入口把"用例被跳过""用例数少于预期"都判为失败，所以缺 selenium / Chrome / 前端产物会红，而不是绿着什么都没测。Selenium Manager 会把驱动解到当前工作目录，所以 `chromedriver/` 与 `se-metadata.json` 在 `.gitignore` 中。
- **新增 `test_*.py` 必须真的产出用例**：`unittest discover` 会导入文件名匹配的文件，一个没有 `TestCase` 的 `test_*.py` 会静默地什么都不测；门禁是 `python tools/check_tests.py`。

## 门禁

```bash
venv/bin/python tools/check_tests.py
venv/bin/python -m unittest tests.test_no_print_in_runtime    # 运行时禁止 print（发行版 console=False）
```
