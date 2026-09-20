<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor -->
<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { loadPlugins, getPlugins } from './core/plugin-loader'
import {
  disposeFrames,
  ensureFrame,
  LIFECYCLE_MESSAGES,
  noteFrameEpoch,
  refreshFrame,
  type LifecycleMessage,
} from './core/plugin-visibility'
import SettingsView from './views/SettingsView.vue'
import StatusView from './views/StatusView.vue'
import Icon from './components/Icon.vue'
import { toastError } from './core/toast'
import { useBridge } from './core/bridge'
import { getStartupPrefs, rememberLastRoute, resolveStartRoute } from './core/preferences'

const router = useRouter()
const route = useRoute()
const bridge = useBridge()
const isReady = ref(false)
const error = ref('')
const navHidden = ref(false)

const visitedPlugins = reactive<Record<string, boolean>>({})
const activePlugin = ref<string | null>(null)
// 插件 iframe 加载到错误标记页（后端返回 data-status-page 属性）时记录状态码
const frameErrors = reactive<Record<string, number>>({})
// 重试计数：改变 iframe src 的查询参数强制重新加载
const reloadCounters = reactive<Record<string, number>>({})

// 必须定义在下面的 route watcher 之前：watcher 是 immediate，会在 setup 期同步执行
const plugins = computed(() => getPlugins())
const keepAlivePlugins = computed(() => plugins.value.filter(p => p.keepAlive))
// 默认组：不声明 keepAlive 的插件离开即卸载 iframe，回来是干净重载
const transientPlugins = computed(() => plugins.value.filter(p => !p.keepAlive))
const currentPlugin = computed(() => route.meta.pluginName as string || '')
const isSettings = computed(() => route.path === '/settings')
const isStatus = computed(() => route.path === '/status')

function onFrameLoad(e: Event, name: string) {
  try {
    const doc = (e.target as HTMLIFrameElement).contentDocument
    const code = doc?.documentElement.getAttribute('data-status-page')
    if (code) {
      frameErrors[name] = Number(code) || 0
      // 跳转到壳内建状态视图展示错误（保留来源路径用于「重试」返回）
      router.push({
        path: '/status',
        query: { code: String(frameErrors[name]), from: route.fullPath },
      })
    } else {
      delete frameErrors[name]
    }
  } catch {
    // 跨域无法读取内容时不处理，保持原行为
    delete frameErrors[name]
  }
  // 新挂载/重载的 frame 必须显式登记初始状态：不保活的插件在 iframe 加载完成前
  // 就可能被切走，此时它还没有过任何状态；不登记的话它会在后台被误判为"可见"，
  // 第一次真正显示时反而收不到通知。重载（重试 / 设置变更改 src）会递增文档代次，
  // 让状态机丢掉旧文档的结论并重新通知一次 —— 否则后台重载的新文档会以为自己可见。
  const mounted = activePlugin.value === name
  const epoch = (frameEpochs.get(name) || 0) + 1
  frameEpochs.set(name, epoch)
  frameMounted.set(name, mounted)
  const reloaded = noteFrameEpoch(name, epoch)
  const messages = reloaded
    ? (mounted && windowVisible.value ? [LIFECYCLE_MESSAGES.shown] : HIDDEN_BY_DEFAULT)
    : ensureFrame(name, mounted)
  notifyFrame(name, messages)
  refreshFrameVisibility(name)
}

function pluginSrc(p: { name: string; entryUrl: string }): string {
  const n = reloadCounters[p.name] || 0
  if (!n) return p.entryUrl
  return p.entryUrl + (p.entryUrl.includes('?') ? '&' : '?') + '_r=' + n
}

// ===== 插件生命周期通知（docs/core-contract-fixes.md §3） =====
// 声明了 `keepAlive: true` 的插件用 keep-alive（v-show 隐藏），切走以后其定时器 /
// rAF 自循环 / 轮询仍在跑，插件前端也无从知道自己的 iframe 是否可见。宿主因此在三种
// 情形下通知插件（不保活的插件切走即卸载，只会收到 dispose）：
//   1. 切换插件（activePlugin 变化）：刚变为非活动 → hidden，刚变为活动 → shown；
//   2. 窗口 visibilitychange（最小化 / 切标签页）：与"可见"取交集；
//   3. beforeunload / pagehide：已挂载的 frame 一律 dispose。
// "该不该发"由 plugin-visibility 状态机判定（只发真正的状态变化）。
const frameRefs = new Map<string, HTMLIFrameElement>()
// 每个 frame 是否已作为活动插件挂载（不保活的插件切走即卸载）
const frameMounted = new Map<string, boolean>()
// 每个 frame 的文档代次（每次 iframe load 递增）：重载后旧文档的可见性结论作废
const frameEpochs = new Map<string, number>()
// 窗口级可见性：最小化 / 切标签页时为 false
const windowVisible = ref(true)
const HIDDEN_BY_DEFAULT: LifecycleMessage[] = []

function setFrameRef(name: string, el: Element | null) {
  const frame = el?.querySelector('iframe')
  if (frame) frameRefs.set(name, frame as HTMLIFrameElement)
}

function sendToFrame(name: string, message: string) {
  const frame = frameRefs.get(name)
  const target = frame?.contentWindow
  if (!target) return
  try {
    target.postMessage({ type: message }, window.location.origin)
  } catch (e) {
    console.warn(`[Shell] 向插件 ${name} 发送 ${message} 失败:`, e)
  }
}

// 以状态机给出的通知为准发消息（状态机内部已去重）
function notifyFrame(name: string, messages: LifecycleMessage[]) {
  if (!frameRefs.has(name)) return
  messages.forEach(message => sendToFrame(name, message))
}

function refreshFrameVisibility(name: string) {
  const state = {
    mounted: !!frameMounted.get(name),
    active: activePlugin.value === name,
    windowVisible: windowVisible.value,
  }
  notifyFrame(name, refreshFrame(name, state))
}

// 路由或窗口可见性变化后，让所有已挂载 frame 的可见性状态重新对齐
function refreshAllFrameVisibility() {
  frameRefs.forEach((_frame, name) => refreshFrameVisibility(name))
}

// 不保活组：离开即卸载 iframe，卸载前发一次 dispose
function disposeLeavingFrames(keepAliveNames: Set<string>) {
  const leaving = Array.from(frameRefs.keys()).filter(name => {
    if (keepAliveNames.has(name)) return false
    if (!frameMounted.get(name)) return false
    frameMounted.set(name, false)
    return true
  })
  disposeFrames(leaving).forEach((message, index) => sendToFrame(leaving[index], message))
}

function onVisibilityChange() {
  windowVisible.value = document.visibilityState !== 'hidden'
  refreshAllFrameVisibility()
}

function onPageHide() {
  // 页面即将卸载：一次性通知所有 frame 释放资源（监听器 / 定时器 / rAF）
  const names = Array.from(frameRefs.keys())
  disposeFrames(names).forEach((message, index) => sendToFrame(names[index], message))
}

function handleRetry(from: string) {
  // 从错误视图「重试」：强制重载对应插件再返回
  const p = getPlugins().find(pl => pl.route === from)
  if (p) {
    delete frameErrors[p.name]
    reloadCounters[p.name] = (reloadCounters[p.name] || 0) + 1
  }
  router.push(from || '/')
}

function onApiError(ev: Event) {
  const d = (ev as CustomEvent).detail
  if (!d || typeof d.status !== 'number') return
  const method = d.method || ''
  if (d.status === 401) toastError('访问令牌无效或缺失（401），请刷新页面重试')
  else if (d.status === 403) toastError('无权访问该资源（403）')
  else if (d.status === 404) toastError(`接口不存在（404）: ${method}`)
  else if (d.status >= 500) toastError(`后端错误（${d.status}）: ${method}`)
}

onMounted(async () => {
  try {
    await loadPlugins()
    const plugins = getPlugins()
    plugins.forEach(p => {
      router.addRoute({
        path: p.route,
        name: p.name,
        component: { template: '<div></div>' },
        meta: { entryUrl: p.entryUrl, pluginName: p.name }
      })
    })
    if (route.path === '/') {
      // 首屏目标由「启动与窗口」偏好决定（默认回到上次离开的页面）
      router.replace(resolveStartRoute(plugins.map(p => p.route)))
    }
    // 供插件 iframe 内通用扩展入口调用：跳转到某个插件路由
    ;(window as any).__omniboxNavigate = (path: string) => router.push(path)

    isReady.value = true

    if (getStartupPrefs().fullscreen) {
      // 桌面窗口：让 pywebview 进全屏；浏览器模式下后端是空实现，忽略即可
      try { await bridge.call('system_toggle_fullscreen') } catch { /* 非桌面模式 */ }
    }
  } catch (e: any) {
    error.value = e.message || '未知错误'
    console.error('插件加载失败:', e)
  }

  const fsObserver = new MutationObserver(() => {
    navHidden.value = document.documentElement.getAttribute('data-video-fullscreen') === 'true'
  })
  fsObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-video-fullscreen'] })

  window.addEventListener('omnibox:api-error', onApiError)
  window.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('beforeunload', onPageHide)
  window.addEventListener('pagehide', onPageHide)
})

onUnmounted(() => {
  window.removeEventListener('omnibox:api-error', onApiError)
  window.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('beforeunload', onPageHide)
  window.removeEventListener('pagehide', onPageHide)
})

watch(
  () => route.meta.pluginName,
  (name) => {
    if (name && typeof name === 'string') {
      visitedPlugins[name] = true
      activePlugin.value = name
      // 上次加载出错后重新进入：清除错误记录并强制重载 iframe，
      // 避免重新显示旧的错误标记页
      if (frameErrors[name]) {
        delete frameErrors[name]
        reloadCounters[name] = (reloadCounters[name] || 0) + 1
      }
    } else if (route.path === '/settings') {
      activePlugin.value = null
    } else if (route.path === '/status') {
      activePlugin.value = null
    }
    // 记下当前页面：下次启动若选「上次离开的页面」就回到这里
    if (name || route.path === '/settings') rememberLastRoute(route.path)
    // 路由切换后的可见性收敛：keep-alive 的 iframe 已存在（这里同步发通知），
    // 不保活的 iframe 由本次渲染挂载/卸载，交给 setFrameRef 与下面的
    // nextTick 兜底，保证两种形态都恰好收到一次 shown / hidden / dispose。
    refreshAllFrameVisibility()
    nextTick(() => {
      frameRefs.forEach((_frame, frameName) => {
        if (transientPlugins.value.some(p => p.name === frameName)) {
          frameMounted.set(frameName, activePlugin.value === frameName)
          notifyFrame(frameName, ensureFrame(frameName, activePlugin.value === frameName))
        }
      })
      disposeLeavingFrames(new Set(keepAlivePlugins.value.map(p => p.name)))
      refreshAllFrameVisibility()
    })
  },
  { immediate: true }
)
</script>

<template>
  <div class="app-shell">
    <aside class="nav-sidebar" :class="{ 'nav-hidden': navHidden }">
      <div class="logo">OmniBox</div>
      <nav v-if="isReady">
        <template v-if="plugins.length > 0">
          <div
            v-for="p in plugins" :key="p.name"
            class="nav-item" :class="{ active: currentPlugin === p.name }"
            @click="router.push(p.route)"
          >
            <Icon :name="p.icon" class="icon" />
            <span class="text">{{ p.displayName }}</span>
          </div>
        </template>
        <div v-else-if="error" class="error">{{ error }}</div>
        <div v-else class="hint">暂无插件</div>
        <div class="nav-divider"></div>
        <div
          class="nav-item" :class="{ active: isSettings }"
          @click="router.push('/settings')"
        >
          <Icon name="icon:settings" class="icon" />
          <span class="text">设置</span>
        </div>
      </nav>
    </aside>
    <main class="main-view">
      <template v-if="isReady">
        <template v-for="p in keepAlivePlugins" :key="p.name">
          <div
            v-if="visitedPlugins[p.name]" v-show="activePlugin === p.name" class="plugin-frame-container"
            :ref="el => setFrameRef(p.name, el as Element | null)"
          >
            <iframe
              :src="pluginSrc(p)"
              :data-plugin-name="p.name"
              frameborder="0"
              class="plugin-iframe"
              allow="fullscreen *"
              @load="onFrameLoad($event, p.name)"
            ></iframe>
          </div>
        </template>
        <template v-for="p in transientPlugins" :key="p.name">
          <div
            v-if="activePlugin === p.name" class="plugin-frame-container"
            :ref="el => setFrameRef(p.name, el as Element | null)"
          >
            <iframe
              :src="pluginSrc(p)"
              :data-plugin-name="p.name"
              frameborder="0"
              class="plugin-iframe"
              allow="fullscreen *"
              @load="onFrameLoad($event, p.name)"
            ></iframe>
          </div>
        </template>
        <SettingsView v-show="isSettings" />
        <StatusView v-show="isStatus" @retry="handleRetry" />
      </template>
      <div v-else-if="error" class="loading">插件加载失败: {{ error }}</div>
      <div v-else class="loading">框架加载中...</div>
    </main>
  </div>
</template>

<style scoped>
.plugin-frame-container {width: 100%;height: 100%;border: none;outline: none;}
.plugin-iframe {width: 100%;height: 100%;border: 0;outline: none;display: block;}
</style>
