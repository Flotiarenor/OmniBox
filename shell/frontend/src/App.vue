<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor -->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { loadPlugins, getPlugins } from './core/plugin-loader'
import SettingsView from './views/SettingsView.vue'

const router = useRouter()
const route = useRoute()
const isReady = ref(false)
const error = ref('')
const navHidden = ref(false)

const visitedPlugins = reactive<Record<string, boolean>>({})
const activePlugin = ref<string | null>(null)

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
    isReady.value = true
  } catch (e: any) {
    error.value = e.message || '未知错误'
    console.error('插件加载失败:', e)
  }

  const fsObserver = new MutationObserver(() => {
    navHidden.value = document.documentElement.getAttribute('data-video-fullscreen') === 'true'
  })
  fsObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-video-fullscreen'] })
})

watch(
  () => route.meta.pluginName,
  (name) => {
    if (name && typeof name === 'string') {
      visitedPlugins[name] = true
      activePlugin.value = name
    } else if (route.path === '/settings') {
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
            <iframe :src="p.entryUrl" frameborder="0" class="plugin-iframe" allow="fullscreen *"></iframe>
          </div>
        </template>
        <template v-for="p in destroyOnLeavePlugins" :key="p.name">
          <div v-if="activePlugin === p.name" class="plugin-frame-container">
            <iframe :src="p.entryUrl" frameborder="0" class="plugin-iframe" allow="fullscreen *"></iframe>
          </div>
        </template>
        <SettingsView v-show="isSettings" />
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