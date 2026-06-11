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
    
    // 动态注入插件路由
    plugins.forEach(p => {
      router.addRoute({
        path: p.route,
        name: p.name,
        component: () => import('./views/PluginFrame.vue'),
        meta: { entryUrl: p.entryUrl, pluginName: p.name }
      })
    })
    
    // 跳转到第一个插件
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
html, body {
  margin: 0 !important;
  padding: 0 !important;
  width: 100%;
  height: 100%;
  overflow: hidden;          /* 防止 body 自身滚动条 */
  background: #ffffff;       /* 与主区域背景一致 */
}
.app-shell {
  --sidebar-bg: #1e2a3a;
  --main-bg: #ffffff;          /* 纯白背景，消除突兀感 */
  --text-light: #ecf0f1;
  --text-muted: #95a5a6;
  --accent: #3498db;
  --sidebar-width: 200px;

  display: flex;
  height: 100vh;
  width: 100vw;
  background: var(--main-bg);
  color: #2c3e50;
  font-family: 'Segoe UI', system-ui, sans-serif;
}

.nav-sidebar {
  width: var(--sidebar-width);
  background: var(--sidebar-bg);
  color: var(--text-light);
  display: flex;
  flex-direction: column;
  /* 移除 box-shadow，消除右侧“白边框”错觉 */
  overflow-y: auto;            /* 保留滚动功能 */

  /* 隐藏滚动条（Firefox） */
  scrollbar-width: none;
  /* 隐藏滚动条（IE/Edge） */
  -ms-overflow-style: none;
}
/* 隐藏滚动条（Chrome/Safari/新Edge） */
.nav-sidebar::-webkit-scrollbar {
  display: none;
}

.logo {
  padding: 24px 20px;
  font-size: 20px;
  font-weight: 600;
  letter-spacing: 1px;
  border-bottom: 1px solid rgba(255,255,255,0.08);
  color: #fff;
}

.nav-item {
  padding: 12px 20px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 12px;
  transition: background 0.2s;
  border-left: 3px solid transparent;
}
.nav-item:hover {
  background: rgba(255,255,255,0.05);
}
.nav-item.active {
  background: rgba(52,152,219,0.15);
  border-left-color: var(--accent);
  color: #fff;
}

.icon { font-size: 18px; }
.text { font-size: 14px; }

.main-view {
  flex: 1;
  display: flex;
  overflow: hidden;            /* 防止主区域出现滚动条，滚动由插件内部处理 */
  background: var(--main-bg);
}

.loading,
.error,
.hint {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  color: var(--text-muted);
}
.error { color: #e74c3c; }
</style>

<!-- 全局样式：移除之前自定义的滚动条样式，因为已隐藏 -->
<style>
/* 确保所有区域默认不显示滚动条（若未来有其他可滚动区域可按需开启） */
* {
  scrollbar-width: none; /* Firefox */
  -ms-overflow-style: none; /* IE/Edge */
}
*::-webkit-scrollbar {
  display: none; /* Chrome/Safari/新Edge */
}
</style>