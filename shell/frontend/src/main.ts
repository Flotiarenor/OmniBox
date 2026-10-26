/*This product includes software developed by flotiarenor.Copyright 2026 flotiarenor*/
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { createRouter } from './router'
import { useBridge } from './core/bridge'
import './styles/shell.css'  // 引入 Shell 样式
import './styles/plugin-page.css'  // 壳渲染的插件页面骨架（PluginChrome）
import { applyStoredAppearance } from './core/appearance'
import { applyBackground } from './core/background'
import { loadShellPrefs, onShellPrefsChanged } from './core/shell-prefs'

async function bootstrap() {
  const bridge = useBridge()
  // 拉齐偏好（迁移旧 localStorage 取值 + 与后端对齐）必须发生在**任何绘制之前**：
  // 注入的是"页面被渲染出来的那一刻"的快照，后端可能在那之后重启过。
  await loadShellPrefs()
  // 画布就绪后再接上"偏好变化即重画"，避免启动期把同一份状态画两遍
  onShellPrefsChanged(applyStoredAppearance)
  onShellPrefsChanged(applyBackground)
  // 壳偏好由后端在 index.html 里注入（`window.__OMNIBOX_SHELL_STATE__`），对齐之后
  // 立刻画一遍，保证插件 iframe 首次加载就能同步到主题与自定义色。
  applyStoredAppearance()
  applyBackground()

  await bridge.init()

  const router = createRouter()
  const app = createApp(App)
  app.use(createPinia())
  app.use(router)
  app.mount('#app')
  await router.isReady()
}

bootstrap().catch(err => {
  console.error('启动失败:', err)
  document.getElementById('app')!.innerHTML = `<div style="padding:40px;text-align:center;color:red;">启动失败: ${err}</div>`
})
