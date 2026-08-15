/*This product includes software developed by flotiarenor.Copyright 2026 flotiarenor*/
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { createRouter } from './router'
import { useBridge } from './core/bridge'
import './styles/shell.css'  // 引入 Shell 样式
import { applyStoredAppearance } from './core/appearance'

async function bootstrap() {
  // 在挂载 Vue 前恢复主题/自定义颜色，确保插件 iframe 首次加载时就能同步到。
  applyStoredAppearance()
  const bridge = useBridge()
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