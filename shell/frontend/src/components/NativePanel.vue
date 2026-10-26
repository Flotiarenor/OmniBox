<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<script setup lang="ts">
/**
 * 原生内容面板：逻辑在隐藏的 content iframe 里，渲染在壳里。
 *
 * 附属插件提交 blocks（SchemaRenderer 的输入），动作回传给 iframe 里的插件逻辑；
 * 壳不直接调用插件后端，也不解释业务。面板只覆盖内容区，宿主页 chrome 仍然可见。
 */
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import SchemaRenderer from './SchemaRenderer.vue'
import type { SchemaAction, SchemaBlock } from '../core/plugin-content'

const props = defineProps<{
  plugin: string
  url: string
  title: string
  /** 宿主插件页面的背景不透明度；缺省时退到壳的全局值。 */
  alpha?: number
}>()

const emit = defineEmits<{
  (e: 'toolbar', actions: SchemaAction[]): void
}>()

const frame = ref<HTMLIFrameElement | null>(null)
const blocks = ref<SchemaBlock[]>([])
const toolbar = ref<SchemaAction[]>([])
const error = ref('')
const preview = ref<{ src: string; alt: string } | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)
/** 打开预览前焦点所在的缩略图按钮；关闭后把焦点还回去。 */
let previewTrigger: HTMLElement | null = null
let listener: ((event: MessageEvent) => void) | null = null

function isOwnFrame(source: unknown): boolean {
  return !!frame.value && frame.value.contentWindow === source
}

function post(payload: Record<string, unknown>): void {
  const target = frame.value?.contentWindow
  if (!target) return
  target.postMessage(payload, window.location.origin)
}

function onMessage(event: MessageEvent): void {
  if (event.origin !== window.location.origin || !isOwnFrame(event.source)) return
  const data = event.data
  if (!data || typeof data.type !== 'string') return
  if (data.type === 'omnibox:native-ready') {
    post({ type: 'omnibox:native-init', plugin: props.plugin })
    return
  }
  if (data.type === 'omnibox:schema') {
    if (Array.isArray(data.blocks)) {
      blocks.value = data.blocks
      error.value = ''
      toolbar.value = Array.isArray(data.toolbar) ? data.toolbar : []
      emit('toolbar', toolbar.value)
    } else {
      error.value = '插件返回的 blocks 不是数组'
    }
    return
  }
  if (data.type === 'omnibox:native-error') {
    error.value = String(data.error || '内容加载失败')
  }
}

function onAction(payload: { action: string; payload?: unknown }): void {
  // SchemaRenderer 的 blocks 来自 Vue 响应式代理，action.payload 也可能是 proxy；
  // postMessage 的结构化克隆不认识 Proxy，必须按 JSON 契约先转成普通数据。
  let plainPayload = payload.payload
  if (plainPayload !== undefined) {
    try {
      plainPayload = JSON.parse(JSON.stringify(plainPayload))
    } catch {
      plainPayload = undefined
    }
  }
  post({ type: 'omnibox:native-action', action: payload.action, payload: plainPayload })
}

/** 宿主顶栏动作回发给隐藏逻辑页。 */
function sendAction(action: string, payload?: unknown): void {
  let plain = payload
  if (plain !== undefined) {
    try {
      plain = JSON.parse(JSON.stringify(plain))
    } catch {
      plain = undefined
    }
  }
  post({ type: 'omnibox:native-action', action, payload: plain })
}

defineExpose({ action: sendAction })

/** 缩略图预览：遮罩、大图与关闭按钮都画在本组件里，不借用宿主页的灯箱。 */
function openPreview(payload: { src: string; alt: string }): void {
  if (!payload || !payload.src) return
  previewTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
  preview.value = { src: payload.src, alt: payload.alt || '' }
  nextTick(() => closeButton.value?.focus())
}

function closePreview(): void {
  if (!preview.value) return
  preview.value = null
  const trigger = previewTrigger
  previewTrigger = null
  nextTick(() => trigger?.focus())
}

/** Esc 关闭预览；面板自身的关闭按钮不受影响。 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape' || !preview.value) return
  event.stopPropagation()
  closePreview()
}

onMounted(() => {
  listener = onMessage
  window.addEventListener('message', listener)
  window.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
  if (listener) window.removeEventListener('message', listener)
  listener = null
  window.removeEventListener('keydown', onKeydown)
  preview.value = null
})
</script>

<template>
  <section class="native-panel" :style="{ '--plugin-glass-alpha': `${alpha ?? 84}%` }" :aria-label="title">
    <div class="native-panel-body">
      <SchemaRenderer v-if="blocks.length" :blocks="blocks" @action="onAction" @preview="openPreview" />
      <div v-else-if="error" class="empty-state empty-state--error">
        <div class="empty-state-text">{{ error }}</div>
      </div>
      <div v-else class="loading">正在加载…</div>
    </div>
    <iframe
      ref="frame"
      :src="url"
      :data-native-plugin="plugin"
      class="native-panel-frame"
      title="native content"
    ></iframe>
    <Teleport to="body">
      <div
        v-if="preview"
        class="native-preview"
        role="dialog"
        aria-modal="true"
        :aria-label="preview.alt ? `预览：${preview.alt}` : '图片预览'"
        @click.self="closePreview"
      >
        <button ref="closeButton" type="button" class="btn btn-sm native-preview-close" @click="closePreview">
          关闭
        </button>
        <img class="native-preview-image" :src="preview.src" :alt="preview.alt || '图片预览'">
      </div>
    </Teleport>
  </section>
</template>
