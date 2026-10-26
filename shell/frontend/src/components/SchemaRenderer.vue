<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<script setup lang="ts">
/**
 * native 内容的唯一渲染器。
 *
 * 输入是插件给的 blocks，输出是壳的组件；交互只回传 {action, payload}，
 * 不把业务规则带进壳。canvas 内容不走这里。
 */
import { nextTick, ref, watch } from 'vue'
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
  for (const block of props.blocks || []) {
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
  }
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
</script>

<template>
  <div class="schema-root">
    <template v-for="(block, index) in blocks" :key="block.id || index">
      <p v-if="block.type === 'text'" class="schema-text" :class="block.tone ? `schema-${block.tone}` : ''">
        {{ block.text }}
      </p>

      <h2 v-else-if="block.type === 'heading'" class="schema-heading">{{ block.text }}</h2>

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
            class="btn"
            :class="action.variant === 'danger' ? 'btn-danger' : action.variant === 'primary' ? 'btn-primary' : ''"
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
        <label v-for="field in block.fields || []" :key="field.key" class="field">
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
          class="btn"
          :class="action.variant === 'danger' ? 'btn-danger' : action.variant === 'primary' ? 'btn-primary' : ''"
          :disabled="action.disabled"
          @click="fire(action)"
        >
          <Icon v-if="action.icon" :name="action.icon" />
          {{ action.label }}
        </button>
      </div>

      <div v-else-if="block.type === 'progress'" class="schema-progress">
        <div class="schema-progress-head">
          <span>{{ block.text || '处理中' }}</span>
          <span>{{ block.value || 0 }} / {{ block.total || 0 }}</span>
        </div>
        <div class="schema-progress-track">
          <div
            class="schema-progress-fill"
            :style="{ width: block.total ? `${Math.min(100, Math.round((block.value || 0) / block.total * 100))}%` : '30%' }"
          ></div>
        </div>
        <div v-if="block.detail" class="schema-progress-detail">{{ block.detail }}</div>
        <button v-if="block.cancel" type="button" class="btn btn-sm" @click="fire(block.cancel)">
          {{ block.cancel.label }}
        </button>
      </div>

      <div v-else-if="block.type === 'empty'" class="empty-state">
        <div class="empty-state-text">{{ block.text }}</div>
        <div v-if="block.hint" class="empty-state-hint">{{ block.hint }}</div>
        <button v-if="block.submit" type="button" class="btn" @click="fire(block.submit)">
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
