<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor -->
<script setup lang="ts">
import { onMounted, reactive, ref, computed, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useBridge } from '../core/bridge'
import { toastError, toastSuccess } from '../core/toast'
import { applyStoredAppearance } from '../core/appearance'

interface SchemaField {
  key: string; label?: string; type?: string; default?: unknown
  placeholder?: string; help?: string
  options?: Array<{ label: string; value: string } | string>
  min?: number; max?: number; step?: number; required?: boolean
  central?: boolean
}
interface SettingsPanel {
  name: string; displayName: string; icon: string
  schema: SchemaField[]; values: Record<string, unknown>
}

const bridge = useBridge()
const route = useRoute()
const panels = ref<SettingsPanel[]>([])
const loading = ref(true)
const saving = ref('')
const error = ref('')
const drafts = reactive<Record<string, Record<string, any>>>({})

// ——— Theme & Color ———
const theme = ref<'light' | 'dark'>(
  (localStorage.getItem('omni-theme') as 'light' | 'dark') ||
  (document.documentElement.getAttribute('data-theme') as 'light' | 'dark') || 'dark'
)

type ColorVar = {
  key: string; label: string
  presets: { name: string; value: string }[]
}
type ColorGroup = {
  label: string; expanded: boolean
  variables: ColorVar[]
}

const _bgPresets = (light: string, dark: string): { name: string; value: string }[] => [
  { name: '深色默认', value: dark }, { name: '浅色默认', value: light },
  { name: '纯黑', value: '#000000' }, { name: '纯白', value: '#ffffff' },
  { name: '蓝灰', value: '#1e293b' }, { name: '暖灰', value: '#3d3d3d' },
]
const _textPresets = (light: string, dark: string): { name: string; value: string }[] => [
  { name: '深色默认', value: dark }, { name: '浅色默认', value: light },
  { name: '纯黑', value: '#000000' }, { name: '纯白', value: '#ffffff' },
  { name: '柔和', value: '#9ca3af' },
]
const _accentPresets: { name: string; value: string }[] = [
  { name: '默认蓝', value: '#2f81f7' }, { name: '蓝(VS)', value: '#0078d4' },
  { name: '紫', value: '#8b5cf6' }, { name: '绿', value: '#10b981' },
  { name: '橙', value: '#f97316' }, { name: '红', value: '#ef4444' },
  { name: '粉', value: '#ec4899' }, { name: '青', value: '#06b6d4' },
]

const colorGroups = reactive<ColorGroup[]>([
  { label: '背景色', expanded: false, variables: [
    { key: '--bg-app', label: '应用背景', presets: _bgPresets('#f3f3f3','#0d1117') },
    { key: '--bg-surface', label: '面板背景', presets: _bgPresets('#ffffff','#161b22') },
    { key: '--bg-sub-sidebar', label: '侧边栏背景', presets: _bgPresets('#ffffff','#161b22') },
    { key: '--bg-hover', label: '悬停背景', presets: _bgPresets('#e9ecef','rgba(255,255,255,0.08)') },
    { key: '--bg-active', label: '选中背景', presets: _bgPresets('#cfe6fa','rgba(0,120,212,0.2)') },
  ]},
  { label: '文本色', expanded: false, variables: [
    { key: '--text-primary', label: '主文本', presets: _textPresets('#1a1a1a','#c9d1d9') },
    { key: '--text-secondary', label: '次要文本', presets: _textPresets('#6c757d','#8b949e') },
    { key: '--text-muted', label: '弱化文本', presets: _textPresets('#adb5bd','#6e7681') },
  ]},
  { label: '边框色', expanded: false, variables: [
    { key: '--border', label: '边框', presets: [
      { name: '深色默认', value: '#30363d' }, { name: '浅色默认', value: '#dee2e6' },
      { name: '深灰', value: '#21262d' }, { name: '浅灰', value: '#e9ecef' },
    ]},
  ]},
  { label: '强调色', expanded: false, variables: [
    { key: '--accent', label: '强调色', presets: _accentPresets },
    { key: '--accent-hover', label: '悬停强调', presets: [
      { name: '深色默认', value: '#58a6ff' }, { name: '浅色默认', value: '#0056b3' },
      ..._accentPresets.slice(2),
    ]},
    { key: '--danger', label: '危险色', presets: [
      { name: '深色默认', value: '#f85149' }, { name: '浅色默认', value: '#dc3545' },
      { name: '红', value: '#ef4444' }, { name: '暗红', value: '#b91c1c' },
    ]},
    { key: '--success', label: '成功色', presets: [
      { name: '深色默认', value: '#3fb950' }, { name: '浅色默认', value: '#28a745' },
      { name: '绿', value: '#10b981' }, { name: '暗绿', value: '#047857' },
    ]},
  ]},
])

const defaultColors = computed(() => {
  const s = getComputedStyle(document.documentElement)
  const m: Record<string, string> = {}
  colorGroups.forEach(g => g.variables.forEach(v => { m[v.key] = s.getPropertyValue(v.key).trim() || '' }))
  return m
})
const customColors = reactive<Record<string, string>>({ ...defaultColors.value })

function setTheme(t: 'light' | 'dark') {
  theme.value = t
  document.documentElement.setAttribute('data-theme', t)
  localStorage.setItem('omni-theme', t)
}
function setColorVar(key: string, value: string) {
  customColors[key] = value
  document.documentElement.style.setProperty(key, value)
  persistCustomColors()
}
function persistCustomColors() {
  const json = JSON.stringify(customColors)
  localStorage.setItem('omni-custom-colors', json)
  document.documentElement.setAttribute('data-custom-colors', json)
}
function resetColors() {
  const d = defaultColors.value
  Object.assign(customColors, d)
  Object.entries(d).forEach(([k, v]) => document.documentElement.style.setProperty(k, v))
  persistCustomColors()
}
function loadCustomColors() {
  applyStoredAppearance()
  const saved = localStorage.getItem('omni-custom-colors')
  if (saved) {
    try {
      const map = JSON.parse(saved) as Record<string, string>
      Object.assign(customColors, map)
      Object.entries(map).forEach(([k, v]) => document.documentElement.style.setProperty(k, v))
    } catch {}
  }
}

async function fetchSettings() {
  loading.value = true
  try {
    const list = await bridge.call('system_settings_list')
    panels.value = list
    list.forEach((p: SettingsPanel) => initDraft(p))
  } catch (e: any) {
    error.value = e.message || '加载设置失败'
  } finally { loading.value = false }
}

onMounted(() => {
  loadCustomColors()
  fetchSettings()
})

watch(() => route.path, (path) => {
  if (path === '/settings') fetchSettings()
})

function fieldDefault(f: SchemaField): unknown {
  return f.default !== undefined ? f.default : (f.type === 'checkbox' ? false : '')
}
function initDraft(p: SettingsPanel) {
  const d: Record<string, unknown> = {}
  p.schema.forEach(f => { d[f.key] = p.values[f.key] !== undefined ? p.values[f.key] : fieldDefault(f) })
  drafts[p.name] = d
}
async function savePanel(p: SettingsPanel) {
  saving.value = p.name
  try {
    const result = await bridge.call('system_settings_save', p.name, drafts[p.name])
    if (result && result.success === false) toastError(result.error || '保存失败')
    else {
      toastSuccess(`「${p.displayName}」设置已保存`)
      const iframe = document.querySelector(`iframe[data-plugin-name="${p.name}"]`) as HTMLIFrameElement | null
      if (iframe?.contentWindow) {
        iframe.contentWindow.postMessage({ type: 'omnibox:settings-changed' }, '*')
      }
    }
  } catch (e: any) { toastError(e.message || '保存失败') }
  finally { saving.value = '' }
}
function resetPanel(p: SettingsPanel) {
  p.schema.forEach(f => { drafts[p.name][f.key] = fieldDefault(f) })
  savePanel(p)
}
function optionLabel(opt: any): string { return typeof opt === 'object' ? opt.label : opt }
function optionValue(opt: any): string { return typeof opt === 'object' ? opt.value : opt }
</script>

<template>
  <div class="settings-page">
    <div class="settings-page-header">
      <h1>设置</h1>
      <p>集中管理所有配置。</p>
    </div>

    <!-- 外观设置 -->
    <div class="settings-panel">
      <div class="settings-panel-header">
        <span class="panel-icon">🎨</span>
        <span class="panel-title">外观设置</span>
      </div>
      <div class="settings-panel-body">
        <div class="field">
          <label class="field-label">主题模式</label>
          <div class="theme-toggle">
            <button class="btn" :class="{ active: theme === 'light' }" @click="setTheme('light')">☀️ 浅色</button>
            <button class="btn" :class="{ active: theme === 'dark' }" @click="setTheme('dark')">🌙 深色</button>
          </div>
        </div>

        <div v-for="group in colorGroups" :key="group.label" class="color-section">
          <div class="color-group-header" @click="group.expanded = !group.expanded">
            <span class="color-group-arrow">{{ group.expanded ? '▼' : '▶' }}</span>
            <span class="color-group-label">{{ group.label }}</span>
            <span class="color-group-count">{{ group.variables.length }}项</span>
          </div>
          <div v-if="group.expanded" class="color-group-body">
            <div v-for="v in group.variables" :key="v.key" class="color-row">
              <span class="color-label">{{ v.label }}</span>
              <div class="color-select-wrap">
                <span class="color-dot" :style="{ background: customColors[v.key] || '#000' }" />
                <select
                  class="color-select"
                  :value="customColors[v.key] || ''"
                  @change="setColorVar(v.key, ($event.target as HTMLSelectElement).value)"
                >
                  <option
                    v-for="p in v.presets" :key="p.value"
                    :value="p.value"
                    :selected="customColors[v.key] === p.value"
                  >{{ p.name }}</option>
                </select>
              </div>
            </div>
          </div>
        </div>

        <div style="margin-top:12px;">
          <button class="btn" @click="resetColors">恢复默认颜色</button>
        </div>
      </div>
    </div>

    <!-- 加载/错误 -->
    <div v-if="loading" class="loading">设置加载中...</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else-if="panels.length === 0" class="settings-empty">当前没有插件声明设置项</div>

    <!-- 插件设置面板 -->
    <div v-else class="settings-grid">
      <div v-for="p in panels" :key="p.name" class="settings-panel">
        <div class="settings-panel-header">
          <span class="panel-icon">{{ p.icon }}</span>
          <span class="panel-title">{{ p.displayName }}</span>
        </div>
        <div class="settings-panel-body">
          <div v-if="p.schema.length === 0" class="field-help">该插件暂无设置项</div>
          <div v-else class="settings-form">
            <div v-for="f in p.schema" :key="f.key" class="field" :class="{ 'field-checkbox': f.type === 'checkbox' }">
              <template v-if="f.type === 'checkbox'">
                <div class="field-checkbox-row">
                  <input type="checkbox" v-model="drafts[p.name][f.key]" />
                  <span class="field-label">{{ f.label || f.key }}</span>
                </div>
              </template>
              <template v-else>
                <label class="field-label" :class="{ required: f.required }">
                  {{ f.label || f.key }}
                  <span v-if="f.help" class="field-tip" :title="f.help">?</span>
                </label>
                <div v-if="f.type === 'range'" class="field-range">
                  <input type="range" v-model.number="drafts[p.name][f.key]" :min="f.min" :max="f.max" :step="f.step" />
                  <span class="field-range-value">{{ drafts[p.name][f.key] }}</span>
                </div>
                <select v-else-if="f.type === 'select'" v-model="drafts[p.name][f.key]">
                  <option v-for="opt in f.options" :key="optionValue(opt)" :value="optionValue(opt)">{{ optionLabel(opt) }}</option>
                </select>
                <textarea v-else-if="f.type === 'textarea'" v-model="drafts[p.name][f.key]" :placeholder="f.placeholder"></textarea>
                <input v-else :type="f.type === 'number' ? 'number' : 'text'" v-model="drafts[p.name][f.key]" :min="f.min" :max="f.max" :placeholder="f.placeholder" />
              </template>
            </div>
          </div>
        </div>
        <div class="settings-panel-footer">
          <button class="btn" @click="resetPanel(p)">恢复默认</button>
          <button class="btn btn-primary" :disabled="saving === p.name" @click="savePanel(p)">
            {{ saving === p.name ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
