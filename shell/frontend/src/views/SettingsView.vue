<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor -->
<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBridge } from '../core/bridge'
import { toastError, toastSuccess } from '../core/toast'
import { applyStoredAppearance, getStoredTheme, persistCustomColors, setStoredTheme } from '../core/appearance'
import { getStartupPrefs, setStartupPrefs, type StartPage, type StartupPrefs } from '../core/preferences'

interface PluginInfo {
  name: string; displayName: string; icon: string
  route: string; entryUrl: string; keepAlive: boolean
}
interface CacheStat { path: string; count: number; bytes: number }
interface ShellInfo {
  data_root: string; config_dir: string; log_dir: string; log_file: string; log_level: string
  caches: CacheStat[]; cache_total: { count: number; bytes: number }
}

const bridge = useBridge()
const route = useRoute()
const router = useRouter()

const SECTIONS = [
  { id: 'appearance', icon: '🎨', label: '外观' },
  { id: 'startup', icon: '🚀', label: '启动与窗口' },
  { id: 'storage', icon: '💾', label: '数据与缓存' },
  { id: 'diagnostics', icon: '🩺', label: '诊断' },
  { id: 'plugins', icon: '🧩', label: '插件' },
  { id: 'about', icon: 'ℹ️', label: '关于' },
] as const
type SectionId = (typeof SECTIONS)[number]['id']
const active = ref<SectionId>('appearance')

// ==================== 外观：主题 ====================
// 主题的落盘与 data-theme 写入都归 appearance.ts（首屏脚本走同一份，
// 这里不再自己 setAttribute + localStorage，避免两处写法漂移）。
const theme = ref<'light' | 'dark'>(getStoredTheme())

function setTheme(t: 'light' | 'dark') {
  theme.value = t
  setStoredTheme(t)
}

// ==================== 外观：可调 CSS 变量 ====================
interface ColorVar { key: string; label: string; presets: { name: string; value: string }[] }
interface ColorGroup { label: string; expanded: boolean; variables: ColorVar[] }

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

// 颜色之外的可调变量：与颜色共用同一条持久化通路（localStorage 的
// omni-custom-colors + html 上的 data-custom-colors），因此插件 iframe 里的
// 圆角与过渡时长也会跟着一起变，不需要新的同步机制。
const EXTRA_VARS = [
  '--radius', '--radius-sm', '--radius-lg',
  '--nav-width', '--transition-fast', '--transition-normal',
]

function allVarKeys(): string[] {
  return [...colorGroups.flatMap(g => g.variables.map(v => v.key)), ...EXTRA_VARS]
}

/** 读「没有内联覆盖时」的默认值。
 *
 * variables.css 的 :root 默认值与用户的自定义值落在同一个元素（html）上，
 * 不先摘掉 style 属性，读到的就是用户当前的值 —— 「恢复默认」会变成空操作。
 * 期间不绘制（同一个同步任务内读完即还原），不会闪一下。
 */
function readDefaultVars(): Record<string, string> {
  const el = document.documentElement
  const inline = el.getAttribute('style')
  el.removeAttribute('style')
  const style = getComputedStyle(el)
  const out: Record<string, string> = {}
  allVarKeys().forEach(key => { out[key] = style.getPropertyValue(key).trim() })
  if (inline !== null) el.setAttribute('style', inline)
  return out
}

// 挂载前 main.ts 已套用过持久化的自定义值，所以这里显式取默认值打底
const customColors = reactive<Record<string, string>>(readDefaultVars())

function setColorVar(key: string, value: string) {
  customColors[key] = value
  document.documentElement.style.setProperty(key, value)
  persistCustomColors(customColors)
}
function resetAppearance() {
  const defaults = readDefaultVars()
  Object.assign(customColors, defaults)
  Object.entries(defaults).forEach(([key, value]) => {
    if (value) document.documentElement.style.setProperty(key, value)
  })
  persistCustomColors(customColors)
  toastSuccess('外观已恢复默认')
}
function loadAppearance() {
  applyStoredAppearance()
  const saved = localStorage.getItem('omni-custom-colors')
  if (!saved) return
  try {
    const map = JSON.parse(saved) as Record<string, string>
    Object.keys(map).forEach(key => { customColors[key] = map[key] })
  } catch {
    // 存储内容损坏时按未配置处理（与 appearance.ts 同一策略）
  }
}

// 一组变量一起变（圆角三个 token / 动效两个时长 token）：下拉里选的是预设，
// 落盘仍是逐个 CSS 变量，所以「恢复默认」与插件侧同步都不需要额外分支。
const SHAPE_PRESETS = [
  { name: '直角', vars: { '--radius': '0px', '--radius-sm': '0px', '--radius-lg': '0px' } },
  { name: '小圆角', vars: { '--radius': '4px', '--radius-sm': '2px', '--radius-lg': '6px' } },
  { name: '默认', vars: { '--radius': '6px', '--radius-sm': '4px', '--radius-lg': '10px' } },
  { name: '大圆角', vars: { '--radius': '10px', '--radius-sm': '8px', '--radius-lg': '16px' } },
]
const MOTION_PRESETS = [
  { name: '正常', vars: { '--transition-fast': '0.15s ease', '--transition-normal': '0.25s ease' } },
  { name: '精简', vars: { '--transition-fast': '0.08s ease', '--transition-normal': '0.12s ease' } },
  { name: '关闭', vars: { '--transition-fast': '0s', '--transition-normal': '0s' } },
]

function presetIndex(presets: { vars: Record<string, string> }[]): number {
  return presets.findIndex(p => Object.entries(p.vars).every(([key, value]) => customColors[key] === value))
}
function applyPreset(presets: { vars: Record<string, string> }[], index: number) {
  const preset = presets[index]
  if (preset) Object.entries(preset.vars).forEach(([key, value]) => setColorVar(key, value))
}

const shapeIndex = computed({
  get: () => presetIndex(SHAPE_PRESETS),
  set: (index: number) => applyPreset(SHAPE_PRESETS, index),
})
const motionIndex = computed({
  get: () => presetIndex(MOTION_PRESETS),
  set: (index: number) => applyPreset(MOTION_PRESETS, index),
})
const navWidth = computed({
  get: () => parseInt(customColors['--nav-width'], 10) || 200,
  set: (width: number) => setColorVar('--nav-width', `${width}px`),
})

// ==================== 启动与窗口 ====================
const START_PAGES: { value: StartPage; label: string }[] = [
  { value: 'last', label: '上次离开的页面' },
  { value: 'first', label: '第一个插件' },
  { value: 'plugin', label: '指定插件' },
  { value: 'settings', label: '设置页' },
]
const startup = reactive<StartupPrefs>(getStartupPrefs())
// 改完即存：这几个值只在下次启动时读，没有「保存按钮忘了点」的语义
watch(startup, () => setStartupPrefs({ ...startup }))

async function toggleFullscreen() {
  try {
    const result = await bridge.call('system_toggle_fullscreen')
    if (result && result.success === false) toastError(result.error || '切换全屏失败')
    else toastSuccess('已切换窗口全屏')
  } catch (e: any) {
    toastError(e.message || '切换全屏失败')
  }
}

// ==================== 数据与缓存 / 诊断 / 插件 / 关于 ====================
const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR']
const REPO_URL = 'https://github.com/Flotiarenor/OmniBox'
const RELEASES_URL = `${REPO_URL}/releases/latest`

const plugins = ref<PluginInfo[]>([])
const info = ref<ShellInfo | null>(null)
const failures = ref<{ name: string; reason: string }[]>([])
const loading = ref(false)
const logLevel = ref('INFO')
const appVersion = typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : 'dev'

async function refreshInfo() {
  loading.value = true
  try {
    const [list, shellInfo] = await Promise.all([
      bridge.call('system_get_plugins'),
      bridge.call('system_get_shell_info'),
    ])
    plugins.value = Array.isArray(list) ? list : []
    info.value = shellInfo || null
    if (info.value?.log_level) logLevel.value = info.value.log_level
  } catch (e: any) {
    toastError(e.message || '读取系统信息失败')
  }
  try {
    const status = await bridge.call('system_get_plugin_status')
    failures.value = (status && status.failures) || []
  } catch {
    // 老版本内核没有这个接口时静默降级，不影响其余信息
    failures.value = []
  }
  loading.value = false
}

async function applyLogLevel() {
  try {
    const result = await bridge.call('system_set_log_level', logLevel.value)
    if (result && result.success === false) toastError(result.error || '设置日志级别失败')
    else toastSuccess(`日志级别已改为 ${logLevel.value}`)
  } catch (e: any) {
    toastError(e.message || '设置日志级别失败')
  }
}

async function clearCaches() {
  if (!window.confirm('清空所有插件的缩略图缓存？下次浏览时插件会按需重新生成。')) return
  try {
    const result = await bridge.call('system_clear_thumb_caches')
    if (result && result.success === false) toastError(result.error || '清理缓存失败')
    else {
      toastSuccess(`已释放 ${formatBytes(result?.freed_bytes || 0)}`)
      await refreshInfo()
    }
  } catch (e: any) {
    toastError(e.message || '清理缓存失败')
  }
}

async function openLogDir() {
  try {
    const result = await bridge.call('system_open_log_dir')
    if (result && result.success === false) toastError(result.error || '打开日志目录失败')
  } catch (e: any) {
    toastError(e.message || '打开日志目录失败')
  }
}

function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  return `${(bytes / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`
}

onMounted(() => {
  loadAppearance()
  refreshInfo()
})

// 设置页是 v-show 常驻的：每次切回来重新拉一次，缓存占用与插件状态才不会过期
watch(() => route.path, (path) => {
  if (path === '/settings') refreshInfo()
})
</script>

<template>
  <div class="settings-page">
    <aside class="settings-nav">
      <div class="settings-nav-title">设置</div>
      <button
        v-for="s in SECTIONS" :key="s.id"
        class="obx-nav-item settings-nav-item" :class="{ active: active === s.id }"
        @click="active = s.id"
      >
        <span class="icon">{{ s.icon }}</span>
        <span class="text">{{ s.label }}</span>
      </button>
    </aside>

    <div class="settings-main">
      <!-- ==================== 外观 ==================== -->
      <section v-if="active === 'appearance'" class="settings-panel">
        <div class="settings-panel-header">
          <span class="panel-icon">🎨</span>
          <span class="panel-title">外观</span>
        </div>
        <div class="settings-panel-body">
          <div class="field">
            <label class="field-label">主题模式</label>
            <div class="theme-toggle">
              <button class="btn" :class="{ active: theme === 'light' }" @click="setTheme('light')">☀️ 浅色</button>
              <button class="btn" :class="{ active: theme === 'dark' }" @click="setTheme('dark')">🌙 深色</button>
            </div>
          </div>

          <div class="field">
            <label class="field-label">界面圆角</label>
            <select v-model.number="shapeIndex">
              <option v-for="(p, i) in SHAPE_PRESETS" :key="p.name" :value="i">{{ p.name }}</option>
            </select>
            <p class="field-help">同时作用于面板、按钮与插件页面（圆角 token 由壳注入）。</p>
          </div>

          <div class="field">
            <label class="field-label">导航栏宽度</label>
            <div class="field-range">
              <input type="range" min="160" max="320" step="10" v-model.number="navWidth" />
              <span class="field-range-value">{{ navWidth }} px</span>
            </div>
          </div>

          <div class="field">
            <label class="field-label">界面动效</label>
            <select v-model.number="motionIndex">
              <option v-for="(p, i) in MOTION_PRESETS" :key="p.name" :value="i">{{ p.name }}</option>
            </select>
            <p class="field-help">「关闭」把过渡时长置 0，插件页面的动画一并静默。</p>
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
            <button class="btn" @click="resetAppearance">恢复默认外观</button>
          </div>
        </div>
      </section>

      <!-- ==================== 启动与窗口 ==================== -->
      <section v-if="active === 'startup'" class="settings-panel">
        <div class="settings-panel-header">
          <span class="panel-icon">🚀</span>
          <span class="panel-title">启动与窗口</span>
        </div>
        <div class="settings-panel-body">
          <div class="field">
            <label class="field-label">启动时打开</label>
            <select v-model="startup.page">
              <option v-for="p in START_PAGES" :key="p.value" :value="p.value">{{ p.label }}</option>
            </select>
          </div>

          <div v-if="startup.page === 'plugin'" class="field">
            <label class="field-label">目标插件</label>
            <select v-model="startup.route">
              <option value="">（未选择）</option>
              <option v-for="p in plugins" :key="p.name" :value="p.route">{{ p.icon }} {{ p.displayName }}</option>
            </select>
            <p class="field-help">插件被移除或改名后自动回退到第一个插件。</p>
          </div>

          <div class="field">
            <div class="field-checkbox-row">
              <input type="checkbox" id="startup-fullscreen" v-model="startup.fullscreen" />
              <span class="field-label">启动后进入全屏</span>
            </div>
            <p class="field-help">仅桌面窗口有效；浏览器访问时忽略。全屏状态下随时可以用下面的按钮切回来。</p>
          </div>

          <div class="field">
            <button class="btn" @click="toggleFullscreen">切换窗口全屏</button>
          </div>
        </div>
      </section>

      <!-- ==================== 数据与缓存 ==================== -->
      <section v-if="active === 'storage'" class="settings-panel">
        <div class="settings-panel-header">
          <span class="panel-icon">💾</span>
          <span class="panel-title">数据与缓存</span>
        </div>
        <div class="settings-panel-body">
          <template v-if="info">
            <div class="info-row">
              <span class="info-label">数据根目录</span>
              <code class="info-value" :title="info.data_root">{{ info.data_root || '—' }}</code>
            </div>
            <div class="info-row">
              <span class="info-label">配置目录</span>
              <code class="info-value" :title="info.config_dir">{{ info.config_dir }}</code>
            </div>

            <div class="field">
              <label class="field-label">缩略图缓存</label>
              <div v-if="info.caches.length" class="table-scroll">
                <table class="data-table">
                  <thead>
                    <tr><th>缓存文件</th><th class="num">条目</th><th class="num">占用</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="c in info.caches" :key="c.path">
                      <td class="mono" :title="c.path">{{ c.path }}</td>
                      <td class="num">{{ c.count }}</td>
                      <td class="num">{{ formatBytes(c.bytes) }}</td>
                    </tr>
                  </tbody>
                  <tfoot>
                    <tr>
                      <td>合计 {{ info.caches.length }} 个缓存</td>
                      <td class="num">{{ info.cache_total.count }}</td>
                      <td class="num">{{ formatBytes(info.cache_total.bytes) }}</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
              <p v-else class="field-help">还没有缩略图缓存（插件生成第一张缩略图后出现）。</p>
            </div>

            <div class="field">
              <button class="btn" :disabled="!info.caches.length" @click="clearCaches">清空缩略图缓存</button>
              <p class="field-help">清空后释放磁盘占用，插件在下次浏览时按需重新生成。</p>
            </div>
          </template>
          <div v-else class="field-help">{{ loading ? '读取中…' : '暂时读不到系统信息。' }}</div>
        </div>
      </section>

      <!-- ==================== 诊断 ==================== -->
      <section v-if="active === 'diagnostics'" class="settings-panel">
        <div class="settings-panel-header">
          <span class="panel-icon">🩺</span>
          <span class="panel-title">诊断</span>
        </div>
        <div class="settings-panel-body">
          <div class="field">
            <label class="field-label">日志级别</label>
            <div class="field-inline">
              <select v-model="logLevel">
                <option v-for="l in LOG_LEVELS" :key="l" :value="l">{{ l }}</option>
              </select>
              <button class="btn" @click="applyLogLevel">应用</button>
            </div>
            <p class="field-help">立即生效并保存，下次启动沿用；DEBUG 会为每张缩略图记一行，排查完记得调回 INFO。</p>
          </div>

          <div class="info-row">
            <span class="info-label">日志文件</span>
            <code class="info-value" :title="info?.log_file">{{ info?.log_file || '—' }}</code>
          </div>

          <div class="field">
            <button class="btn" @click="openLogDir">打开日志目录</button>
          </div>

          <div class="field">
            <label class="field-label">插件加载失败</label>
            <ul v-if="failures.length" class="fail-list">
              <li v-for="f in failures" :key="f.name">
                <strong>{{ f.name }}</strong><span class="mono">{{ f.reason }}</span>
              </li>
            </ul>
            <p v-else class="field-help">全部插件加载正常。</p>
          </div>
        </div>
      </section>

      <!-- ==================== 插件 ==================== -->
      <section v-if="active === 'plugins'" class="settings-panel">
        <div class="settings-panel-header">
          <span class="panel-icon">🧩</span>
          <span class="panel-title">插件</span>
        </div>
        <div class="settings-panel-body">
          <p class="field-help settings-hint">
            插件的设置项由插件自己维护：打开插件页面后点它自己的「设置」按钮。
          </p>
          <div v-if="plugins.length" class="table-scroll">
            <table class="data-table">
              <thead>
                <tr><th>插件</th><th>标识</th><th>路由</th><th>常驻</th><th></th></tr>
              </thead>
              <tbody>
                <tr v-for="p in plugins" :key="p.name">
                  <td>{{ p.icon }} {{ p.displayName }}</td>
                  <td class="mono">{{ p.name }}</td>
                  <td class="mono">{{ p.route }}</td>
                  <td>{{ p.keepAlive ? '是' : '否' }}</td>
                  <td class="num"><button class="btn" @click="router.push(p.route)">打开</button></td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="field-help">{{ loading ? '读取中…' : '当前没有已加载的插件。' }}</p>
        </div>
      </section>

      <!-- ==================== 关于 ==================== -->
      <section v-if="active === 'about'" class="settings-panel">
        <div class="settings-panel-header">
          <span class="panel-icon">ℹ️</span>
          <span class="panel-title">关于</span>
        </div>
        <div class="settings-panel-body">
          <div class="info-row">
            <span class="info-label">版本</span>
            <span class="info-value">OmniBox {{ appVersion }}</span>
          </div>
          <div class="info-row">
            <span class="info-label">许可</span>
            <span class="info-value">Apache License 2.0</span>
          </div>
          <div class="info-row">
            <span class="info-label">项目主页</span>
            <a class="info-value" :href="REPO_URL" target="_blank" rel="noopener">{{ REPO_URL }}</a>
          </div>
          <div class="info-row">
            <span class="info-label">新版本</span>
            <a class="info-value" :href="RELEASES_URL" target="_blank" rel="noopener">{{ RELEASES_URL }}</a>
          </div>
          <p class="field-help">更新需要手动下载安装，应用本身不会联网检查或自动更新。</p>
        </div>
      </section>
    </div>
  </div>
</template>
