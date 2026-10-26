<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<script setup lang="ts">
/**
 * 壳渲染的插件扩展入口。
 *
 * 宿主不再自己拼扩展侧栏：壳从 `system_get_plugin_extensions` 取入口，按 section 分组渲染成
 * 导航项；点击交给 PluginChrome 决定是打开 native 面板还是发视图消息。
 */
import { computed, onMounted, ref } from 'vue'
import Icon from './Icon.vue'
import { useBridge } from '../core/bridge'

interface ExtensionEntry {
  id?: string
  label?: string
  icon?: string
  section?: string
  plugin?: string
  view?: string
  embedUrl?: string
  native?: { url?: string; title?: string } | null
}

const props = defineProps<{
  plugin: string
  title?: string
  /** 唯一的选中态来源：由 PluginChrome 维护，主导航点击或 native 关闭时清空。 */
  activeId?: string
}>()

const emit = defineEmits<{
  (e: 'open', entry: ExtensionEntry): void
}>()

const bridge = useBridge()
const entries = ref<ExtensionEntry[]>([])

const groups = computed(() => {
  const map = new Map<string, ExtensionEntry[]>()
  for (const entry of entries.value) {
    const key = entry.section || props.title || '扩展'
    if (!map.has(key)) map.set(key, [])
    map.get(key)!.push(entry)
  }
  return Array.from(map, ([title, items]) => ({ title, items }))
})

onMounted(async () => {
  try {
    const list = await bridge.call('system_get_plugin_extensions', props.plugin, 'sidebar')
    entries.value = Array.isArray(list) ? list as ExtensionEntry[] : []
  } catch {
    entries.value = []
  }
})

function open(entry: ExtensionEntry): void {
  emit('open', entry)
}
</script>

<template>
  <template v-for="group in groups" :key="group.title">
    <div class="plugin-nav-label">{{ group.title }}</div>
    <button
      v-for="entry in group.items"
      :key="entry.id || entry.plugin || group.title"
      type="button"
      class="obx-nav-item plugin-nav-item plugin-extension-item"
      :class="{ active: props.activeId === (entry.id || entry.view || entry.plugin) }"
      @click="open(entry)"
    >
      <Icon v-if="entry.icon" :name="entry.icon" />
      <span class="plugin-nav-text">{{ entry.label || entry.id }}</span>
    </button>
  </template>
</template>
