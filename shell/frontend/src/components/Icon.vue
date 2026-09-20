<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<script setup lang="ts">
/**
 * 图标组件：渲染壳图标集里的一个图标。
 *
 * 用法：`<Icon name="settings" />`（或 manifest 的 `icon:settings` 原样传入）
 *
 * 三条刻意的设计：
 * 1. **`icon:` 前缀的字符串仍按文本渲染**。manifest 的 `icon` 字段历史上存的是 emoji，
 *    第三方/旧插件不改也能正常显示；新插件写成 `icon:<名字>` 即得到矢量图标。
 *    这样"统一图标"不需要给 manifest 加新字段，也不需要枚举映射表。
 * 2. **引用的是同文档的 `#名字`，不是外部文件**。外部文件的 `<use>` 在 pywebview 的
 *    WebView2 里不渲染（实测包围盒恒为 0，DOM 与控制台都正常），所以 sprite 由
 *    `icons.generated.ts` 在挂载前内联进文档。详见该文件头的实测对照表。
 * 3. `ensureIcons()` 幂等，且在组件挂载前先调一次，避免首帧取不到 symbol。
 */
import { computed, onMounted } from 'vue'
import { ensureIcons } from '../core/icons.generated'

const props = withDefaults(defineProps<{ name?: string; large?: boolean }>(), {
  name: '',
  large: false,
})

/** 没有声明 `icon` 时的默认图标（manifest 的 `icon` 必填，这里只作兜底） */
const DEFAULT_ICON = 'icon:package'

/** 实际参与渲染的值：`icon:` 前缀走 sprite，其它按纯文本渲染（旧插件传的 emoji） */
const iconValue = computed(() => props.name || DEFAULT_ICON)

const isSprite = computed(() => iconValue.value.startsWith('icon:'))

const symbolId = computed(() => iconValue.value.slice('icon:'.length))

/** 同文档引用：`#名字`。外部文件引用在 WebView2 里不渲染，见文件头说明。 */
const spriteHref = computed(() => `#${symbolId.value}`)

const textValue = computed(() => (isSprite.value ? '' : iconValue.value))

onMounted(ensureIcons)
</script>

<template>
  <svg v-if="isSprite" class="obx-icon" :class="{ 'obx-icon-lg': large }" aria-hidden="true">
    <use :href="spriteHref" />
  </svg>
  <span v-else class="obx-icon-text">{{ textValue }}</span>
</template>
