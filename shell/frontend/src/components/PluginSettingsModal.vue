<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor-->
<script setup lang="ts">
/**
 * 壳内插件设置弹窗：盖住整个壳文档（含导航栏），按插件后端声明的 settings_schema 渲染。
 *
 * 与内容页里 `base.js` 的 `openSettingsModal` 的差别只有渲染位置：schema、当前值与保存
 * 全部走 `<插件>__get_settings_schema` / `<插件>__get_settings` / `<插件>__save_settings`，
 * 壳不持有任何设置语义。内容 iframe 内的 `.modal` 只相对自己的视口，盖不住壳的侧栏；
 * 设置这类页面级 chrome 因此必须由壳渲染。
 *
 * 凭据类字段（`secret: true`）不回显掩码：打开时输入框留空。留空表示"不改动"；
 * 填写新值表示覆盖；勾选「清除已保存的凭据」后提交空字符串，由 `PluginBase.save_settings`
 * 按"空串=清除"处理。不能再用"省略空值"代替显式清除。
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useBridge } from '../core/bridge'
import { getGlass, GLASS_LIMITS } from '../core/background'
import { pluginAlpha, setPluginAlpha } from '../core/shell-prefs'
import { toastError, toastSuccess } from '../core/toast'
import { useDialogFocus } from '../core/use-dialog-focus'
import type { SchemaField, SchemaFieldOption } from '../core/plugin-content'
import SettingsDirectoryField from './SettingsDirectoryField.vue'
import '../styles/settings-modal.css'

const props = defineProps<{
  plugin: string
  title?: string
  /** native companion 没有自己的页面骨架，不显示「外观」块。 */
  showAppearance?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'saved'): void
}>()

const overlayRef = ref<HTMLElement | null>(null)
const dialogRef = ref<HTMLElement | null>(null)
const { focusFirstField } = useDialogFocus({
  overlay: overlayRef,
  container: dialogRef,
  onClose: () => emit('close'),
})

const bridge = useBridge()
const shellGlass = getGlass()
const followShell = ref(pluginAlpha(props.plugin) === null)
const pluginAlphaValue = ref(pluginAlpha(props.plugin) ?? shellGlass.alpha)

function updatePluginAlpha(event: Event): void {
  const value = Number((event.target as HTMLInputElement).value)
  pluginAlphaValue.value = Math.min(
    GLASS_LIMITS.alphaMax, Math.max(GLASS_LIMITS.alphaMin, Math.round(value)))
  setPluginAlpha(props.plugin, pluginAlphaValue.value)
}

function toggleFollowShell(event: Event): void {
  const follow = (event.target as HTMLInputElement).checked
  followShell.value = follow
  setPluginAlpha(props.plugin, follow ? null : pluginAlphaValue.value)
}

const schema = ref<SchemaField[]>([])
/** 按可选 section 元数据分组；无 section 的旧 schema 归入无标题组，仍按原顺序渲染。 */
interface SettingsFieldGroup {
  key: string
  title: string
  description: string
  order: number
  first: boolean
  fields: SchemaField[]
}
const fieldGroups = computed<SettingsFieldGroup[]>(() => {
  const groups = new Map<string, {
    key: string
    title: string
    description: string
    order: number
    firstIndex: number
    fields: Array<{ field: SchemaField; index: number }>
  }>()
  schema.value.forEach((field, index) => {
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
    group.order = Math.min(
      group.order,
      Number.isFinite(sectionOrder) ? sectionOrder : index,
    )
    group.fields.push({ field, index })
  })
  const ordered = Array.from(groups.values()).map(group => {
    const fields = group.fields
      .slice()
      .sort((a, b) => {
        const ao = Number(a.field.order)
        const bo = Number(b.field.order)
        const av = Number.isFinite(ao) ? ao : a.index
        const bv = Number.isFinite(bo) ? bo : b.index
        return av - bv || a.index - b.index
      })
      .map(item => item.field)
    return {
      key: group.key,
      title: group.title,
      description: group.description,
      order: group.order,
      firstIndex: group.firstIndex,
      fields,
    }
  })
  ordered.sort((a, b) => a.order - b.order || a.firstIndex - b.firstIndex)
  return ordered.map((group, index) => ({
    key: group.key,
    title: group.title,
    description: group.description,
    order: group.order,
    first: index === 0 && props.showAppearance === false,
    fields: group.fields,
  }))
})
/** get_settings() 的原始值：只用于回填初值、只读信息行与凭据"已配置"提示。 */
const loaded = reactive<Record<string, unknown>>({})
/** 可编辑字段的草稿值；目录字段单独存字符串数组。 */
const draft = reactive<Record<string, string | number | boolean>>({})
const dirs = reactive<Record<string, string[]>>({})
/** 凭据的显式清除：勾选后提交空串，不再用"省略空值"表达清除。 */
const clearedSecrets = reactive<Record<string, boolean>>({})
const loading = ref(true)
const saving = ref(false)
const error = ref('')
/** 保存失败的汇总文案与后端字段级 errors；与"读取失败"分开，后者会替换整个表单。 */
const saveError = ref('')
const fieldErrors = reactive<Record<string, string>>({})

function directoryPaths(key: string): string[] {
  return (dirs[key] || []).filter(path => String(path).trim() !== '')
}

function setDirectoryPaths(key: string, paths: string[]): void {
  dirs[key] = paths.slice()
}

const dialogTitle = () => props.title || '插件设置'

function fieldId(field: SchemaField): string {
  return `plugin-settings-${props.plugin}-${field.key}`
}

function isMasked(value: unknown): boolean {
  return value === '********'
}

function hasStoredSecret(field: SchemaField): boolean {
  const value = loaded[field.key]
  return value !== undefined && value !== null && String(value) !== ''
}

function setSecretClear(key: string, event: Event): void {
  const checked = (event.target as HTMLInputElement).checked
  clearedSecrets[key] = checked
  if (checked) draft[key] = ''
}

function currentValue(field: SchemaField): unknown {
  if (loaded[field.key] !== undefined) return loaded[field.key]
  if (field.value !== undefined) return field.value
  return field.default
}

function normalizePaths(raw: unknown): string[] {
  if (Array.isArray(raw)) {
    return raw.map(item => String(item).trim()).filter(Boolean)
  }
  return String(raw == null ? '' : raw)
    .split('\n')
    .map(item => item.trim())
    .filter(Boolean)
}

function initDraft(fields: SchemaField[]): void {
  fields.forEach(field => {
    if (field.type === 'info') return
    if (field.type === 'directory') {
      const paths = normalizePaths(currentValue(field))
      dirs[field.key] = field.multi ? paths : paths.slice(0, 1)
      return
    }
    if (field.secret) {
      // 不把掩码灌进输入框：留空表示"不改"，要清除必须显式勾选。
      draft[field.key] = ''
      clearedSecrets[field.key] = false
      return
    }
    const value = currentValue(field)
    if (field.type === 'checkbox') {
      draft[field.key] = !!value
    } else if (field.type === 'number' || field.type === 'range') {
      const num = Number(value)
      draft[field.key] = Number.isFinite(num) ? num : 0
    } else {
      draft[field.key] = value == null ? '' : String(value)
    }
  })
}

function placeholderFor(field: SchemaField): string {
  if (field.secret) {
    if (clearedSecrets[field.key]) return '保存后清除已保存的凭据'
    return isMasked(loaded[field.key])
      ? '已配置，留空保持不变；填写新值可覆盖；勾选下方可清除'
      : (field.placeholder || '未配置，填写后保存')
  }
  if (field.type === 'directory') {
    return field.placeholder || '输入目录绝对路径'
  }
  return field.placeholder || ''
}

function optionValue(option: SchemaFieldOption | string): string {
  return typeof option === 'object' ? String(option.value ?? '') : String(option)
}

function optionLabel(option: SchemaFieldOption | string): string {
  return typeof option === 'object' ? String(option.label ?? option.value ?? '') : String(option)
}

function eventValue(ev: Event): string {
  return (ev.target as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement).value
}

function textValue(key: string): string {
  const value = draft[key]
  return value == null ? '' : String(value)
}

function numberValue(key: string): number {
  const value = Number(draft[key])
  return Number.isFinite(value) ? value : 0
}

function setText(key: string, ev: Event): void {
  draft[key] = eventValue(ev)
}

function setNumber(key: string, ev: Event): void {
  const raw = eventValue(ev)
  draft[key] = raw === '' ? 0 : Number(raw)
}

function setChecked(key: string, ev: Event): void {
  draft[key] = (ev.target as HTMLInputElement).checked
}

function infoValue(field: SchemaField): string {
  const value = currentValue(field)
  return value == null ? '' : String(value)
}

/** 回到打开弹窗时的取值（get_settings 优先，其次 schema 的 value/default）。
 *  保存时用它判断字段是否真的被改过；未变化的键不提交，避免非管理员只改普通字段
 *  却因为把 admin_only 的原值一并回传而被整单拒绝。 */
function comparableValue(field: SchemaField): string | number | boolean {
  if (field.type === 'directory') {
    const paths = normalizePaths(currentValue(field))
    return field.multi ? paths.join('\n') : (paths[0] || '')
  }
  if (field.type === 'checkbox') return !!currentValue(field)
  if (field.type === 'number' || field.type === 'range') {
    const num = Number(currentValue(field))
    return Number.isFinite(num) ? num : 0
  }
  const value = currentValue(field)
  return value == null ? '' : String(value)
}

function collect(): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  schema.value.forEach(field => {
    if (field.hidden === true || field.type === 'info') return
    if (field.type === 'directory') {
      const paths = (dirs[field.key] || [])
        .map(path => String(path).trim())
        .filter(Boolean)
      const next = field.multi ? paths.join('\n') : (paths[0] || '')
      if (next === comparableValue(field)) return
      out[field.key] = next
      return
    }
    if (field.secret) {
      // 显式清除：提交空串。留空且未勾选 = 不改动；填写新值 = 覆盖。
      if (clearedSecrets[field.key]) {
        out[field.key] = ''
        return
      }
      const value = String(draft[field.key] ?? '').trim()
      if (value) out[field.key] = value
      return
    }
    if (field.type === 'checkbox') {
      const next = !!draft[field.key]
      if (next === comparableValue(field)) return
      out[field.key] = next
      return
    }
    if (field.type === 'number' || field.type === 'range') {
      const num = Number(draft[field.key])
      if (!Number.isFinite(num)) return
      if (num === comparableValue(field)) return
      out[field.key] = num
      return
    }
    const next = String(draft[field.key] ?? '')
    if (next === comparableValue(field)) return
    out[field.key] = next
  })
  return out
}

function clearFieldErrors(): void {
  Object.keys(fieldErrors).forEach(key => { delete fieldErrors[key] })
}

async function loadSettings(): Promise<void> {
  loading.value = true
  error.value = ''
  saveError.value = ''
  clearFieldErrors()
  Object.keys(loaded).forEach(key => { delete loaded[key] })
  Object.keys(draft).forEach(key => { delete draft[key] })
  Object.keys(dirs).forEach(key => { delete dirs[key] })
  Object.keys(clearedSecrets).forEach(key => { delete clearedSecrets[key] })
  try {
    const [rawSchema, rawValues] = await Promise.all([
      bridge.call(`${props.plugin}__get_settings_schema`),
      bridge.call(`${props.plugin}__get_settings`),
    ])
    const fields: SchemaField[] = Array.isArray(rawSchema)
      ? rawSchema.filter((field): field is SchemaField => (
        !!field && typeof field === 'object' && typeof field.key === 'string'
        && field.hidden !== true
      ))
      : []
    const values = rawValues && typeof rawValues === 'object'
      ? rawValues as Record<string, unknown>
      : {}
    Object.keys(values).forEach(key => { loaded[key] = values[key] })
    schema.value = fields
    initDraft(fields)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '读取设置失败'
  } finally {
    loading.value = false
  }
}

function applySaveErrors(result: unknown): void {
  const data = result && typeof result === 'object' ? result as Record<string, unknown> : {}
  const errors = data.errors && typeof data.errors === 'object'
    ? data.errors as Record<string, unknown>
    : {}
  Object.entries(errors).forEach(([key, message]) => {
    fieldErrors[key] = String(message)
  })
  const fieldMessages = Object.values(errors).map(message => String(message)).filter(Boolean)
  saveError.value = String(data.error || fieldMessages.join('；') || '保存失败')
}

async function saveSettings(): Promise<void> {
  if (saving.value || loading.value || error.value) return
  saving.value = true
  saveError.value = ''
  clearFieldErrors()
  try {
    const result = await bridge.call(`${props.plugin}__save_settings`, collect())
    if (result && result.success === false) {
      // 后端返回字段级 errors 时保留弹窗，把文案挂到对应输入框；不要只用 toast
      // 报一句汇总后让用户自己猜是哪一项。
      applySaveErrors(result)
      return
    }
    toastSuccess('设置已保存')
    emit('saved')
    emit('close')
  } catch (e) {
    saveError.value = e instanceof Error ? e.message : '保存失败'
    toastError(saveError.value)
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await loadSettings()
  await nextTick()
  focusFirstField()
})
</script>

<template>
  <div ref="overlayRef" class="settings-modal-overlay" @pointerdown.self="emit('close')">
    <section
      ref="dialogRef"
      class="settings-modal"
      role="dialog"
      aria-modal="true"
      :aria-label="dialogTitle()"
      @pointerdown.stop
    >
      <header class="settings-modal-head">
        <h3 class="settings-modal-title">{{ dialogTitle() }}</h3>
        <button type="button" class="settings-modal-close" aria-label="关闭" @click="emit('close')">
          ×
        </button>
      </header>

      <div class="settings-modal-body obx-scroll">
        <section
          v-if="props.showAppearance !== false"
          class="settings-section settings-section--first settings-section--appearance"
        >
          <div class="settings-section-head">
            <h3 class="settings-section-title">外观</h3>
          </div>
          <div class="settings-fields">
            <div class="field field-checkbox">
              <input
                id="plugin-appearance-follow"
                type="checkbox"
                :checked="followShell"
                @change="toggleFollowShell"
              />
              <label class="field-label" for="plugin-appearance-follow">
                跟随壳透明度（{{ shellGlass.alpha }}%）
              </label>
            </div>
            <div class="field">
              <label class="field-label" for="plugin-appearance-alpha">背景不透明度</label>
              <div class="field-range">
                <input
                  id="plugin-appearance-alpha"
                  type="range"
                  :min="GLASS_LIMITS.alphaMin"
                  :max="GLASS_LIMITS.alphaMax"
                  :value="pluginAlphaValue"
                  :disabled="followShell"
                  @input="updatePluginAlpha"
                />
                <span class="field-range-value">{{ pluginAlphaValue }} %</span>
              </div>
            </div>
            <p class="field-help">
              只覆盖本插件页面的背景不透明度；取消“跟随”后数值立即生效，保存设置时会一并写入。
            </p>
          </div>
        </section>

        <div v-if="loading" class="settings-modal-state">正在读取设置…</div>
        <div v-else-if="error" class="settings-modal-state settings-modal-error">{{ error }}</div>
        <div v-else-if="!schema.length" class="settings-modal-state">该插件暂无设置项</div>
        <div v-else class="settings-form">
          <p v-if="saveError" class="settings-save-error" role="alert">{{ saveError }}</p>
          <section
            v-for="(group, groupIndex) in fieldGroups"
            :key="group.key || ('group-' + groupIndex)"
            class="settings-section"
            :class="{ 'settings-section--first': group.first }"
          >
            <div v-if="group.title || group.description" class="settings-section-head">
              <h3 v-if="group.title" class="settings-section-title">{{ group.title }}</h3>
              <p v-if="group.description" class="settings-section-description">
                {{ group.description }}
              </p>
            </div>
            <div class="settings-fields">
              <div
                v-for="field in group.fields"
                :key="field.key"
                class="field"
                :class="{
                  'field-checkbox': field.type === 'checkbox',
                  'settings-field--wide': field.type === 'directory' || field.type === 'textarea',
                }"
              >
            <template v-if="field.type === 'checkbox'">
              <input
                :id="fieldId(field)"
                type="checkbox"
                :checked="!!draft[field.key]"
                @change="setChecked(field.key, $event)"
              >
              <label class="field-label" :for="fieldId(field)">{{ field.label || field.key }}</label>
            </template>
            <template v-else>
              <label
                class="field-label"
                :class="{ required: field.required }"
                :for="fieldId(field)"
              >{{ field.label || field.key }}</label>

              <div v-if="field.type === 'info'" class="field-info">{{ infoValue(field) }}</div>

              <div v-else-if="field.type === 'range'" class="field-range">
                <input
                  :id="fieldId(field)"
                  type="range"
                  :min="field.min"
                  :max="field.max"
                  :step="field.step"
                  :value="numberValue(field.key)"
                  @input="setNumber(field.key, $event)"
                >
                <span class="field-range-value">{{ draft[field.key] }}</span>
              </div>

              <select
                v-else-if="field.type === 'select'"
                :id="fieldId(field)"
                class="settings-modal-select"
                :value="textValue(field.key)"
                @change="setText(field.key, $event)"
              >
                <option
                  v-for="option in field.options || []"
                  :key="optionValue(option)"
                  :value="optionValue(option)"
                >{{ optionLabel(option) }}</option>
              </select>

              <textarea
                v-else-if="field.type === 'textarea'"
                :id="fieldId(field)"
                :value="textValue(field.key)"
                :placeholder="placeholderFor(field)"
                @input="setText(field.key, $event)"
              ></textarea>

              <SettingsDirectoryField
                v-else-if="field.type === 'directory'"
                :paths="directoryPaths(field.key)"
                :single="!field.multi"
                :placeholder="placeholderFor(field)"
                :empty-text="field.emptyText || '未添加任何目录'"
                :local-only="field.local_only === true"
                :mount="dialogRef"
                @update:paths="setDirectoryPaths(field.key, $event)"
              />

              <input
                v-else
                :id="fieldId(field)"
                :type="field.type === 'number' ? 'number' : (field.secret ? 'password' : 'text')"
                :value="textValue(field.key)"
                :min="field.min"
                :max="field.max"
                :step="field.step"
                :placeholder="placeholderFor(field)"
                :disabled="field.secret === true && clearedSecrets[field.key] === true"
                @input="field.type === 'number' ? setNumber(field.key, $event) : setText(field.key, $event)"
              >
              <label
                v-if="field.secret && hasStoredSecret(field)"
                class="settings-secret-clear"
              >
                <input
                  type="checkbox"
                  :checked="!!clearedSecrets[field.key]"
                  @change="setSecretClear(field.key, $event)"
                >
                清除已保存的凭据
              </label>
            </template>

            <p v-if="field.help || field.hint" class="field-help">{{ field.help || field.hint }}</p>
            <p v-if="fieldErrors[field.key]" class="field-help settings-field-error" role="alert">
              {{ fieldErrors[field.key] }}
            </p>
              </div>
            </div>
          </section>
        </div>
      </div>

      <footer class="settings-modal-foot">
        <button type="button" class="btn" @click="emit('close')">取消</button>
        <button
          type="button"
          class="btn btn-primary"
          :disabled="loading || saving || !!error || !schema.length"
          @click="saveSettings"
        >{{ saving ? '保存中…' : '保存' }}</button>
      </footer>
    </section>
  </div>
</template>
