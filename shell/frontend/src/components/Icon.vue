<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<script setup lang="ts">
/**
 * 图标组件：渲染壳 sprite（`/shell/icons.svg`）里的一个图标。
 *
 * 用法：`<Icon name="settings" />`（或 manifest 的 `icon:settings` 原样传入）
 *
 * 两条刻意的设计：
 * 1. **`icon:` 前缀的字符串仍按文本渲染**。manifest 的 `icon` 字段历史上存的是 emoji，
 *    第三方/旧插件不改也能正常显示；新插件写成 `icon:<名字>` 即得到矢量图标。
 *    这样"统一图标"不需要给 manifest 加新字段，也不需要枚举映射表。
 * 2. 单个 `<use>` 指向外部 sprite，**不内联图形**：一份 sprite 被壳与所有插件 iframe
 *    共享，浏览器只下载一次。`href` 用绝对路径 `/shell/icons.svg` —— 壳是 history
 *    路由（`/settings` 这类路径），相对路径会在嵌套路由下解析错。
 */
import { computed } from 'vue'

const props = withDefaults(defineProps<{ name?: string; large?: boolean }>(), {
  name: '',
  large: false,
})

/** 非 `icon:` 的值按纯文本渲染时的兜底（manifest 的默认图标） */
const TEXT_FALLBACK = '📦'

const isSprite = computed(() => props.name.startsWith('icon:'))

const symbolId = computed(() => (isSprite.value ? props.name.slice('icon:'.length) : ''))

const spriteHref = computed(() => `/shell/icons.svg#${symbolId.value}`)

const textValue = computed(() => (isSprite.value ? '' : props.name || TEXT_FALLBACK))
</script>

<template>
  <svg v-if="isSprite" class="obx-icon" :class="{ 'obx-icon-lg': large }" aria-hidden="true">
    <use :href="spriteHref" />
  </svg>
  <span v-else class="obx-icon-text">{{ textValue }}</span>
</template>
