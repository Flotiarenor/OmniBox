<!--
  OmniBox 壳内建状态视图（与 /settings 同级的基础视图）：
  - 错误模式：/status?code=404&from=/xxx —— 插件加载失败时由壳跳转至此，显示本体风格错误卡片
  - 调试模式：--status-debug 启动 —— 显示调试面板（健康检查 200 / API 鉴权 401 / 标记页链接）
  - 普通模式：直接访问 /status —— 提示调试模式未开启
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useBridge } from '../core/bridge'
import { toastError, toastSuccess, toastInfo } from '../core/toast'

const route = useRoute()
const bridge = useBridge()

const debugMode = ref(false)
const configLoaded = ref(false)

const code = computed(() => {
  const raw = route.query.code
  const n = parseInt(String(raw || ''), 10)
  return Number.isFinite(n) ? n : 0
})
const from = computed(() => String(route.query.from || ''))
const isErrorMode = computed(() => code.value > 0)
const emit = defineEmits<{ (e: 'retry', from: string): void }>()

const errorInfo = computed(() => {
  switch (code.value) {
    case 401:
      return { title: '未授权', detail: '访问该资源需要有效的访问令牌，请刷新页面后重试。' }
    case 403:
      return { title: '禁止访问', detail: '该资源超出了允许访问的目录范围。' }
    case 404:
      return { title: '页面不存在', detail: '请求的插件页面或资源不存在，可能已被移动或删除。' }
    case 500:
      return { title: '服务器错误', detail: '后端处理请求时发生异常，请查看日志后重试。' }
    default:
      return { title: `加载失败（${code.value}）`, detail: '发生未知错误，请重试。' }
  }
})

onMounted(async () => {
  try {
    const cfg = await bridge.call('system_get_config')
    debugMode.value = !!(cfg && cfg.debug && cfg.debug.status_debug)
  } catch (e) {
    debugMode.value = false
  }
  configLoaded.value = true
})

// ===== 调试面板交互 =====
const healthOut = ref('点击按钮触发')
const apiOut = ref('点击按钮触发')

async function runHealth() {
  healthOut.value = '请求中…'
  try {
    const r = await fetch('/health')
    healthOut.value = `${r.status}  ${(await r.text()).slice(0, 120)}`
  } catch (e: any) {
    healthOut.value = `异常: ${e.message}`
  }
}

async function apiDemo(mode: 'none' | 'cookie' | 'bad') {
  apiOut.value = '请求中…'
  try {
    let r: Response
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (mode === 'bad') headers['X-Omnibox-Token'] = 'bad-token'
    r = await fetch('/api/system_get_config', {
      method: 'POST',
      headers,
      body: '{}',
      // 无令牌/错误令牌演示必须禁用 Cookie，否则浏览器会自动携带有效 Cookie 导致 200
      credentials: mode === 'none' || mode === 'bad' ? 'omit' : 'same-origin',
    })
    if (mode === 'cookie' && r.status !== 200) {
      // 兜底：确保 Cookie 存在（先访问首页拿 Set-Cookie）
      await fetch('/')
      r = await fetch('/api/system_get_config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      })
    }
    const body = (await r.text()).slice(0, 150)
    apiOut.value = `${r.status}  ${body}`
    if (r.status === 200) toastSuccess('鉴权通过（200）')
    else if (r.status === 401) toastError('未授权（401）')
    else toastInfo(`响应 ${r.status}`)
  } catch (e: any) {
    apiOut.value = `异常: ${e.message}`
  }
}
</script>

<template>
  <div class="status-view">
    <!-- ===== 错误模式：插件/页面加载失败时由壳跳转至此 ===== -->
    <div v-if="isErrorMode" class="status-error-card">
      <div class="status-error-code">{{ code }}</div>
      <div class="status-error-title">{{ errorInfo.title }}</div>
      <div class="status-error-detail">{{ errorInfo.detail }}</div>
      <div class="status-error-actions">
        <button class="btn btn-primary" @click="emit('retry', from)">{{ from ? '重试' : '返回首页' }}</button>
      </div>
    </div>

    <!-- ===== 调试模式：--status-debug 启动时显示 ===== -->
    <div v-else-if="debugMode" class="status-debug">
      <div class="status-debug-header">
        <div class="status-debug-title">🔬 状态调试面板</div>
        <div class="status-debug-sub">触发 200 / 401 / 403 / 404 场景，观察标记页与壳内错误区域</div>
      </div>

      <div class="section">
        <h2>1️⃣ 健康检查（200）</h2>
        <div class="row">
          <button class="btn btn-sm" @click="runHealth">GET /health</button>
          <span class="desc">应返回 <code>200</code> JSON <code>{"status":"ok"}</code>；nginx / 探活依赖它。</span>
        </div>
        <pre class="out">{{ healthOut }}</pre>
      </div>

      <div class="section">
        <h2>2️⃣ API 鉴权（401 / 200）</h2>
        <div class="row">
          <button class="btn btn-sm" @click="apiDemo('none')">无令牌</button>
          <button class="btn btn-sm" @click="apiDemo('cookie')">带 Cookie</button>
          <button class="btn btn-sm" @click="apiDemo('bad')">错误令牌</button>
          <span class="desc">分别应返回 <code>401</code>（JSON）、<code>200</code>、<code>401</code>。
            无令牌/错误令牌演示已禁用 Cookie（否则浏览器会自动携带有效 Cookie 导致 200）。</span>
        </div>
        <pre class="out">{{ apiOut }}</pre>
      </div>

      <div class="section">
        <h2>3️⃣ 错误跳转演示（标记页 → 壳内 /status）</h2>
        <div class="row">
          <a class="btn btn-sm" target="_blank" href="/thumbs/x.png">401 未授权</a>
          <a class="btn btn-sm" target="_blank" href="/file?path=..%2Fsecret&plugin=image-viewer">403 越权</a>
          <a class="btn btn-sm" target="_blank" href="/plugins/nope/frontend/index.html">404 不存在</a>
          <a class="btn btn-sm" target="_blank" href="/some/spa/route">200 SPA fallback</a>
          <span class="desc">
            后端标记页只负责正确返回 HTTP 状态码（curl / 探活依赖），浏览器顶层打开时
            会自动跳转到本视图（<code>/status?code=…</code>）统一展示，不再出现独立页面。
            注意：浏览器已持有令牌 Cookie 时，<code>/thumbs/x.png</code> 会通过鉴权显示
            <code>404</code>（文件不存在）而非 401；401 需隐身窗口 / curl 访问。</span>
        </div>
      </div>

      <div class="section">
        <h2>4️⃣ 壳内错误区域（iframe 404 → 自动跳转到本视图）</h2>
        <div class="row">
          <span class="desc">导航栏打开「💥 调试坏插件」（调试服务器注入）→ 壳检测到 iframe 加载了
            404 标记页 → 自动跳转到 <code>/status?code=404&from=…</code>，显示上方错误卡片；点「重试」返回并强制重载。</span>
        </div>
        <div class="row">
          <span class="hint">💡 API 错误 Toast：在主界面 F12 Console 执行<br>
            <code>fetch('/api/no_such_method', {method:'POST'})</code> → 404 Toast<br>
            <code>fetch('/api/system_get_config', {method:'POST', credentials:'omit'})</code> → 401 Toast</span>
        </div>
      </div>

      <div class="section">
        <h2>5️⃣ 校验清单</h2>
        <div class="row">
          <span class="hint">
            <span class="tag ok">200</span> /health JSON &nbsp;·&nbsp;
            <span class="tag err">401</span> 无令牌 API & 页面 &nbsp;·&nbsp;
            <span class="tag err">403</span> 越权路径 &nbsp;·&nbsp;
            <span class="tag err">404</span> 不存在资源 / 未知 API（GET 也返回 404 JSON）&nbsp;·&nbsp;
            <span class="tag">200</span> SPA 前端路由 fallback 保留
          </span>
        </div>
      </div>
    </div>

    <!-- ===== 普通模式：未开启调试 ===== -->
    <div v-else-if="configLoaded" class="status-error-card">
      <div class="status-error-code">🔒</div>
      <div class="status-error-title">调试模式未开启</div>
      <div class="status-error-detail">
        状态调试面板仅在启动时携带 <code>--status-debug</code> 参数时可用。<br>
        例如：<code>python main.py --web-only --status-debug</code>
      </div>
    </div>
  </div>
</template>

<style scoped>
.status-view {
  width: 100%;
  height: 100%;
  overflow-y: auto;
  background: var(--bg-app);
  display: flex;
  align-items: flex-start;
  justify-content: center;
}
.status-error-card {
  margin-top: 15vh;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
  padding: 40px 52px;
  text-align: center;
  max-width: 520px;
}
.status-error-code {
  font-size: 64px;
  font-weight: 700;
  line-height: 1.1;
  color: var(--danger);
}
.status-error-title {
  font-size: 18px;
  font-weight: 600;
  margin: 12px 0 8px;
  color: var(--text-primary);
}
.status-error-detail {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.8;
  margin-bottom: 24px;
}
.status-error-actions .btn {
  min-width: 96px;
}
.status-debug {
  width: 100%;
  max-width: 860px;
  padding: 24px 20px 60px;
}
.status-debug-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-primary);
}
.status-debug-sub {
  font-size: 13px;
  color: var(--text-secondary);
  margin: 4px 0 20px;
}
.section {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px 20px;
  margin-bottom: 14px;
  box-shadow: var(--shadow-sm);
}
.section h2 {
  font-size: 14px;
  color: var(--text-primary);
  margin: 0 0 10px;
}
.row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin: 6px 0;
}
.row .desc {
  font-size: 12px;
  color: var(--text-secondary);
  width: 100%;
  line-height: 1.7;
}
.tag {
  font-size: 12px;
  font-weight: 600;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--bg-hover);
  color: var(--text-secondary);
}
.tag.ok { background: var(--success); color: #fff; }
.tag.err { background: var(--danger); color: #fff; }
.out {
  margin-top: 8px;
  padding: 8px 12px;
  font-size: 12px;
  font-family: Consolas, monospace;
  background: var(--bg-app);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-all;
  min-height: 18px;
}
code {
  background: var(--bg-hover);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 12px;
}
.hint {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.8;
}
</style>
