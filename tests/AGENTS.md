# tests

根 [AGENTS.md](../AGENTS.md) 的全局约定仍然适用。

## 运行

```bash
venv/bin/python -m unittest discover -s tests                  # 全量
venv/bin/python -m unittest tests.test_file_server_paths -v     # 单例
```

## 四类文件

| 类别 | 数量 | 说明 |
| --- | --- | --- |
| `test_*.py` | 84 | `unittest` 用例，被 `discover` 收集 |
| `debug_*.py` | 16 | 手工调试脚本（带 `__main__`），不被收集；改动某个功能时用它现场观察 |
| `*_e2e.py` | 9 | 真实浏览器端到端，需要 `requirements-e2e.txt`，不进 CI |
| `tests/js/*.mjs` | 26 | 无头前端用例（DOM 替身 + 真实脚本），由同名 `test_*_js.py` 用 node 驱动 |

`tests/harness/` 是多实例用例的共享工具（`app_instance.py` `browser_binary.py` `support.py`），不是被测代码，也不被收集。

## 规则

- **跑测试前先构建前端**：壳首页由 `shell/frontend/dist/index.html` 提供，而 `dist/` 不在仓库里；否则 `test_file_server_shell_state.py` 的用例拿不到首页。CI 在 `unittest` 之前先执行 `npm --prefix shell/frontend run build`。
- **node 缺失时 `tests/js/*.mjs` 静默跳过**：驱动都带 `@unittest.skipUnless(shutil.which('node'), …)`，"全绿"不代表前端逻辑被测过。
- **e2e 需要单装依赖**：`venv/Scripts/pip install -r requirements-e2e.txt`（selenium）；未安装时自动跳过。Selenium Manager 会把驱动解到当前工作目录，所以 `chromedriver/` 与 `se-metadata.json` 在 `.gitignore` 中。
- **新增 `test_*.py` 必须真的产出用例**：`unittest discover` 会导入文件名匹配的文件，一个没有 `TestCase` 的 `test_*.py` 会静默地什么都不测；门禁是 `python tools/check_tests.py`。

## 门禁

```bash
venv/bin/python tools/check_tests.py
venv/bin/python -m unittest tests.test_no_print_in_runtime    # 运行时禁止 print（发行版 console=False）
```
