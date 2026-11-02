<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<script setup lang="ts">
/**
 * 原生内容面板：逻辑在隐藏的 content iframe 里，渲染在壳里。
 *
 * 附属插件提交 blocks（SchemaRenderer 的输入），动作回传给 iframe 里的插件逻辑；
 * 壳不直接调用插件后端，也不解释业务。面板只覆盖内容区，宿主页 chrome 仍然可见。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import SchemaRenderer from './SchemaRenderer.vue'
import Icon from './Icon.vue'
import { toast, toastError, toastInfo, toastSuccess } from '../core/toast'
import type { SchemaAction, SchemaBlock, SchemaToast } from '../core/plugin-content'

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
/** 逻辑页声明的版式；缺省 default 保持既有排版。 */
const layout = ref<'default' | 'document'>('default')
const error = ref('')
const preview = ref<{ src: string; alt: string } | null>(null)
const closeButton = ref<HTMLButtonElement | null>(null)
/** 打开预览前焦点所在的缩略图按钮；关闭后把焦点还回去。 */
let previewTrigger: HTMLElement | null = null
let listener: ((event: MessageEvent) => void) | null = null

const confirmCancel = ref<HTMLButtonElement | null>(null)
/** 打开确认框前焦点所在的元素；关闭后还回去（元素已卸载时跳过）。 */
let confirmTrigger: HTMLElement | null = null
/** 最近一次弹过的 Toast，用于去重：轮询重发同样的文本不该刷屏。 */
let lastToast = ''

const confirmBlock = computed<SchemaBlock | null>(() => findConfirm(blocks.value))

/** 递归查找确认块：`confirm` 可能出现在任意 `section` 里。 */
function findConfirm(list: SchemaBlock[] | undefined): SchemaBlock | null {
  for (const block of list || []) {
    if (block.type === 'confirm') return block
    if (Array.isArray(block.blocks)) {
      const found = findConfirm(block.blocks)
      if (found) return found
    }
  }
  return null
}

// 弹窗开关完全由 blocks 派生：块在则开、块消失则关，不另存 open 布尔，
// 因此轮询重发、切走再回来都不会与插件的状态分叉。
watch(confirmBlock, (now, before) => {
  if (now && !before) {
    confirmTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    nextTick(() => confirmCancel.value?.focus())
  } else if (!now && before) {
    const trigger = confirmTrigger
    confirmTrigger = null
    if (trigger && trigger.isConnected) nextTick(() => trigger.focus())
  }
})

/** 确认层的动作直接回发给隐藏逻辑页（与 SchemaRenderer 的动作同一条通道）。 */
function fireAction(action: SchemaAction | undefined): void {
  if (!action) return
  sendAction(action.id, action.payload)
}

/** 确认层内的 Tab 循环：焦点不许跑到弹窗后面的面板上。 */
function trapFocus(event: KeyboardEvent): void {
  if (event.key !== 'Tab') return
  const root = event.currentTarget as HTMLElement | null
  if (!root) return
  const items = Array.from(root.querySelectorAll<HTMLElement>('button:not([disabled])'))
  if (!items.length) return
  const first = items[0]
  const last = items[items.length - 1]
  const active = document.activeElement
  if (event.shiftKey && active === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

/** 瞬时反馈：tone 映射到壳的 Toast 类型；同一段文本连发只弹一次。 */
function showToast(payload: SchemaToast | undefined): void {
  if (!payload || !payload.text) return
  const key = (payload.tone || 'default') + ':' + payload.text
  if (key === lastToast) return
  lastToast = key
  const text = payload.text
  if (payload.tone === 'success') toastSuccess(text)
  else if (payload.tone === 'danger') toastError(text)
  else if (payload.tone === 'warning') toast(text, 'warning')
  else toastInfo(text)
}

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
      layout.value = data.layout === 'document' ? 'document' : 'default'
      emit('toolbar', toolbar.value)
      if (data.toast && typeof data.toast.text === 'string') {
        showToast(data.toast as SchemaToast)
      }
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

/** Esc 关闭预览；确认层打开时 Esc 等于取消。 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  if (confirmBlock.value) {
    event.stopPropagation()
    fireAction(confirmBlock.value.cancel)
    return
  }
  if (!preview.value) return
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
      <SchemaRenderer
        v-if="blocks.length"
        :blocks="blocks"
        :layout="layout"
        @action="onAction"
        @preview="openPreview"
      />
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
    <!-- 阻断确认：遮罩画在 .native-panel 内部，不 Teleport 到 body —— 面板被隐藏或
         卸载时它一起消失，不会留下盖在别处的孤儿弹窗。 -->
    <div
      v-if="confirmBlock"
      class="native-confirm"
      @click.self="fireAction(confirmBlock.cancel)"
      @keydown="trapFocus"
    >
      <div
        class="native-confirm-box"
        role="dialog"
        aria-modal="true"
        aria-labelledby="native-confirm-title"
        tabindex="-1"
      >
        <div class="native-confirm-head">
          <Icon
            v-if="confirmBlock.icon || confirmBlock.tone === 'danger'"
            :name="confirmBlock.icon || 'icon:triangle-alert'"
            class="native-confirm-icon"
          />
          <h2 id="native-confirm-title" class="native-confirm-title">
            {{ confirmBlock.title || '确认操作' }}
          </h2>
        </div>
        <p v-if="confirmBlock.text" class="native-confirm-text">{{ confirmBlock.text }}</p>
        <div class="native-confirm-actions">
          <button
            v-if="confirmBlock.cancel"
            ref="confirmCancel"
            type="button"
            class="btn"
            @click="fireAction(confirmBlock.cancel)"
          >{{ confirmBlock.cancel.label }}</button>
          <button
            v-if="confirmBlock.confirm"
            type="button"
            class="btn btn-danger-solid"
            @click="fireAction(confirmBlock.confirm)"
          >{{ confirmBlock.confirm.label }}</button>
        </div>
      </div>
    </div>
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
