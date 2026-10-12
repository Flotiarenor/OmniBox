<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor -->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBridge } from '../core/bridge'
import { toastError, toastSuccess } from '../core/toast'
import Icon from '../components/Icon.vue'
import {
  APPEARANCE_CHANGED, applyStoredAppearance, getStoredTheme,
  setAppearanceVar, setStoredTheme,
} from '../core/appearance'
import {
  BACKGROUND_DEFAULTS, BACKGROUND_FITS, BACKGROUND_LIMITS,
  backgroundUrl, getBackground, getGlass, GLASS_LIMITS,
  samePath, setBackground, setGlass,
  type BackgroundFit, type BackgroundState, type GlassState,
} from '../core/background'
import { getStartupPrefs, setStartupPrefs, type StartPage, type StartupPrefs } from '../core/preferences'
import { clearShellVar, shellPrefs, SIDEBAR_WIDTH } from '../core/shell-prefs'

interface PluginInfo {
  name: string; displayName: string; icon: string
  route: string; entryUrl: string; keepAlive: boolean
}
interface CacheStat { path: string; count: number; bytes: number }
interface ShellInfo {
  data_root: string; config_dir: string; log_dir: string; log_file: string; log_level: string
  webview_high_performance: boolean; webview_high_performance_effective: boolean
  webview_high_performance_env: string
  caches: CacheStat[]; cache_total: { count: number; bytes: number }
}

const bridge = useBridge()
const route = useRoute()
const router = useRouter()

const SECTIONS = [
  { id: 'appearance', icon: 'palette', label: '外观' },
  { id: 'background', icon: 'images', label: '背景' },
  { id: 'startup', icon: 'rocket', label: '启动与窗口' },
  { id: 'storage', icon: 'hard-drive', label: '数据与缓存' },
  { id: 'diagnostics', icon: 'stethoscope', label: '诊断' },
  { id: 'plugins', icon: 'puzzle', label: '插件' },
  { id: 'about', icon: 'info', label: '关于' },
] as const
type SectionId = (typeof SECTIONS)[number]['id']
const active = ref<SectionId>('appearance')
// 分区标题栏的图标与左栏导航同一个名字：模板里统一写 `icon:${ICON[分区]}`，由 Icon.vue
// 解析 —— 名字只维护一份，增删分区时不会因为下标错位而串图标。
const ICON = Object.fromEntries(SECTIONS.map(s => [s.id, s.icon])) as Record<SectionId, string>


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

// 颜色之外的可调变量：与颜色共用同一条持久化通路（壳偏好的 colors 表 +
// html 上的 data-custom-colors），因此插件 iframe 里的圆角与过渡时长也会跟着一起变，
// 不需要新的同步机制。
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
  // 统一走 setAppearanceVar（读-改-写偏好表）：拖动分隔条也写同一份快照，
  // 两边各自整段覆盖对方的写法会丢值。
  setAppearanceVar(key, value)
}
function resetAppearance() {
  const defaults = readDefaultVars()
  Object.assign(customColors, defaults)
  Object.entries(defaults).forEach(([key, value]) => {
    if (value) {
      document.documentElement.style.setProperty(key, value)
      setAppearanceVar(key, value)
    } else {
      // 默认值读不到（变量只在内联里被覆盖过）：直接把这个键从偏好里删掉，
      // 让它退回 variables.css 的默认，而不是留一个空值在内联上。
      document.documentElement.style.removeProperty(key)
      clearShellVar(key)
    }
  })
  toastSuccess('外观已恢复默认')
}
function loadAppearance() {
  applyStoredAppearance()
  const map = shellPrefs().colors
  Object.keys(map).forEach(key => { customColors[key] = map[key] })
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

// ==================== 外观：各插件内边栏宽度 ====================
// 五个插件的内边栏共用 `--sub-sidebar-width` 一支 token（分隔条拖动写的也是它），
// 因此这里显式列出的就是那个共享值：滑块、拖动、双击分隔条复位三条路径同源。
// 变量名不在这里拼 —— 设置项只写共享变量，五个插件一起变。
const SIDEBAR_VAR = '--sub-sidebar-width'

const sidebarWidth = computed({
  get: () => parseInt(customColors[SIDEBAR_VAR], 10) || SIDEBAR_WIDTH.def,
  set: (width: number) => setColorVar(SIDEBAR_VAR, `${width}px`),
})

/** 复位：共享值回到默认宽度。
 *
 * 写回默认宽度而不是删除：`--sub-sidebar-width` 是拖动通路正在用的那一支，
 * 发一次明确的取值能让已挂载的 iframe 立刻跟上，不必等各自重算。 */
function resetSidebarWidth() {
  sidebarWidth.value = SIDEBAR_WIDTH.def
  toastSuccess('内边栏宽度已恢复默认')
}

// ==================== 背景：图片库、显示方式与玻璃 ====================
// 图片不走 CSS 变量广播（绝对路径会超过那套通路的 64 字符值上限），状态与 URL 都在
// core/background.ts 里；这里只负责"导入哪张、删哪张、怎么显示"。
const background = reactive(getBackground())
const glass = reactive(getGlass())

function updateBackground(patch: Partial<BackgroundState>) {
  setBackground(patch)
  Object.assign(background, getBackground())
}

/** 点缩略图：选中它当背景；点的正是当前那张时取消（开关式，没有单独的"清除"按钮）。 */
function chooseBackground(path: string) {
  if (samePath(path, background.path)) {
    updateBackground({ path: '' })
    toastSuccess('已取消背景图')
    return
  }
  updateBackground({ path })
  toastSuccess('背景图已切换')
}

function resetBackgroundLook() {
  updateBackground({ ...BACKGROUND_DEFAULTS })
  toastSuccess('已恢复默认显示方式')
}

const currentFit = computed(
  () => BACKGROUND_FITS.find(item => item.value === background.fit) || BACKGROUND_FITS[0])

function updateGlass(patch: Partial<GlassState>) {
  setGlass(patch)
  Object.assign(glass, getGlass())
}

/** 图片库条目：`<config_dir>/backgrounds` 里的图片，由后端导入 / 删除 / 枚举。 */
interface BackgroundImage { name: string; path: string; size: number; mtime: number }
const bgImages = ref<BackgroundImage[]>([])

async function loadBackgroundImages() {
  try {
    const images = await bridge.call('system_list_background_images')
    bgImages.value = Array.isArray(images) ? images : []
  } catch (e: any) {
    toastError(e.message || '读取背景图库失败')
  }
}

/** 系统文件对话框（桌面窗口模式）。用户取消返回空串；该模式不支持时给出原因。 */
async function pickImagePath(): Promise<string> {
  try {
    const result = await bridge.call('system_pick_path', 'image')
    if (result && result.success === false) {
      toastError(result.error || '打开系统文件对话框失败')
      return ''
    }
    return result?.path || ''
  } catch (e: any) {
    toastError(e.message || '打开系统文件对话框失败')
    return ''
  }
}

/** 选一张图当背景：由后端复制进图片库，再把它设为背景。
 *
 * 复制而不是引用原路径（见 shell_info 的说明）：壳级 `/file` 因此只需要放行图片库
 * 这一个目录，原图被移动或删除也不影响已设置的背景。
 */
async function pickBackgroundImage() {
  const picked = await pickImagePath()
  if (!picked) return
  try {
    const result = await bridge.call('system_import_background_image', picked)
    if (result && result.success === false) {
      toastError(result.error || '导入图片失败')
      return
    }
    const path = result?.image?.path
    if (!path) {
      toastError('导入已完成，但没有取到图片库内的路径')
      return
    }
    await loadBackgroundImages()
    updateBackground({ path })
    toastSuccess('已导入并设为背景图')
  } catch (e: any) {
    toastError(e.message || '导入图片失败')
  }
}

async function removeBackgroundImage(img: BackgroundImage) {
  try {
    const result = await bridge.call('system_delete_background_image', img.path)
    if (result && result.success === false) {
      toastError(result.error || '删除图片失败')
      return
    }
  } catch (e: any) {
    toastError(e.message || '删除图片失败')
    return
  }
  // 删掉的正是当前背景时一并清空：否则背景指向一个已删除的文件，图片请求 404，
  // 界面表现为"背景图突然不见了"，而设置页仍显示它被选中。
  if (samePath(img.path, background.path)) updateBackground({ path: '' })
  await loadBackgroundImages()
  toastSuccess(`已删除 ${img.name}`)
}

/** 打开设置页时对齐图片库与已选背景。
 *
 * 旧版本把用户目录配成壳级 `/file` 的允许根、直接引用原路径，那些路径现在不再被
 * 放行：把已选中的图片导入图片库，用户不必重新选图。源文件已不存在（或不再是图片）
 * 时清空选择并说明原因 —— 留着一个加载不出来的路径，界面只会显示"背景是空的"。
 */
async function syncBackgroundLibrary() {
  await loadBackgroundImages()
  const current = background.path
  if (!current) return
  if (bgImages.value.some(img => samePath(img.path, current))) return
  try {
    const result = await bridge.call('system_import_background_image', current)
    if (result && result.success !== false && result?.image?.path) {
      await loadBackgroundImages()
      updateBackground({ path: result.image.path })
      toastSuccess('原背景图已导入图片库')
      return
    }
  } catch {
    // 与"导入失败"同一处理：下面按原文件取不到处理
  }
  updateBackground({ path: '' })
  toastError('原背景图已不在原路径，请重新选择图片')
}

// ==================== 启动与窗口 ====================
const START_PAGES: { value: StartPage; label: string }[] = [
  { value: 'last', label: '上次离开的页面' },
  { value: 'first', label: '第一个插件' },
  { value: 'plugin', label: '指定插件' },
  { value: 'settings', label: '设置页' },
]
// 启动偏好住在壳偏好里（`shell.json` 的 prefs.startup），与主题、外观同一份落盘：
// 一个 watch 覆盖整段。改完即存 —— 这几个值只在下次启动时读，没有
// 「保存按钮忘了点」的语义。
const startup = reactive<StartupPrefs>(getStartupPrefs())
watch(startup, () => setStartupPrefs({ ...startup }))

async function toggleFullscreen() {
  try {
    const current = await bridge.call('system_get_window_fullscreen')
    if (current && current.success === false) {
      // 浏览器访问模式：说清楚为什么不能切，而不是报"已切换"却什么都没发生
      toastError(current.error || '当前没有可切换全屏的桌面窗口')
      return
    }
    const result = await bridge.call('system_set_window_fullscreen', !current?.fullscreen)
    if (result && result.success === false) toastError(result.error || '切换全屏失败')
    else toastSuccess(result?.fullscreen ? '已进入窗口全屏' : '已退出窗口全屏')
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
// 高性能 GPU 模式：改动只在下次启动生效，所以另存一份"已保存"的初值用于对比，
const gpuHighPerf = ref(true)
const gpuHighPerfSaved = ref(true)
const gpuHighPerfForcedOff = ref(false)
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
    // 存在未保存改动时不覆盖：SettingsView 以 v-show 常驻，`refreshInfo` 会在每次
    // 路由切回设置页时重跑（见下方 route watch）。无条件覆盖会丢弃已勾选但未点
    // 「应用」的改动，并使按钮回到禁用态，且不产生任何提示。
    const serverValue = info.value?.webview_high_performance !== false
    if (gpuHighPerf.value === gpuHighPerfSaved.value) {
      gpuHighPerf.value = serverValue
      gpuHighPerfSaved.value = serverValue
    }
    // 生效值为 false 而保存值为 true，说明被环境变量覆盖为关闭，界面需要给出依据。
    gpuHighPerfForcedOff.value =
      serverValue &&
      info.value?.webview_high_performance_effective === false
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

async function applyGpuHighPerf() {
  try {
    const result = await bridge.call('system_set_webview_high_performance', gpuHighPerf.value)
    if (result && result.success === false) {
      toastError(result.error || '设置高性能模式失败')
      return
    }
    gpuHighPerfSaved.value = gpuHighPerf.value
    // 是否需重启取自后端的 restart_required，不在前端写死：若后端改为即时生效，
    // 固定文案会继续提示需重启，与实际行为不符。
    const needsRestart = result?.restart_required !== false
    toastSuccess(
      (gpuHighPerf.value ? '已开启' : '已关闭') +
      (needsRestart ? '，重启程序后生效' : '，已生效')
    )
  } catch (e: any) {
    toastError(e.message || '设置高性能模式失败')
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

function onAppearanceChanged(event: Event) {
  const detail = (event as CustomEvent).detail
  if (detail && typeof detail.key === 'string') customColors[detail.key] = detail.value
}

onMounted(() => {
  loadAppearance()
  refreshInfo()
  syncBackgroundLibrary()
  // 拖动分隔条会改同一个变量，把新值同步回这份 reactive：
  // 否则滑块停在旧值上，用户一动就把拖动结果覆盖掉。
  window.addEventListener(APPEARANCE_CHANGED, onAppearanceChanged)
})

onUnmounted(() => {
  window.removeEventListener(APPEARANCE_CHANGED, onAppearanceChanged)
})

// 设置页是 v-show 常驻的：每次切回来重新拉一次，缓存占用与插件状态才不会过期；
// 图片库也可能被别处或手工改动，一并重列（此时已选背景一定在库内，不会再触发导入）。
watch(() => route.path, (path) => {
  if (path === '/settings') {
    refreshInfo()
    loadBackgroundImages()
  }
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
        <Icon :name="`icon:${s.icon}`" />
        <span class="text">{{ s.label }}</span>
      </button>
    </aside>

    <div class="settings-main">
      <!-- ==================== 外观 ==================== -->
      <section v-if="active === 'appearance'" class="settings-panel">
        <div class="settings-panel-header">
          <Icon class="panel-icon" :name="`icon:${ICON.appearance}`" />
          <span class="panel-title">外观</span>
        </div>
        <div class="settings-panel-body">
          <div class="field">
            <label class="field-label">主题模式</label>
            <div class="theme-toggle">
              <button class="btn" :class="{ active: theme === 'light' }" @click="setTheme('light')">
                <Icon name="icon:sun" /> 浅色
              </button>
              <button class="btn" :class="{ active: theme === 'dark' }" @click="setTheme('dark')">
                <Icon name="icon:moon" /> 深色
              </button>
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
            <p class="field-help">只作用于壳左侧那一条导航栏（也可直接拖动它的右边缘）。</p>
          </div>

          <div class="field">
            <label class="field-label">各插件内边栏宽度</label>
            <div class="field-range">
              <input
                id="sub-sidebar-width"
                type="range"
                :min="SIDEBAR_WIDTH.min" :max="SIDEBAR_WIDTH.max" step="10"
                v-model.number="sidebarWidth"
              />
              <span class="field-range-value">{{ sidebarWidth }} px</span>
            </div>
            <p class="field-help">
              五个插件（文档阅读 / 团体组网 / 图片相册 / 漫画库 / 媒体播放器）的内边栏共用这一支宽度：
              拖动任意一个插件的边栏改的也是它，其余插件跟着一起变；双击分隔条或按回车回到默认
              {{ SIDEBAR_WIDTH.def }} px。
            </p>
            <div class="field-inline">
              <button id="sub-sidebar-reset" class="btn" @click="resetSidebarWidth">恢复默认宽度</button>
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
              <span class="color-group-arrow"><Icon :name="group.expanded ? 'icon:chevron-down' : 'icon:chevron-right'" /></span>
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

      <!-- ==================== 背景 ==================== -->
      <!-- 背景图与它的显示方式、以及"背景怎么透出来"的玻璃效果单独成面板：
           这几项都是围绕同一张图生效的，放在「外观」里会与主题 / 配色混在一起。 -->
      <section v-if="active === 'background'" class="settings-panel">
        <div class="settings-panel-header">
          <Icon class="panel-icon" :name="`icon:${ICON.background}`" />
          <span class="panel-title">背景</span>
        </div>
        <div class="settings-panel-body">
          <div class="field">
            <label class="field-label">背景图片</label>
            <div class="field-inline">
              <button id="bg-pick" class="btn" @click="pickBackgroundImage">选择图片…</button>
            </div>
            <div v-if="bgImages.length" class="bg-grid">
              <div
                v-for="img in bgImages" :key="img.path"
                class="bg-item" :class="{ active: samePath(img.path, background.path) }"
              >
                <button class="bg-thumb" :title="img.path" @click="chooseBackground(img.path)">
                  <img :src="backgroundUrl({ path: img.path, version: 0 })" :alt="img.name" loading="lazy" />
                  <span>{{ img.name }}</span>
                </button>
                <button
                  class="bg-remove" title="从图片库删除" aria-label="从图片库删除"
                  @click="removeBackgroundImage(img)"
                >
                  <Icon name="icon:trash-2" />
                </button>
              </div>
            </div>
            <p v-else class="field-help">图片库还是空的：点「选择图片…」后图片会复制一份到配置目录，可以随时切换或删除。</p>
            <p v-if="bgImages.length" class="field-help">
              点缩略图设为背景，再点一次取消；右上角的按钮从图片库删除（不影响原图）。
              图片保存在配置目录的 backgrounds 子目录（见「数据与缓存」）。
            </p>
          </div>

          <div class="field">
            <label class="field-label">显示方式</label>
            <select
              id="bg-fit"
              :value="background.fit"
              @change="updateBackground({ fit: ($event.target as HTMLSelectElement).value as BackgroundFit })"
            >
              <option v-for="item in BACKGROUND_FITS" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
            <p class="field-help">{{ currentFit.help }}</p>
          </div>

          <div v-if="background.fit === 'free'" class="field">
            <label class="field-label">位置</label>
            <div class="field-range">
              <span class="info-label">水平</span>
              <input
                id="bg-pos-x"
                type="range" :min="BACKGROUND_LIMITS.posMin" :max="BACKGROUND_LIMITS.posMax"
                :value="background.posX"
                @input="updateBackground({ posX: Number(($event.target as HTMLInputElement).value) })"
              />
              <span class="field-range-value">{{ background.posX }} %</span>
            </div>
            <div class="field-range">
              <span class="info-label">垂直</span>
              <input
                id="bg-pos-y"
                type="range" :min="BACKGROUND_LIMITS.posMin" :max="BACKGROUND_LIMITS.posMax"
                :value="background.posY"
                @input="updateBackground({ posY: Number(($event.target as HTMLInputElement).value) })"
              />
              <span class="field-range-value">{{ background.posY }} %</span>
            </div>
            <p class="field-help">自由模式按原图尺寸摆放（object-fit: none），这两个滑块对应 object-position。</p>
          </div>

          <div class="field">
            <label class="field-label">缩放</label>
            <div class="field-range">
              <input
                id="bg-zoom"
                type="range" :min="BACKGROUND_LIMITS.zoomMin" :max="BACKGROUND_LIMITS.zoomMax" step="5"
                :value="background.zoom"
                @input="updateBackground({ zoom: Number(($event.target as HTMLInputElement).value) })"
              />
              <span class="field-range-value">{{ background.zoom }} %</span>
            </div>
            <p class="field-help">按原图尺寸重算显示尺寸，缩放的是图片本身（不是已裁好的那一块）；对每种显示方式都生效，低于 100% 时窗口里会露出应用背景色。</p>
          </div>

          <div class="field">
            <label class="field-label">翻转</label>
            <div class="field-inline">
              <button
                id="bg-flip-x" class="btn" :class="{ active: background.flipX }"
                @click="updateBackground({ flipX: !background.flipX })"
              >水平翻转</button>
              <button
                id="bg-flip-y" class="btn" :class="{ active: background.flipY }"
                @click="updateBackground({ flipY: !background.flipY })"
              >垂直翻转</button>
            </div>
            <p class="field-help">镜像由 transform: scaleX(-1) / scaleY(-1) 施加，同样对每种显示方式都生效。</p>
          </div>

          <div class="field">
            <button
              id="bg-reset" class="btn" :disabled="!background.path" @click="resetBackgroundLook"
            >恢复默认显示方式</button>
          </div>

          <div class="field">
            <label class="field-label">玻璃效果</label>
            <div class="field-range">
              <span class="info-label">模糊强度</span>
              <input
                id="glass-blur"
                type="range" :min="GLASS_LIMITS.blurMin" :max="GLASS_LIMITS.blurMax"
                :value="glass.blur"
                @input="updateGlass({ blur: Number(($event.target as HTMLInputElement).value) })"
              />
              <span class="field-range-value">{{ glass.blur }} px</span>
            </div>
            <div class="field-range">
              <span class="info-label">不透明度</span>
              <input
                id="glass-alpha"
                type="range" :min="GLASS_LIMITS.alphaMin" :max="GLASS_LIMITS.alphaMax"
                :value="glass.alpha"
                @input="updateGlass({ alpha: Number(($event.target as HTMLInputElement).value) })"
              />
              <span class="field-range-value">{{ glass.alpha }} %</span>
            </div>
            <p class="field-help">
              设置背景图后自动生效：导航栏、侧栏与设置面板半透明并模糊它（不透明面板会把
              整张背景图挡住，因此这两项不单独开关）。插件页只能"透"不能"糊"：
              iframe 是独立文档，模糊不到父页面的背景图，因此插件页里由壳提供的工具栏、
              侧栏与内容区改为半透明；插件自己写死的面板底色需要它自行加 .obx-glass
              才会一起透。
            </p>
          </div>
        </div>
      </section>

      <!-- ==================== 启动与窗口 ==================== -->
      <section v-if="active === 'startup'" class="settings-panel">
        <div class="settings-panel-header">
          <Icon class="panel-icon" :name="`icon:${ICON.startup}`" />
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

          <div class="field">
            <div class="field-checkbox-row">
              <input type="checkbox" id="gpu-high-performance" v-model="gpuHighPerf" />
              <span class="field-label">WebView2 高性能 GPU 模式</span>
            </div>
            <div class="field-inline">
              <button class="btn" :disabled="gpuHighPerf === gpuHighPerfSaved" @click="applyGpuHighPerf">应用</button>
              <span v-if="gpuHighPerf !== gpuHighPerfSaved" class="field-help">改动需重启程序后生效</span>
            </div>
            <p v-if="gpuHighPerfForcedOff" class="field-help">
              已被环境变量 {{ info?.webview_high_performance_env }} 强制关闭，此处的勾选不会生效。
            </p>
            <p class="field-help">
              强制启动WebView2 高性能 GPU 模式以缓解某些情况下桌面窗口卡顿的问题
            </p>
          </div>
        </div>
      </section>

      <!-- ==================== 数据与缓存 ==================== -->
      <section v-if="active === 'storage'" class="settings-panel">
        <div class="settings-panel-header">
          <Icon class="panel-icon" :name="`icon:${ICON.storage}`" />
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
          <Icon class="panel-icon" :name="`icon:${ICON.diagnostics}`" />
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
          <Icon class="panel-icon" :name="`icon:${ICON.plugins}`" />
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
          <Icon class="panel-icon" :name="`icon:${ICON.about}`" />
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
