<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useBridge } from '../core/bridge'
import { toastError, toastSuccess } from '../core/toast'

interface SchemaField {
  key: string
  label?: string
  type?: string
  default?: unknown
  placeholder?: string
  help?: string
  options?: Array<{ label: string; value: string } | string>
  min?: number
  max?: number
  step?: number
  required?: boolean
}

interface SettingsPanel {
  name: string
  displayName: string
  icon: string
  schema: SchemaField[]
  values: Record<string, unknown>
}

const bridge = useBridge()
const panels = ref<SettingsPanel[]>([])
const loading = ref(true)
const saving = ref('')
const error = ref('')
const drafts = reactive<Record<string, Record<string, any>>>({})

function fieldDefault(f: SchemaField): unknown {
  return f.default !== undefined ? f.default : (f.type === 'checkbox' ? false : '')
}

function initDraft(p: SettingsPanel) {
  const d: Record<string, unknown> = {}
  p.schema.forEach((f) => {
    d[f.key] = p.values[f.key] !== undefined ? p.values[f.key] : fieldDefault(f)
  })
  drafts[p.name] = d
}

onMounted(async () => {
  try {
    const list = await bridge.call('system_settings_list')
    panels.value = list
    list.forEach((p: SettingsPanel) => initDraft(p))
  } catch (e: any) {
    error.value = e.message || '加载设置失败'
  } finally {
    loading.value = false
  }
})

async function savePanel(p: SettingsPanel) {
  saving.value = p.name
  try {
    const result = await bridge.call('system_settings_save', p.name, drafts[p.name])
    if (result && result.success === false) {
      toastError(result.error || '保存失败')
    } else {
      toastSuccess(`「${p.displayName}」设置已保存`)
    }
  } catch (e: any) {
    toastError(e.message || '保存失败')
  } finally {
    saving.value = ''
  }
}

function resetPanel(p: SettingsPanel) {
  p.schema.forEach((f) => {
    drafts[p.name][f.key] = fieldDefault(f)
  })
  savePanel(p)
}

function optionLabel(opt: { label: string; value: string } | string): string {
  return typeof opt === 'object' ? opt.label : opt
}
function optionValue(opt: { label: string; value: string } | string): string {
  return typeof opt === 'object' ? opt.value : opt
}
</script>

<template>
  <div class="settings-page">
    <div class="settings-page-header">
      <h1>设置</h1>
      <p>集中管理所有插件的配置，插件内的设置弹窗使用同一套配置项定义。</p>
    </div>

    <div v-if="loading" class="loading">设置加载中...</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else-if="panels.length === 0" class="settings-empty">
      当前没有插件声明设置项
    </div>

    <div v-else class="settings-grid">
      <div v-for="p in panels" :key="p.name" class="settings-panel">
        <div class="settings-panel-header">
          <span class="panel-icon">{{ p.icon }}</span>
          <span class="panel-title">{{ p.displayName }}</span>
        </div>

        <div class="settings-panel-body">
          <div v-if="p.schema.length === 0" class="field-help">该插件暂无设置项</div>
          <div v-else class="settings-form">
            <div
              v-for="f in p.schema"
              :key="f.key"
              class="field"
              :class="{ 'field-checkbox': f.type === 'checkbox' }"
            >
              <template v-if="f.type === 'checkbox'">
                <div class="field-checkbox-row">
                  <input
                    type="checkbox"
                    v-model="drafts[p.name][f.key]"
                  />
                  <span class="field-label">{{ f.label || f.key }}</span>
                </div>
              </template>

              <template v-else>
                <label class="field-label" :class="{ required: f.required }">
                  {{ f.label || f.key }}
                </label>

                <div v-if="f.type === 'range'" class="field-range">
                  <input
                    type="range"
                    v-model.number="drafts[p.name][f.key]"
                    :min="f.min"
                    :max="f.max"
                    :step="f.step"
                  />
                  <span class="field-range-value">{{ drafts[p.name][f.key] }}</span>
                </div>

                <select v-else-if="f.type === 'select'" v-model="drafts[p.name][f.key]">
                  <option
                    v-for="opt in f.options"
                    :key="optionValue(opt)"
                    :value="optionValue(opt)"
                  >
                    {{ optionLabel(opt) }}
                  </option>
                </select>

                <textarea
                  v-else-if="f.type === 'textarea'"
                  v-model="drafts[p.name][f.key]"
                  :placeholder="f.placeholder"
                ></textarea>

                <input
                  v-else
                  :type="f.type === 'number' ? 'number' : 'text'"
                  v-model="drafts[p.name][f.key]"
                  :min="f.min"
                  :max="f.max"
                  :step="f.step"
                  :placeholder="f.placeholder"
                />
              </template>

              <p v-if="f.help" class="field-help">{{ f.help }}</p>
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
