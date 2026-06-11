<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { loadPlugins, getPlugins } from './core/plugin-loader'

const router = useRouter()
const route = useRoute()
const isReady = ref(false)
const error = ref('')

onMounted(async () => {
  try {
    await loadPlugins()
    const plugins = getPlugins()
    plugins.forEach(p => {
      router.addRoute({
        path: p.route,
        name: p.name,
        component: () => import('./views/PluginFrame.vue'),
        meta: { entryUrl: p.entryUrl, pluginName: p.name }
      })
    })
    if (plugins.length > 0 && route.path === '/') {
      router.replace(plugins[0].route)
    }
    isReady.value = true
  } catch (e: any) {
    error.value = e.message || '未知错误'
    console.error('插件加载失败:', e)
  }
})

const plugins = computed(() => getPlugins())
const currentPlugin = computed(() => route.meta.pluginName as string || '')
</script>

<template>
  <div class="app-shell">
    <aside class="nav-sidebar">
      <div class="logo">OmniBox</div>
      <nav v-if="isReady && plugins.length > 0">
        <div
          v-for="p in plugins" :key="p.name"
          class="nav-item" :class="{ active: currentPlugin === p.name }"
          @click="router.push(p.route)"
        >
          <span class="icon">{{ p.icon }}</span>
          <span class="text">{{ p.displayName }}</span>
        </div>
      </nav>
      <div v-else-if="error" class="error">{{ error }}</div>
      <div v-else class="hint">暂无插件</div>
    </aside>
    <main class="main-view">
      <router-view v-if="isReady" />
      <div v-else-if="error" class="loading">插件加载失败: {{ error }}</div>
      <div v-else class="loading">框架加载中...</div>
    </main>
  </div>
</template>

<style scoped>
</style>