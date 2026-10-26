# shell/frontend

Vue 3 + TypeScript + Vite。`src/` 是壳的界面（`components/` `core/` `router/` `styles/` `views/`、
`App.vue`、`main.ts`），`public/shell/` 是注入给插件的共享资源。
根 [AGENTS.md](../../AGENTS.md) 的全局约定仍然适用。

## 共享资源有两份副本

`public/shell/*` 会被 Vite 复制进 `dist/shell/`。

- `/shell/<file>` 优先发 `dist/shell/`，dist 中不存在时回落 `public/shell/`（`serve_shell_assets`）。
- **改完 `public/shell/` 下任何文件必须重新构建**：`npm --prefix shell/frontend run build`。只改源文件会继续发旧副本，症状是"新标记 + 旧样式"，容易被误判为 CSS 写错。
- 壳首页（`/`、`/settings`、插件路由）由 `dist/index.html` 提供，dist 缺失时直接 404；CI 在跑测试前会先构建一次。
- `dist/` 不提交（`.gitignore`）。

`public/shell/` 共 16 个文件：

| 类别 | 文件 |
| --- | --- |
| 样式 | `variables.css` `shared.css` `base.css` `effects.css` |
| 组件 | `folder-picker.{css,js}` `freshness.{css,js}` `splitter.{css,js}` `content-ui.{css,js}` |
| 脚本 | `base.js` `native-client.js` `motion.js` `icons.generated.js` |

## 与 `res/` 的区别

`res/` 不经 Vite，只有一份，改完立即生效、不需要构建（`serve_res_assets`）。
图标 sprite 放在 `res/icons/icons.svg`，由 `tools/build_icons.py` 生成。

## 门禁

```bash
node tools/check_frontend_escape.cjs      # 属性注入的转义一致性
python tools/check_npm_audit.py           # 依赖漏洞：脚本内部固定官方源（本机 registry 不实现 audit 端点）
npm --prefix shell/frontend run build     # vue-tsc --noEmit + vite build
```

前端纯逻辑用例在 `tests/js/*.mjs`，由 `tests/test_*_js.py` 用 node 驱动，见 [tests/AGENTS.md](../../tests/AGENTS.md)。
