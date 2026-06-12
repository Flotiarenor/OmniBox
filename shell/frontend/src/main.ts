/*This product includes software developed by flotiarenor.Copyright 2026 flotiarenor*/
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { createRouter } from './router'
import { useBridge } from './core/bridge'
import './styles/shell.css'  // 引入 Shell 样式

async function bootstrap() {
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