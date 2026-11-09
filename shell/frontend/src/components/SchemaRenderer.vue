<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<script setup lang="ts">
/**
 * native 内容的唯一渲染器。
 *
 * 输入是插件给的 blocks，输出是壳的组件；交互只回传 {action, payload}，
 * 不把业务规则带进壳。canvas 内容不走这里。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import Icon from './Icon.vue'
import type {
  SchemaAction,
  SchemaBlock,
  SchemaField,
  SchemaFieldOption,
  SchemaGroup,
  SchemaItem,
} from '../core/plugin-content'

const props = defineProps<{
  blocks: SchemaBlock[]
  /** `document` 约束正文度量；缺省维持既有排版。 */
  layout?: 'default' | 'document'
  /** 嵌套渲染（`section` 内部）：不再产出滚动容器与版式内边距，间距交给父级。 */
  nested?: boolean
}>()

const emit = defineEmits<{
  (e: 'action', payload: { action: string; payload?: unknown }): void
  (e: 'preview', payload: { src: string; alt: string }): void
}>()

const checked = ref<Record<string, boolean>>({})
const formValues = ref<Record<string, string | number | boolean>>({})
/** directory 字段的路径列表；multi 字段可有多行，提交时序列化为换行文本。 */
const dirs = ref<Record<string, string[]>>({})
/** 正在选择目录的字段 key；空串表示没有进行中的选择。 */
const picking = ref('')
const pickError = ref('')
/** 正在显示“已复制”的 code 块键。 */
const copied = ref('')
let copyTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 递归遍历 blocks（`section` 可嵌套）。表单与勾选状态原来只扫顶层，
 * 加嵌套后必须深度遍历，否则分区里的字段拿不到初值。
 */
function walkBlocks(list: SchemaBlock[] | undefined, visit: (block: SchemaBlock) => void): void {
  for (const block of list || []) {
    visit(block)
    if (Array.isArray(block.blocks)) walkBlocks(block.blocks, visit)
  }
}

/** 动作按钮的类：danger/primary 沿用旧命名，accent/link 是补充档位。 */
function actionClass(action: SchemaAction | undefined): string {
  if (!action) return 'btn'
  const size = action.size === 'sm' ? ' btn-sm' : ''
  if (action.variant === 'primary') return 'btn btn-primary' + size
  if (action.variant === 'danger') return 'btn btn-danger' + size
  if (action.variant === 'accent') return 'btn btn-accent' + size
  if (action.variant === 'link') return 'btn btn-link' + size
  return 'btn' + size
}

function codeKey(block: SchemaBlock, index: number): string {
  return block.id || 'code-' + index
}

/** 复制：优先剪贴板 API；被拒时退回选中 + execCommand（WebView2 下仍然可用）。 */
async function copyCode(block: SchemaBlock, index: number): Promise<void> {
  const text = String(block.text == null ? '' : block.text)
  if (!text) return
  const key = codeKey(block, index)
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    try {
      const area = document.createElement('textarea')
      area.value = text
      area.setAttribute('readonly', '')
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      document.execCommand('copy')
      document.body.removeChild(area)
    } catch {
      return
    }
  }
  copied.value = key
  if (copyTimer) clearTimeout(copyTimer)
  copyTimer = setTimeout(() => {
    if (copied.value === key) copied.value = ''
    copyTimer = null
  }, 2000)
}

onBeforeUnmount(() => {
  if (copyTimer) clearTimeout(copyTimer)
  copyTimer = null
})

interface FolderPickerProvider {
  label?: string
  plugin?: string
  embedUrl?: string
  icon?: string
}

interface FolderPickerApi {
  openDirBrowser(path?: string, mount?: HTMLElement, returnFocus?: HTMLElement): Promise<string | null>
  loadNetworkProviders(): Promise<FolderPickerProvider[]>
  openProviderMenu(
    providers: FolderPickerProvider[],
    mount?: HTMLElement,
    returnFocus?: HTMLElement,
  ): Promise<FolderPickerProvider | null>
  openNetworkPicker(
    provider: FolderPickerProvider,
    mount?: HTMLElement,
    returnFocus?: HTMLElement,
  ): Promise<{ action: string; path?: string; label?: string } | null>
}

/** 壳内目录选择器由 /shell/folder-picker.js 挂到 window，native 不再走系统对话框。 */
function folderPicker(): FolderPickerApi | null {
  return (window as unknown as { FolderPicker?: FolderPickerApi }).FolderPicker || null
}

function normalizeDirPaths(raw: unknown): string[] {
  if (Array.isArray(raw)) {
    return raw.map(item => String(item).trim()).filter(Boolean)
  }
  return String(raw == null ? '' : raw)
    .split('\n')
    .map(item => item.trim())
    .filter(Boolean)
}

/** 目录字段的 DOM 行始终保留一个空行；提交时只序列化非空路径。 */
function directoryRows(raw: unknown, multi: boolean): string[] {
  const paths = normalizeDirPaths(raw)
  const rows = multi ? paths : paths.slice(0, 1)
  return rows.length ? rows : ['']
}

function directoryText(rows: string[], multi: boolean): string {
  const paths = rows.map(path => String(path).trim()).filter(Boolean)
  return multi ? paths.join('\n') : (paths[0] || '')
}

function syncDirectory(field: SchemaField): void {
  formValues.value[field.key] = directoryText(dirs.value[field.key] || [''], !!field.multi)
}

function pickToken(field: SchemaField, index?: number): string {
  return index === undefined ? field.key : field.key + ':' + index
}

function isPicking(field: SchemaField, index?: number): boolean {
  return picking.value === pickToken(field, index)
}

function currentDir(field: SchemaField, index = 0): string {
  return (dirs.value[field.key] || [''])[index] || ''
}

function applyDirectoryPath(field: SchemaField, path: string, index?: number): void {
  const value = String(path || '').trim()
  if (!value) return
  const rows = dirs.value[field.key] || ['']
  if (field.multi) {
    if (index !== undefined && index >= 0 && index < rows.length) rows[index] = value
    else if (!rows.includes(value)) rows.push(value)
  } else {
    rows.splice(0, rows.length, value)
  }
  syncDirectory(field)
}

function setDirectoryPath(field: SchemaField, index: number, event: Event): void {
  const rows = dirs.value[field.key] || ['']
  rows[index] = (event.target as HTMLInputElement).value
  syncDirectory(field)
}

function addDirectory(field: SchemaField): void {
  const rows = dirs.value[field.key] || ['']
  rows.push('')
  syncDirectory(field)
}

function removeDirectory(field: SchemaField, index: number): void {
  const rows = dirs.value[field.key] || ['']
  if (rows.length <= 1) rows[0] = ''
  else rows.splice(index, 1)
  syncDirectory(field)
}

/** 单行字段的清空：多行字段由每行的“移除”处理。 */
function clearDirectory(field: SchemaField): void {
  dirs.value[field.key] = ['']
  syncDirectory(field)
}

/** 打开壳内 FolderPicker；返回路径回填到当前行（多行字段按行号）。 */
async function pickDirectory(field: SchemaField, index?: number, event?: Event): Promise<void> {
  if (picking.value) return
  const trigger = event && event.currentTarget instanceof HTMLElement ? event.currentTarget : undefined
  trigger?.focus()
  const picker = folderPicker()
  if (!picker) {
    pickError.value = '目录选择器未加载'
    return
  }
  picking.value = pickToken(field, index)
  pickError.value = ''
  try {
    const path = await picker.openDirBrowser(currentDir(field, index ?? 0), undefined, trigger)
    if (path) applyDirectoryPath(field, path, index)
  } catch (error) {
    pickError.value = error instanceof Error ? error.message : '无法打开目录选择'
  } finally {
    picking.value = ''
    nextTick(() => trigger?.focus())
  }
}

/** 网络位置：提供方把共享项取到本地目录后回填；local_only 字段不显示入口。 */
async function pickNetworkDirectory(field: SchemaField, index?: number, event?: Event): Promise<void> {
  if (picking.value) return
  const trigger = event && event.currentTarget instanceof HTMLElement ? event.currentTarget : undefined
  trigger?.focus()
  const picker = folderPicker()
  if (!picker) {
    pickError.value = '目录选择器未加载'
    return
  }
  picking.value = pickToken(field, index)
  pickError.value = ''
  try {
    const providers = await picker.loadNetworkProviders()
    if (!providers.length) {
      pickError.value = '没有可用的网络位置来源：需要安装提供该能力的插件（如「团体组网」）'
      return
    }
    const provider = providers.length === 1
      ? providers[0]
      : await picker.openProviderMenu(providers, undefined, trigger)
    if (!provider) return
    const picked = await picker.openNetworkPicker(provider, undefined, trigger)
    if (!picked || picked.action !== 'picked' || !picked.path) return
    applyDirectoryPath(field, picked.path, index)
  } catch (error) {
    pickError.value = error instanceof Error ? error.message : '无法打开网络位置'
  } finally {
    picking.value = ''
    nextTick(() => trigger?.focus())
  }
}

function initState(): void {
  const nextChecked: Record<string, boolean> = {}
  const nextForm: Record<string, string | number | boolean> = {}
  const nextDirs: Record<string, string[]> = {}
  walkBlocks(props.blocks, (block) => {
    if (block.type === 'checkbox-list') {
      for (const group of block.groups || []) {
        for (const item of group.items || []) {
          if (item.value) nextChecked[item.value] = !!item.checked
        }
      }
    }
    if (block.type === 'form') {
      for (const field of block.fields || []) {
        const initial: unknown = field.value !== undefined ? field.value : field.default
        if (field.type === 'directory') {
          const rows = directoryRows(initial, !!field.multi)
          nextDirs[field.key] = rows
          nextForm[field.key] = directoryText(rows, !!field.multi)
          continue
        }
        if (typeof initial === 'string' || typeof initial === 'number'
            || typeof initial === 'boolean') {
          nextForm[field.key] = initial
        }
      }
    }
  })
  checked.value = nextChecked
  formValues.value = nextForm
  dirs.value = nextDirs
  picking.value = ''
  pickError.value = ''
}

watch(() => props.blocks, initState, { immediate: true, deep: true })

function fire(action?: SchemaAction, payload?: unknown): void {
  if (!action) return
  emit('action', { action: action.id, payload: payload === undefined ? action.payload : payload })
}

function toggle(value: string): void {
  checked.value[value] = !checked.value[value]
  emit('action', { action: 'toggle', payload: { value, checked: checked.value[value] } })
}

function selectedValues(): string[] {
  return Object.keys(checked.value).filter(key => checked.value[key])
}

function submitCheckbox(block: SchemaBlock): void {
  fire(block.submit, { values: selectedValues() })
}

function submitForm(block: SchemaBlock): void {
  pickError.value = ''
  for (const field of block.fields || []) {
    if (field.type === 'directory') syncDirectory(field)
  }
  fire(block.submit, { values: { ...formValues.value } })
}

function setFieldText(key: string, event: Event): void {
  const target = event.target as HTMLInputElement | HTMLTextAreaElement
  formValues.value[key] = target.value
}

/** 缩略图点击后由 NativePanel 在壳文档里画遮罩预览，不引入 base.js 的灯箱。 */
function previewItem(item: SchemaItem): void {
  const src = item.image
  if (!src) return
  emit('preview', { src, alt: item.title || item.label || '' })
}

/** 根节点类：顶层是滚动容器（可选 document 版式），嵌套时只是一个普通堆叠容器。 */
const rootClass = computed(() => {
  if (props.nested) return 'schema-stack'
  return props.layout === 'document' ? 'schema-root schema-root--document' : 'schema-root'
})

/** 嵌套渲染（`section`）时把预览事件继续上抛给 NativePanel。 */
function forwardPreview(payload: { src: string; alt: string }): void {
  emit('preview', payload)
}

/**
 * 嵌套渲染（`section`）时把动作上抛给 NativePanel。
 *
 * 递归组件有自己的 emit 域：子组件的 `emit('action')` 不会自动冒泡到父组件的监听者，
 * 必须在这一层重新 emit 一次。早期版本把模板写成 `@action="onAction"`（那是 NativePanel
 * 里的函数名，这里并不存在），结果是 section 内的所有按钮点了都没有任何反应。
 */
function forwardAction(payload: { action: string; payload?: unknown }): void {
  emit('action', payload)
}

/** 动作按钮的尺寸类；缺省不写，沿用 `.btn` 的默认尺寸。 */
function actionSizeClass(action: SchemaAction | undefined): string {
  return action && action.size === 'sm' ? ' btn-sm' : ''
}

function previewLabel(item: SchemaItem): string {
  const name = item.title || item.label || ''
  return name ? `预览 ${name}` : '预览图片'
}

function optionValue(option: SchemaFieldOption | string): string {
  return typeof option === 'object' ? option.value : option
}

function optionLabel(option: SchemaFieldOption | string): string {
  return typeof option === 'object' ? (option.label || option.value) : option
}

function groupActions(group: SchemaGroup): SchemaAction[] {
  return group.actions || []
}

interface FormFieldGroup {
  key: string
  title: string
  description: string
  fields: SchemaField[]
}

/**
 * 表单字段按可选 `section` 元数据分组。
 *
 * 与壳内设置弹窗（`PluginSettingsModal.fieldGroups`）同一套规则：`section` 作分组键，
 * `sectionTitle` / `sectionDescription` 是组标题与说明，`sectionOrder` 与 `order` 决定
 * 组间与组内次序；没有写 section 的旧 schema 归入无标题组，渲染结果与原来一致。
 */
function fieldGroups(block: SchemaBlock): FormFieldGroup[] {
  const groups = new Map<string, {
    key: string
    title: string
    description: string
    order: number
    firstIndex: number
    fields: Array<{ field: SchemaField; index: number }>
  }>()
  ;(block.fields || []).forEach((field, index) => {
    const key = field.section || ''
    let group = groups.get(key)
    if (!group) {
      group = {
        key,
        title: '',
        description: '',
        order: Number.POSITIVE_INFINITY,
        firstIndex: index,
        fields: [],
      }
      groups.set(key, group)
    }
    if (!group.title) group.title = field.sectionTitle || field.section || ''
    if (!group.description) group.description = field.sectionDescription || ''
    const sectionOrder = Number(field.sectionOrder)
    group.order = Math.min(group.order, Number.isFinite(sectionOrder) ? sectionOrder : index)
    group.fields.push({ field, index })
  })
  const ordered = Array.from(groups.values()).map(group => ({
    key: group.key,
    title: group.title,
    description: group.description,
    order: group.order,
    firstIndex: group.firstIndex,
    fields: group.fields
      .slice()
      .sort((a, b) => {
        const ao = Number(a.field.order)
        const bo = Number(b.field.order)
        const av = Number.isFinite(ao) ? ao : a.index
        const bv = Number.isFinite(bo) ? bo : b.index
        return av - bv || a.index - b.index
      })
      .map(item => item.field),
  }))
  ordered.sort((a, b) => a.order - b.order || a.firstIndex - b.firstIndex)
  return ordered.map(group => ({
    key: group.key,
    title: group.title,
    description: group.description,
    fields: group.fields,
  }))
}

/** stats 的列数交给 CSS 自适应（`auto-fit`），不提供按块覆盖。 */
</script>

<template>
  <div :class="rootClass">
    <template v-for="(block, index) in blocks" :key="block.id || index">
      <p v-if="block.type === 'text'" class="schema-text" :class="block.tone ? `schema-tone-${block.tone}` : ''">
        <span v-if="block.dot" class="schema-dot" :class="`schema-dot--${block.dot}`" aria-hidden="true"></span>{{ block.text }}
      </p>

      <h2 v-else-if="block.type === 'heading'" class="schema-heading">{{ block.text }}</h2>

      <dl v-else-if="block.type === 'stats'" class="schema-stats">
        <div v-for="(metric, mi) in block.metrics || []" :key="mi" class="schema-stat">
          <dt class="schema-stat-label">{{ metric.label }}</dt>
          <dd class="schema-stat-value" :class="metric.tone ? `schema-tone-${metric.tone}` : ''">
            {{ metric.value }}
          </dd>
          <dd v-if="metric.hint" class="schema-stat-hint">{{ metric.hint }}</dd>
          <dd v-if="metric.action" class="schema-stat-action">
            <button type="button" :class="actionClass(metric.action)" :disabled="metric.action.disabled" @click="fire(metric.action)">
              {{ metric.action.label }}
            </button>
          </dd>
        </div>
      </dl>

      <section v-else-if="block.type === 'section'" class="schema-section">
        <div v-if="block.title" class="schema-section-head">
          <h3 class="schema-section-title">{{ block.title }}</h3>
        </div>
        <div class="schema-section-body">
          <SchemaRenderer
            v-if="block.blocks && block.blocks.length"
            :blocks="block.blocks"
            :layout="layout"
            nested
            @action="forwardAction"
            @preview="forwardPreview"
          />
        </div>
      </section>

      <div
        v-else-if="block.type === 'banner'"
        class="schema-banner"
        :class="block.tone ? `schema-tone-${block.tone}` : 'schema-tone-default'"
        role="status"
        aria-live="polite"
      >
        <Icon v-if="block.icon" :name="block.icon" class="schema-banner-icon" />
        <span class="schema-banner-text">{{ block.text }}</span>
        <button
          v-if="block.action"
          type="button"
          :class="actionClass(block.action)"
          :disabled="block.action.disabled"
          @click="fire(block.action)"
        >
          {{ block.action.label }}
        </button>
      </div>

      <div v-else-if="block.type === 'code'" class="schema-code">
        <div class="schema-code-head">
          <span v-if="block.language" class="schema-code-language">{{ block.language }}</span>
          <button
            v-if="block.copyable !== false"
            type="button"
            class="btn btn-link schema-code-copy"
            :aria-label="'复制' + (block.language || '文本')"
            @click="copyCode(block, index)"
          >
            {{ copied === codeKey(block, index) ? '已复制' : '复制' }}
          </button>
        </div>
        <pre class="schema-code-body" :class="{ 'is-wrapped': block.wrap !== false }"><code>{{ block.text }}</code></pre>
      </div>

      <dl v-else-if="block.type === 'kv'" class="schema-kv">
        <template v-for="(item, i) in block.items || []" :key="i">
          <dt>{{ item.title }}</dt>
          <dd>{{ item.sub }}</dd>
        </template>
      </dl>

      <table v-else-if="block.type === 'table'" class="schema-table">
        <thead>
          <tr><th v-for="(col, i) in block.columns || []" :key="i">{{ col }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="(row, ri) in block.rows || []" :key="ri">
            <td v-for="(cell, ci) in row" :key="ci">{{ cell }}</td>
          </tr>
        </tbody>
      </table>

      <ul v-else-if="block.type === 'list'" class="schema-list">
        <li v-for="(item, i) in block.items || []" :key="i" class="schema-row">
          <button
            v-if="item.image"
            type="button"
            class="schema-preview-btn schema-preview-btn--thumb"
            :aria-label="previewLabel(item)"
            @click="previewItem(item)"
          >
            <img :src="item.image" :alt="item.title || ''" class="schema-thumb">
          </button>
          <div class="schema-row-main">
            <div class="schema-row-title">{{ item.title }}</div>
            <div v-if="item.sub" class="schema-row-sub">{{ item.sub }}</div>
          </div>
          <span v-if="item.meta" class="schema-row-meta">{{ item.meta }}</span>
          <span v-if="item.badge" class="schema-badge">{{ item.badge }}</span>
          <button
            v-for="action in item.actions || []"
            :key="action.id"
            type="button"
            :class="actionClass(action)"
            :disabled="action.disabled"
            @click="fire(action)"
          >
            <Icon v-if="action.icon" :name="action.icon" />
            {{ action.label }}
          </button>
        </li>
      </ul>

      <div v-else-if="block.type === 'cards'" class="schema-cards">
        <div v-for="(item, i) in block.items || []" :key="i" class="schema-card">
          <button
            v-if="item.image"
            type="button"
            class="schema-preview-btn schema-preview-btn--card"
            :aria-label="previewLabel(item)"
            @click="previewItem(item)"
          >
            <img :src="item.image" :alt="item.title || ''" class="schema-card-image">
          </button>
          <div class="schema-card-body">
            <div class="schema-card-title">{{ item.title }}</div>
            <div v-if="item.sub" class="schema-card-sub">{{ item.sub }}</div>
            <span v-if="item.badge" class="schema-badge">{{ item.badge }}</span>
            <button
              v-for="action in item.actions || []"
              :key="action.id"
              type="button"
              class="btn"
              :disabled="action.disabled"
              @click="fire(action)"
            >
              <Icon v-if="action.icon" :name="action.icon" />
              {{ action.label }}
            </button>
          </div>
        </div>
      </div>

      <div v-else-if="block.type === 'checkbox-list'" class="schema-checkbox-list">
        <section v-for="(group, gi) in block.groups || []" :key="gi" class="schema-group">
          <div class="schema-group-head">
            <span class="schema-group-title">{{ group.title }}</span>
            <button
              v-for="action in groupActions(group)"
              :key="action.id"
              type="button"
              class="btn btn-sm"
              @click="fire(action)"
            >
              {{ action.label }}
            </button>
          </div>
          <div
            v-for="(item, ii) in group.items || []"
            :key="ii"
            class="schema-check"
          >
            <input
              :id="'schema-check-' + index + '-' + gi + '-' + ii"
              type="checkbox"
              :checked="!!checked[item.value || '']"
              @change="toggle(item.value || '')"
            >
            <button
              v-if="item.image"
              type="button"
              class="schema-preview-btn schema-preview-btn--check"
              :aria-label="previewLabel(item)"
              @click="previewItem(item)"
            >
              <img :src="item.image" :alt="item.label || ''" class="schema-check-thumb">
            </button>
            <label
              :for="'schema-check-' + index + '-' + gi + '-' + ii"
              class="schema-check-label"
            >{{ item.label || item.title }}</label>
            <span v-if="item.meta" class="schema-check-meta">{{ item.meta }}</span>
          </div>
        </section>
        <div v-if="block.submit" class="schema-actions">
          <button type="button" class="btn btn-primary" @click="submitCheckbox(block)">
            {{ block.submit.label }}
          </button>
        </div>
      </div>

      <form v-else-if="block.type === 'form'" class="schema-form" @submit.prevent="submitForm(block)">
        <fieldset v-for="group in fieldGroups(block)" :key="group.key" class="schema-form-section">
          <legend v-if="group.title" class="schema-form-section-title">{{ group.title }}</legend>
          <p v-if="group.description" class="schema-form-section-description">{{ group.description }}</p>
          <label v-for="field in group.fields" :key="field.key" class="field">
          <span>{{ field.label || field.key }}</span>
          <span v-if="field.type === 'directory'" class="schema-directory-list" style="display: flex; flex-direction: column; gap: 8px;">
            <span
              v-for="(path, index) in (dirs[field.key] || [''])"
              :key="`${field.key}-${index}`"
              class="schema-directory"
            >
              <input
                :value="path"
                type="text"
                :placeholder="field.placeholder"
                @input="setDirectoryPath(field, index, $event)"
              >
              <button
                type="button"
                class="btn btn-sm"
                :disabled="!!picking"
                @click="pickDirectory(field, field.multi ? index : undefined, $event)"
              >
                {{ isPicking(field, field.multi ? index : undefined) ? '选择中…' : '浏览…' }}
              </button>
              <button
                v-if="!field.local_only"
                type="button"
                class="btn btn-sm"
                :disabled="!!picking"
                @click="pickNetworkDirectory(field, field.multi ? index : undefined, $event)"
              >
                网络位置
              </button>
              <button
                v-if="field.multi"
                type="button"
                class="btn btn-sm"
                :disabled="!dirs[field.key] || dirs[field.key].length <= 1"
                @click="removeDirectory(field, index)"
              >
                移除
              </button>
              <button
                v-if="!field.multi && path"
                type="button"
                class="btn btn-sm"
                :disabled="!!picking"
                @click="clearDirectory(field)"
              >
                清空
              </button>
            </span>
            <button
              v-if="field.multi"
              type="button"
              class="btn btn-sm"
              :disabled="!!picking"
              @click="addDirectory(field)"
            >
              添加目录
            </button>
            <p v-if="field.emptyText" class="field-help">{{ field.emptyText }}</p>
          </span>
          <div v-else-if="field.type === 'info'" class="field-info">
            {{ String(formValues[field.key] ?? '') }}
          </div>
          <div v-else-if="field.type === 'range'" class="field-range">
            <input
              v-model.number="formValues[field.key]"
              type="range"
              :min="field.min"
              :max="field.max"
              :step="field.step"
            >
            <span class="field-range-value">{{ formValues[field.key] }}</span>
          </div>
          <select v-else-if="field.type === 'select'" v-model="formValues[field.key]">
            <option
              v-for="option in field.options || []"
              :key="optionValue(option)"
              :value="optionValue(option)"
            >
              {{ optionLabel(option) }}
            </option>
          </select>
          <textarea
            v-else-if="field.type === 'textarea'"
            :value="String(formValues[field.key] ?? '')"
            :placeholder="field.placeholder"
            @input="setFieldText(field.key, $event)"
          ></textarea>
          <input
            v-else-if="field.type === 'checkbox'"
            v-model="formValues[field.key]"
            type="checkbox"
          >
          <input
            v-else
            v-model="formValues[field.key]"
            :type="field.type === 'number' ? 'number' : (field.secret ? 'password' : 'text')"
            :min="field.min"
            :max="field.max"
            :step="field.step"
            :placeholder="field.placeholder"
          >
          <span v-if="field.hint || field.help" class="field-help">{{ field.hint || field.help }}</span>
          </label>
        </fieldset>
        <p v-if="pickError" class="schema-text schema-danger" role="alert">{{ pickError }}</p>
        <div v-if="block.submit" class="schema-actions">
          <button type="submit" class="btn btn-primary">{{ block.submit.label }}</button>
        </div>
      </form>

      <div v-else-if="block.type === 'actions'" class="schema-actions" :class="'schema-actions--' + (block.id || 'none')">
        <button
          v-for="action in block.actions || []"
          :key="action.id"
          type="button"
          :class="actionClass(action)"
          :disabled="action.disabled"
          @click="fire(action)"
        >
          <Icon v-if="action.icon" :name="action.icon" />
          {{ action.label }}
        </button>
      </div>

      <div v-else-if="block.type === 'progress'" class="schema-progress">
        <div class="schema-progress-head">
          <span class="schema-progress-text">{{ block.text || '处理中' }}</span>
          <span v-if="!block.indeterminate && block.total" class="schema-progress-count">
            {{ Math.min(100, Math.round((block.value || 0) / block.total * 100)) }}%
          </span>
        </div>
        <div
          class="schema-progress-track"
          :class="{ 'is-indeterminate': block.indeterminate }"
          role="progressbar"
          :aria-label="block.text || '进度'"
          aria-valuemin="0"
          :aria-valuemax="block.indeterminate ? undefined : (block.total || 0)"
          :aria-valuenow="block.indeterminate ? undefined : (block.value || 0)"
        >
          <div
            class="schema-progress-fill"
            :style="{ width: block.indeterminate ? undefined : (block.total ? `${Math.min(100, Math.round((block.value || 0) / block.total * 100))}%` : '0%') }"
          ></div>
        </div>
        <div v-if="block.detail" class="schema-progress-foot">
          <span class="schema-progress-detail">{{ block.detail }}</span>
        </div>
        <button v-if="block.cancel" type="button" :class="actionClass(block.cancel)" @click="fire(block.cancel)">
          {{ block.cancel.label }}
        </button>
      </div>

      <div v-else-if="block.type === 'empty'" class="empty-state">
        <Icon v-if="block.icon" :name="block.icon" class="empty-state-icon" />
        <div class="empty-state-text">{{ block.text }}</div>
        <div v-if="block.hint" class="empty-state-hint">{{ block.hint }}</div>
        <button v-if="block.submit" type="button" :class="actionClass(block.submit)" @click="fire(block.submit)">
          {{ block.submit.label }}
        </button>
      </div>

      <div v-else-if="block.type === 'image-grid'" class="schema-image-grid">
        <figure v-for="(item, i) in block.items || []" :key="i">
          <button
            type="button"
            class="schema-preview-btn schema-preview-btn--grid"
            :aria-label="previewLabel(item)"
            @click="previewItem(item)"
          >
            <img :src="item.image" :alt="item.label || item.title || ''">
          </button>
          <figcaption v-if="item.label || item.title">{{ item.label || item.title }}</figcaption>
        </figure>
      </div>
    </template>
  </div>
</template>
