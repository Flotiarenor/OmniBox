<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor -->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { loadPlugins, getPlugins } from './core/plugin-loader'
import SettingsView from './views/SettingsView.vue'
import StatusView from './views/StatusView.vue'
import { toastError } from './core/toast'

const router = useRouter()
const route = useRoute()
const isReady = ref(false)
const error = ref('')
const navHidden = ref(false)

const visitedPlugins = reactive<Record<string, boolean>>({})
const activePlugin = ref<string | null>(null)
// 插件 iframe 加载到错误标记页（后端返回 data-status-page 属性）时记录状态码
const frameErrors = reactive<Record<string, number>>({})
// 重试计数：改变 iframe src 的查询参数强制重新加载
const reloadCounters = reactive<Record<string, number>>({})

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
}

function pluginSrc(p: { name: string; entryUrl: string }): string {
  const n = reloadCounters[p.name] || 0
  if (!n) return p.entryUrl
  return p.entryUrl + (p.entryUrl.includes('?') ? '&' : '?') + '_r=' + n
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
    if (plugins.length > 0 && route.path === '/') {
      router.replace(plugins[0].route)
    } else if (route.path === '/') {
      router.replace('/settings')
    }
    // 供插件 iframe 内通用扩展入口调用：跳转到某个插件路由
    ;(window as any).__omniboxNavigate = (path: string) => router.push(path)

    isReady.value = true
  } catch (e: any) {
    error.value = e.message || '未知错误'
    console.error('插件加载失败:', e)
  }

  const fsObserver = new MutationObserver(() => {
    navHidden.value = document.documentElement.getAttribute('data-video-fullscreen') === 'true'
  })
  fsObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-video-fullscreen'] })

  window.addEventListener('omnibox:api-error', onApiError)
})

onUnmounted(() => {
  window.removeEventListener('omnibox:api-error', onApiError)
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
  },
  { immediate: true }
)

const plugins = computed(() => getPlugins())
const keepAlivePlugins = computed(() => plugins.value.filter(p => !p.destroyOnLeave))
const destroyOnLeavePlugins = computed(() => plugins.value.filter(p => p.destroyOnLeave))
const currentPlugin = computed(() => route.meta.pluginName as string || '')
const isSettings = computed(() => route.path === '/settings')
const isStatus = computed(() => route.path === '/status')
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
            <span class="icon">{{ p.icon }}</span>
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
          <span class="icon">⚙️</span>
          <span class="text">设置</span>
        </div>
      </nav>
    </aside>
    <main class="main-view">
      <template v-if="isReady">
        <template v-for="p in keepAlivePlugins" :key="p.name">
          <div v-if="visitedPlugins[p.name]" v-show="activePlugin === p.name" class="plugin-frame-container">
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
        <template v-for="p in destroyOnLeavePlugins" :key="p.name">
          <div v-if="activePlugin === p.name" class="plugin-frame-container">
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
