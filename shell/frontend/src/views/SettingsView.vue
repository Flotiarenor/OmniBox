<!--This product includes software developed by flotiarenor.Copyright 2026 flotiarenor -->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBridge } from '../core/bridge'
import { freshnessOverviewErrorText } from '../core/plugin-chrome-protocol'
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
const REPO_URL = 'https://github.com/Flotiarenor/Cubby'
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
  await loadFreshness()
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

// ==================== 关于：检查更新 ====================
// 版本比较、资产选择、形态判定全在后端；这里只把返回的 code 投影成一段状态行。
// 失败一律写状态行，不弹 toast、不弹框 —— 用户是来看结果的，飘走的提示等于没反应。
type UpdateForm = 'installer' | 'zip' | 'portable' | 'dev' | 'platform'
type UpdateState =
  | 'idle' | 'checking' | 'up_to_date' | 'update_available'
  | 'downloading' | 'ready' | 'error' | 'disabled'
type UpdateJobState = 'idle' | 'downloading' | 'verifying' | 'ready' | 'error' | 'cancelled'

interface UpdateAsset { name: string; size: number | null }
interface UpdateResult {
  ok: boolean
  code: string
  current: string
  latest: string
  form: UpdateForm
  download_allowed: boolean
  asset: UpdateAsset | null
  html_url: string
  retry_after_seconds: number
  message: string
  checked_at: number
  cached: boolean
}
interface UpdateJob {
  state: UpdateJobState
  version: string
  asset: string
  received: number
  total: number
  percent: number
  path: string
  code: string
  message: string
}
interface UpdateStatus {
  form: UpdateForm
  enabled: boolean
  download_allowed: boolean
  reason: string
  job: UpdateJob | null
}

/** 段末那句保留在每种状态下都成立的话：检查只由用户点击触发，应用不自动装。 */
const UPDATE_TAIL = '由你点击触发检查，只提示，不自动安装。'
/** 有新版时按形态写清"将要发生什么"；dev / platform 不下载，不在这张表里。 */
const UPDATE_FORM_HELP: Partial<Record<UpdateForm, string>> = {
  installer: '将下载安装器；2.0 只下载与校验，一键覆盖安装属 2.1。',
  zip: '将下载 zip 包；2.0 只下载与校验，替换程序目录属 2.1。',
  portable: '请解压覆盖到同一目录，数据在程序目录（不可写时在默认数据根）。',
}
/** 不检查更新的原因；判据来自后端的形态判定，前端不自己推断。 */
const UPDATE_DISABLED_HELP: Partial<Record<UpdateForm, string>> = {
  dev: '开发模式不检查更新。',
  platform: 'Linux 版请在发布页手动下载。',
}
/** 下载/校验期间的重读间隔：进度要跟手，又不能把一次慢请求叠成多个定时器。 */
const UPDATE_POLL_MS = 500
/** 后端 60 秒内复用上一次检查结果；这段时间里的重复点击不发请求，只给一行说明。 */
const UPDATE_CACHE_SECONDS = 60

const updateState = ref<UpdateState>('idle')
const updateForm = ref<UpdateForm | ''>('')
/** 后端说这个形态不检查更新时不再渲染检查按钮（没有可做的事）。 */
const updateEnabled = ref(true)
const updateDownloadAllowed = ref(true)
const updateResult = ref<UpdateResult | null>(null)
const updateJob = ref<UpdateJob | null>(null)
const updateErrorText = ref('')
/** 取消 / 打开下载位置 / 信息过期这类一次性结论，附在状态行后面。 */
const updateNotice = ref('')
/** 取不到更新状态时的说明（旧内核没有这个方法，或非管理员被拒）。 */
const updateUnavailable = ref('')
const updateReason = ref('')
/** 形态与 enabled 已经对齐过一次：在此之前不渲染检查按钮，避免 dev / Linux 上闪一下。 */
const updateStatusLoaded = ref(false)
/** 限流倒计时的剩余秒数；> 0 时检查按钮禁用。 */
const retryLeft = ref(0)
const checkingUpdate = ref(false)
const startingDownload = ref(false)
const cancellingDownload = ref(false)
/** 失败来源：`download` 时「重试」重下安装包，`check` 时重新检查。 */
const updateRetryKind = ref<'check' | 'download' | ''>('')
let updatePollTimer = 0
let updateCountdownTimer = 0
/** 上一次检查结果落地的时刻（毫秒）：60 秒内的重复点击由 `cached` 判据拦在前面。 */
let updateCheckedAt = 0
/** 组件已卸载：在途的读取回来之后不得再写状态、也不得再排定时器。 */
let updateDisposed = false

/** 有新版且当前形态允许自动下载：安装器与 zip 才有「下载并校验」。 */
const canStartDownload = computed(() => (
  updateEnabled.value
  && updateDownloadAllowed.value
  && !!updateResult.value?.asset
  && updateResult.value?.code === 'update_available'
))

/** 检查按钮：失败时改叫「重试」；`bad_version` 只留发布页链接，不再给重试。 */
const showCheckButton = computed(() => (
  updateStatusLoaded.value
  && updateState.value !== 'disabled'
  && updateResult.value?.code !== 'bad_version'
))
const checkUpdateLabel = computed(() => {
  if (updateState.value === 'checking') return '正在检查…'
  if (updateState.value === 'error') {
    // 文案与动作必须一致：下载失败时点它要真的重下，不能只是重新检查一遍
    return updateRetryKind.value === 'download' ? '重试下载' : '重试'
  }
  return '检查更新'
})
const checkUpdateDisabled = computed(() => (
  checkingUpdate.value
  || startingDownload.value
  || updateState.value === 'checking'
  || updateState.value === 'downloading'
  || retryLeft.value > 0
))

const showReleasesLink = computed(() => (
  updateState.value === 'disabled'
  || (updateState.value === 'update_available' && !canStartDownload.value)
  || (updateState.value === 'error' && updateResult.value?.code === 'bad_version')
))

/** 形态说明只在"真的会下载/已经装好"时出现，平时只留段末那句。 */
const updateFormHelp = computed(() => {
  const form = updateForm.value
  const base = form ? UPDATE_FORM_HELP[form] || '' : ''
  const state = updateState.value
  const relevant = !!base && (
    state === 'downloading'
    || state === 'ready'
    // 有新版但这一版没有可用安装包时不能承诺"安装器会覆盖安装"
    || (state === 'update_available'
      && !!updateResult.value?.asset
      && updateDownloadAllowed.value)
  )
  if (relevant) return `${base}${UPDATE_TAIL}`
  // 不检查更新的形态（开发模式 / Linux）连"由你点击触发检查"都不成立
  return state === 'disabled' ? '' : UPDATE_TAIL
})

const updateDisabledText = computed(() => {
  const form = updateForm.value
  const known = form ? UPDATE_DISABLED_HELP[form] || '' : ''
  if (known) return known
  if (updateUnavailable.value) return updateUnavailable.value
  return updateReason.value ? `检查更新不可用：${updateReason.value}` : '检查更新不可用。'
})

const updateStatusBase = computed(() => {
  const result = updateResult.value
  const job = updateJob.value
  switch (updateState.value) {
    case 'checking':
      return '正在检查…'
    case 'up_to_date':
      return `已是最新版本（v${result?.latest || appVersion}）`
    case 'update_available': {
      const version = result?.latest || ''
      if (!result?.asset) {
        return `发现新版本 v${version}，这一版没有可下载的安装包，请打开发布页查看。`
      }
      if (!updateDownloadAllowed.value) {
        return `发现新版本 v${version}，当前形态不提供自动下载，请打开发布页手动下载。`
      }
      const size = result.asset.size ? `，${formatBytes(result.asset.size)}` : ''
      return `发现新版本 v${version}（当前 v${appVersion}）：${result.asset.name}${size}。`
    }
    case 'downloading': {
      const total = job?.total || 0
      if (job?.state === 'verifying') {
        return `正在校验 ${job?.asset || '安装包'}…`
      }
      const received = job?.received || 0
      const percent = total > 0
        ? Math.floor((received / total) * 100)
        : Math.round(job?.percent || 0)
      const size = total > 0 ? formatBytes(total) : '大小未知'
      return `正在下载 ${job?.asset || '安装包'}：${formatBytes(received)} / ${size}（${percent}%）`
    }
    case 'ready': {
      const path = job?.path ? `：${job.path}。` : '。'
      return `已下载并通过校验${path}一键安装将在 2.1 提供，当前可到该文件夹手动运行安装器。`
    }
    case 'error': {
      if (retryLeft.value > 0) {
        const minutes = Math.ceil(retryLeft.value / 60)
        return retryLeft.value >= 60
          ? `GitHub 限流，请 ${minutes} 分钟后重试（剩余 ${retryLeft.value} 秒）`
          : `GitHub 限流，请 ${retryLeft.value} 秒后重试`
      }
      return updateErrorText.value || '检查失败'
    }
    case 'disabled':
      return updateDisabledText.value
    default:
      return updateStatusLoaded.value ? '还没有检查过。' : '正在读取更新状态…'
  }
})

const updateStatusText = computed(() => {
  const base = updateStatusBase.value
  const notice = updateNotice.value
  if (!notice) return base
  if (updateState.value === 'checking' || updateState.value === 'disabled') return base
  // idle 下没有别的结论可写，状态行直接就是这条提示（例如"信息已过期，请重新检查"）
  if (updateState.value === 'idle') return notice
  return `${base}（${notice}）`
})

function stopUpdatePoll(): void {
  if (updatePollTimer) {
    window.clearTimeout(updatePollTimer)
    updatePollTimer = 0
  }
}

function stopUpdateCountdown(): void {
  if (updateCountdownTimer) {
    window.clearTimeout(updateCountdownTimer)
    updateCountdownTimer = 0
  }
}

/** 限流倒计时：每秒自排一次，到 0 停（`retryLeft` 归零后按钮自然可用）。 */
function tickUpdateCountdown(): void {
  stopUpdateCountdown()
  if (retryLeft.value <= 0 || route.path !== '/settings') return
  updateCountdownTimer = window.setTimeout(() => {
    updateCountdownTimer = 0
    retryLeft.value = Math.max(0, retryLeft.value - 1)
    tickUpdateCountdown()
  }, 1000)
}

/**
 * 这一页还在用户看得见的位置：未卸载、仍在设置页、仍停在「关于」段。
 *
 * 在途的读取回来之后要按它判一次 —— 否则 `resetUpdateState()` 刚刚收回的状态会被
 * 迟到的响应写回来，卸载之后也还会再排一个定时器。
 */
function updateViewVisible(): boolean {
  return !updateDisposed && route.path === '/settings' && active.value === 'about'
}

/**
 * 下载 / 校验期间按 500 ms 重读一次进度。
 *
 * 用自排的 setTimeout 而不是 setInterval：一次慢请求会让 setInterval 的触发叠在一起，
 * 自排则"这一次读完了才排下一次"。看不见这一页（切到别的分区、离开设置页）就不排。
 */
function scheduleUpdatePoll(): void {
  stopUpdatePoll()
  if (!updateViewVisible()) return
  if (updateState.value !== 'downloading') return
  updatePollTimer = window.setTimeout(async () => {
    updatePollTimer = 0
    await refreshUpdateState()
  }, UPDATE_POLL_MS)
}

async function refreshUpdateState(): Promise<void> {
  try {
    const status = (await bridge.call('system_get_update_state')) as UpdateStatus | null
    if (updateViewVisible()) applyUpdateStatus(status)
  } catch {
    // 轮询失败不打断下载：下一次轮询或用户手动检查会重新对齐，不往状态行写瞬时错误
  }
  scheduleUpdatePoll()
}

function applyUpdateStatus(status: UpdateStatus | null): void {
  if (!status) return
  if (status.form) updateForm.value = status.form
  if (typeof status.download_allowed === 'boolean') {
    updateDownloadAllowed.value = status.download_allowed
  }
  updateReason.value = String(status.reason || '')
  if (status.enabled === false) {
    updateEnabled.value = false
    updateState.value = 'disabled'
    stopUpdatePoll()
    stopUpdateCountdown()
    retryLeft.value = 0
    return
  }
  updateEnabled.value = true
  if (updateState.value === 'disabled') updateState.value = 'idle'
  const job = status.job
  if (!job || job.state === 'idle') {
    // 后端已经没有升级动作（进程重启、状态被清）：本地不能停在「下载中」，
    // 否则会每 500 ms 读一次、界面上却没有任何可点的动作。
    if (updateState.value === 'downloading') {
      stopUpdatePoll()
      updateJob.value = null
      updateState.value = canStartDownload.value ? 'update_available' : 'idle'
      updateNotice.value = '下载状态已重置，请重新开始'
    }
    return
  }
  applyUpdateJob(job)
}

function applyUpdateJob(job: UpdateJob): void {
  updateJob.value = job
  switch (job.state) {
    case 'downloading':
    case 'verifying':
      updateState.value = 'downloading'
      updateErrorText.value = ''
      scheduleUpdatePoll()
      return
    case 'ready':
      updateState.value = 'ready'
      updateErrorText.value = ''
      stopUpdatePoll()
      return
    case 'error':
      updateState.value = 'error'
      updateErrorText.value = downloadFailureText(job)
      updateRetryKind.value = 'download'
      stopUpdatePoll()
      return
    case 'cancelled':
      stopUpdatePoll()
      if (updateState.value === 'downloading') {
        updateState.value = canStartDownload.value ? 'update_available' : 'idle'
      }
      return
    default:
      return
  }
}

/** 下载失败的一行原因：`job.message` 里的异常串只在末选，能分类的先分类。 */
function downloadFailureText(job: UpdateJob): string {
  switch (job.code) {
    case 'network':
      return '下载失败：连不上 GitHub'
    case 'digest_mismatch':
      return '安装包校验不通过，已删除，请重试'
    case 'timeout':
      return '下载超时，已中止，请重试'
    case 'too_large':
      return '安装包超过允许的大小上限'
    case 'io_error':
      return '写出安装包失败，请检查磁盘空间'
    default:
      return job.message || '下载失败'
  }
}

/** 进入「关于」段时对齐形态、是否可检查、以及可能仍在跑的下载。 */
async function loadUpdateState(): Promise<void> {
  if (route.path !== '/settings') return
  try {
    const status = (await bridge.call('system_get_update_state')) as UpdateStatus | null
    if (updateDisposed) return
    applyUpdateStatus(status)
    updateUnavailable.value = ''
  } catch (e: any) {
    if (updateDisposed) return
    // 冻结契约里失败也是 `ok=false` 的信封；走到这里说明接口不存在或通道断了
    const message = String((e as { message?: string } | null)?.message || e || '')
    updateEnabled.value = false
    updateState.value = 'disabled'
    updateUnavailable.value = /403|禁止访问|管理员|权限/.test(message)
      ? '只有管理员可以检查更新。'
      : '这个版本没有提供检查更新。'
  }
  if (updateDisposed) return
  if (retryLeft.value > 0) tickUpdateCountdown()
  updateStatusLoaded.value = true
  scheduleUpdatePoll()
}

function applyUpdateResult(result: UpdateResult | null): void {
  if (!result) {
    updateState.value = 'error'
    updateErrorText.value = '检查失败'
    updateRetryKind.value = 'check'
    return
  }
  updateResult.value = result
  updateCheckedAt = Date.now()
  // 检查本身失败才会走到下面的 error 分支；下载失败在 applyUpdateJob / applyStartResult
  // 里单独登记为 `download`，两处不能混。
  updateRetryKind.value = 'check'
  if (result.form) updateForm.value = result.form
  if (typeof result.download_allowed === 'boolean') {
    updateDownloadAllowed.value = result.download_allowed
  }
  switch (result.code) {
    case 'update_available':
      updateState.value = 'update_available'
      return
    case 'no_asset':
      updateState.value = 'update_available'
      return
    case 'up_to_date':
      updateState.value = 'up_to_date'
      if (result.cached) {
        updateNotice.value = `刚刚检查过，${UPDATE_CACHE_SECONDS} 秒内复用上次结果`
      }
      return
    case 'disabled':
      updateEnabled.value = false
      updateState.value = 'disabled'
      return
    case 'rate_limited':
      updateState.value = 'error'
      // 倒计时期间状态行显示"请 N 分钟后重试"，到期后落到这句
      updateErrorText.value = 'GitHub 限流，请稍后重试'
      retryLeft.value = Math.max(0, Math.round(Number(result.retry_after_seconds) || 0))
      tickUpdateCountdown()
      return
    case 'forbidden':
      updateState.value = 'error'
      // 「GitHub 拒绝访问」与「当前使用者不是管理员」是两回事：后者是通道级 403，
      // 只由 checkUpdate / loadUpdateState 的 catch 分支处理，这里不看 message。
      updateErrorText.value = '检查失败：没有权限访问 GitHub'
      return
    case 'network':
      updateState.value = 'error'
      updateErrorText.value = '检查失败：连不上 GitHub'
      return
    case 'not_found':
      updateState.value = 'error'
      updateErrorText.value = '没找到发布信息'
      return
    case 'bad_version':
      updateState.value = 'error'
      updateErrorText.value = '版本号无法比较，请到发布页手动查看'
      return
    default:
      updateState.value = 'error'
      updateErrorText.value = '检查失败'
  }
}

async function checkUpdate(): Promise<void> {
  if (checkingUpdate.value || retryLeft.value > 0) return
  if (updateState.value === 'downloading') return
  // 后端 60 秒内复用上一次结果，这段时间里再点一次不会真的发请求：
  // 必须有可见的反馈，否则按钮看起来像坏了。
  if (updateState.value === 'up_to_date'
      && Date.now() - updateCheckedAt < UPDATE_CACHE_SECONDS * 1000) {
    updateNotice.value = `刚刚检查过，${UPDATE_CACHE_SECONDS} 秒内不会重复请求`
    return
  }
  checkingUpdate.value = true
  updateNotice.value = ''
  updateErrorText.value = ''
  updateState.value = 'checking'
  stopUpdatePoll()
  try {
    const result = (await bridge.call('system_check_update', appVersion)) as UpdateResult | null
    if (updateDisposed) return
    applyUpdateResult(result)
  } catch (e: any) {
    if (updateDisposed) return
    // 后端不抛异常；这里接住的是通道级失败（非管理员 403、方法不存在）
    const message = String((e as { message?: string } | null)?.message || e || '')
    updateState.value = 'error'
    updateRetryKind.value = 'check'
    updateErrorText.value = /403|禁止访问|管理员|权限/.test(message)
      ? '只有管理员可以检查更新'
      : '检查失败：接口不可用'
  } finally {
    checkingUpdate.value = false
  }
}

/** 重试按失败来源分派：下载失败重下安装包，检查失败重新检查。 */
async function retryUpdate(): Promise<void> {
  if (updateRetryKind.value === 'download' && canStartDownload.value) {
    await startDownload()
    return
  }
  await checkUpdate()
}

function enterDownloading(version: string, assetName: string, total: number): void {
  const current = updateJob.value
  updateJob.value = {
    state: 'downloading',
    version,
    asset: assetName || current?.asset || '',
    received: current?.received || 0,
    total: total || current?.total || 0,
    percent: current?.percent || 0,
    path: '',
    code: '',
    message: '',
  }
  updateState.value = 'downloading'
  updateErrorText.value = ''
  updateNotice.value = ''
  scheduleUpdatePoll()
}

function applyStartResult(started: any, version: string, asset: UpdateAsset): void {
  if (started?.started) {
    enterDownloading(version, asset.name, Number(started.total) || Number(asset.size) || 0)
    return
  }
  // 已有下载在跑：不新开一份，跟上它的进度
  if (started?.running) {
    enterDownloading(version, asset.name, 0)
    return
  }
  // 上一次的产物还通过校验，直接进 ready
  if (started?.ready) {
    updateJob.value = {
      state: 'ready',
      version,
      asset: asset.name,
      received: Number(asset.size) || 0,
      total: Number(asset.size) || 0,
      percent: 100,
      path: String(started.path || ''),
      code: '',
      message: '',
    }
    updateState.value = 'ready'
    updateErrorText.value = ''
    updateNotice.value = ''
    stopUpdatePoll()
    return
  }
  switch (started?.error) {
    case 'stale':
      updateResult.value = null
      updateJob.value = null
      updateState.value = 'idle'
      updateNotice.value = '信息已过期，请重新检查'
      return
    case 'not_allowed':
      updateState.value = 'error'
      updateRetryKind.value = 'check'
      updateErrorText.value = '当前形态不能自动下载，请打开发布页手动下载'
      return
    case 'busy':
      updateState.value = 'error'
      updateRetryKind.value = 'download'
      updateErrorText.value = '已有升级动作在进行，请稍后再试'
      return
    case 'too_large':
      updateState.value = 'error'
      // 重下拿到的还是同一个超限资产，只能重新检查
      updateRetryKind.value = 'check'
      updateErrorText.value = '安装包超过允许的大小上限'
      return
    case 'no_result':
      updateState.value = 'error'
      updateRetryKind.value = 'check'
      updateErrorText.value = '没有可用的检查结果，请重新检查'
      return
    default:
      updateState.value = 'error'
      updateRetryKind.value = 'download'
      updateErrorText.value = '下载没有结果，请重试'
  }
}

async function startDownload(): Promise<void> {
  if (startingDownload.value) return
  const result = updateResult.value
  const asset = result?.asset
  if (!result || !asset || !canStartDownload.value) return
  startingDownload.value = true
  try {
    const started = await bridge.call('system_start_update_download', result.latest, asset.name)
    if (updateDisposed) return
    applyStartResult(started, result.latest, asset)
  } catch {
    if (updateDisposed) return
    updateState.value = 'error'
    updateRetryKind.value = 'download'
    updateErrorText.value = '启动下载失败'
  } finally {
    startingDownload.value = false
  }
}

async function cancelDownload(): Promise<void> {
  if (cancellingDownload.value) return
  cancellingDownload.value = true
  try {
    const result = await bridge.call('system_cancel_update_download')
    if (result && (result.cancelled || result.running === false)) {
      stopUpdatePoll()
      updateState.value = canStartDownload.value ? 'update_available' : 'idle'
      updateNotice.value = result.cancelled ? '下载已取消' : ''
    }
  } catch {
    updateNotice.value = '取消失败，下载仍在进行'
  } finally {
    cancellingDownload.value = false
  }
}

async function openUpdateDir(): Promise<void> {
  try {
    const result = await bridge.call('system_open_update_dir')
    if (result && result.success === false) {
      updateNotice.value = result.error || '打开下载位置失败'
    } else {
      updateNotice.value = ''
    }
  } catch {
    updateNotice.value = '打开下载位置失败'
  }
}

/** 离开设置页 / 卸载组件：状态只在内存里，回来当作没检查过。 */
function resetUpdateState(): void {
  stopUpdatePoll()
  stopUpdateCountdown()
  // 不可检查的形态保持 disabled：否则返回时检查按钮会闪一下再被状态快照收回
  updateState.value = updateEnabled.value ? 'idle' : 'disabled'
  updateResult.value = null
  updateJob.value = null
  updateErrorText.value = ''
  updateNotice.value = ''
  updateUnavailable.value = ''
  updateRetryKind.value = ''
  updateCheckedAt = 0
  retryLeft.value = 0
  checkingUpdate.value = false
  startingDownload.value = false
  cancellingDownload.value = false
}

// ==================== 数据与缓存：统一刷新状态 ====================
// 各插件的「校验」在它自己的工具栏里；没有页面骨架的插件（清理器、下载器）没有那一条
// 工具栏，这张表就是它们的刷新入口：状态投影 + 重建（丢弃派生缓存后重新全量扫描）。
interface FreshnessTask { state?: string; total?: number; processed?: number }
interface FreshnessState {
  plugin: string
  available?: boolean
  busy?: boolean
  entries?: number
  dirs?: number
  unit?: string
  mode?: string
  version_stale?: boolean
  stale_dirs?: number
  /** 当前挂不上的根数量（盘未挂载 / 目录被移走）：这条与"待校验"是两回事 */
  unreachable_roots?: number | unknown[]
  last_sync_at?: number
  last_verify_at?: number
  ttl_seconds?: number
  ttl_remaining?: number
  task?: FreshnessTask | null
  error?: string
}

const freshnessStates = ref<FreshnessState[]>([])
/** 概览读取失败的原因；'' = 正常。权限与"旧内核不提供"是永久结论，transient 是瞬时故障。 */
type FreshnessProblem = '' | 'admin' | 'unsupported' | 'transient'
const freshnessProblem = ref<FreshnessProblem>('')
const freshnessUnavailable = ref('')
const FRESHNESS_TRANSIENT_TEXT = '暂时读不到统一刷新状态，可重试。'
/** 瞬时失败的重试上限：抖动要能自己恢复，持续失败时不能无限每 1.5 秒敲一次。 */
const FRESHNESS_RETRY_LIMIT = 3
let freshnessRetries = 0
const rebuildingPlugin = ref('')
/** 后台校验进行中时的概览重读定时器（有任务在跑才转，见 scheduleFreshnessPoll）。 */
const FRESHNESS_POLL_MS = 1500
let freshnessPollTimer = 0

function stopFreshnessPoll(): void {
  if (freshnessPollTimer) {
    window.clearTimeout(freshnessPollTimer)
    freshnessPollTimer = 0
  }
}

/**
 * 概览读取失败的三分类。
 *
 * 概览是管理员专属端点：403 必须说权限，旧内核没有这个方法才说"不提供"，其余（网络
 * 抖动、后端重启、5xx）是瞬时失败 —— 把后者说成"旧内核"会把人引到错误的排查方向，
 * 而且会让这张表永久停在错误态。
 */
function overviewFailureKind(error: unknown): Exclude<FreshnessProblem, ''> {
  const message = String((error as { message?: string } | null)?.message || error || '')
  if (/403|禁止访问|管理员|权限/.test(message)) return 'admin'
  // 旧内核：HTTP 通道是 404 的「页面不存在」，桌面模式是 "xxx is not a function"。
  if (/404|页面不存在|不存在|not found|not a function|未知(方法|接口)/i.test(message)) {
    return 'unsupported'
  }
  return 'transient'
}

/**
 * 有任务在跑（重建 / 校验）时按 1.5 秒重读概览：没有它，表格会永远停在"校验中…"。
 *
 * 瞬时失败也继续重试（有上限）：否则一次网络抖动就把这张表永久停掉；权限 / 旧内核
 * 结论不再重试（再读也是同一句）。
 */
function scheduleFreshnessPoll(): void {
  stopFreshnessPoll()
  // 设置页是 v-show 常驻的：不在这一页时不该为一张看不见的表轮询。
  if (route.path !== '/settings') return
  const busy = freshnessStates.value.some(item => item.busy)
  const retrying = freshnessProblem.value === 'transient' && freshnessRetries < FRESHNESS_RETRY_LIMIT
  if (!busy && !retrying) return
  freshnessPollTimer = window.setTimeout(async () => {
    freshnessPollTimer = 0
    if (freshnessProblem.value === 'transient') freshnessRetries += 1
    await loadFreshness()
  }, FRESHNESS_POLL_MS)
}

async function loadFreshness() {
  try {
    const list = await bridge.call('system_freshness_overview')
    freshnessStates.value = Array.isArray(list) ? list : []
    freshnessProblem.value = ''
    freshnessUnavailable.value = ''
    freshnessRetries = 0
  } catch (e) {
    // 整块降级，不影响其余信息。三分类见 overviewFailureKind：权限不足与"旧内核没有
    // 这个接口"分开说；瞬时失败保留上一份概览（尤其 busy 项），轮询因此不会断。
    const problem = overviewFailureKind(e)
    freshnessProblem.value = problem
    freshnessUnavailable.value = problem === 'transient'
      ? FRESHNESS_TRANSIENT_TEXT
      : freshnessOverviewErrorText(e)
    if (problem !== 'transient') freshnessStates.value = []
  }
  scheduleFreshnessPoll()
}

function unreachableRoots(item: FreshnessState): number {
  const raw = item.unreachable_roots
  if (Array.isArray(raw)) return raw.length
  const n = Number(raw)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0
}

function pluginLabel(name: string): string {
  return plugins.value.find(p => p.name === name)?.displayName || name
}

/** 状态列：一个单元格说清"现在在做什么、还剩多少没做"。 */
function freshnessStatus(item: FreshnessState): string {
  if (item.available === false) return item.error ? `不可用：${item.error}` : '不可用'
  const task = item.task || {}
  if (item.busy) return task.total ? `校验中 ${task.processed || 0}/${task.total}` : '校验中…'
  const unreachable = unreachableRoots(item)
  if (unreachable) {
    return item.stale_dirs
      ? `部分目录不可达（${unreachable}）· ${item.stale_dirs} 个目录待重算`
      : `部分目录不可达（${unreachable}）`
  }
  if (item.mode === 'remote' && item.ttl_seconds) {
    return item.ttl_remaining && item.ttl_remaining > 0
      ? `缓存 ${Math.ceil(item.ttl_remaining)} 秒后过期`
      : '缓存已过期'
  }
  if (item.version_stale) {
    return item.stale_dirs ? `待校验 · ${item.stale_dirs} 个目录待重算` : '待校验'
  }
  const unit = item.unit || '项'
  const indexed = item.entries ? `已索引 ${item.entries} ${unit}` : ''
  const stamp = item.last_verify_at ? `校验于 ${shellTimeAgo(item.last_verify_at)}`
    : (item.last_sync_at ? `同步于 ${shellTimeAgo(item.last_sync_at)}` : '')
  return [indexed, stamp || '尚未校验'].filter(Boolean).join(' · ')
}

/** `Freshness.timeAgo` 由壳首页加载的 `/shell/freshness.js` 提供；取不到时退成日期。 */
function shellTimeAgo(ts: number): string {
  const api = (window as unknown as { Freshness?: { timeAgo?: (value: number) => string } }).Freshness
  if (api && typeof api.timeAgo === 'function') return api.timeAgo(ts)
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

async function rebuildFreshness(item: FreshnessState) {
  const label = pluginLabel(item.plugin)
  const ok = window.confirm(
    `重建「${label}」的派生缓存？\n`
    + '会丢弃指纹库与派生索引后重新全量扫描；文件多时需要较长时间。',
  )
  if (!ok) return
  rebuildingPlugin.value = item.plugin
  try {
    const result = await bridge.call('system_freshness_rebuild', item.plugin, 'derived')
    if (result && result.started === false) {
      if (result.error === 'unsupported') toastError('这个插件没有参与统一刷新')
      else if (result.running) toastError('已有任务在跑，完成后再试')
      else toastError(result.error || '重建启动失败')
    } else {
      toastSuccess(`已开始重建「${label}」`)
    }
  } catch (e: any) {
    toastError(e.message || '重建失败')
  } finally {
    rebuildingPlugin.value = ''
    await loadFreshness()
  }
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
  // 概览轮询不能在页面卸载后继续：定时器持有组件的闭包与 bridge 句柄
  stopFreshnessPoll()
  // 检查更新的轮询与限流倒计时同理：状态只在内存里，卸载即回 idle。
  // 先置 disposed 再复位：在途的那一次读取回来时就不许再写状态、也不许再排定时器。
  updateDisposed = true
  resetUpdateState()
})

// 设置页是 v-show 常驻的：每次切回来重新拉一次，缓存占用与插件状态才不会过期；
// 图片库也可能被别处或手工改动，一并重列（此时已选背景一定在库内，不会再触发导入）。
watch(() => route.path, (path) => {
  if (path === '/settings') {
    // 回到设置页是一次新的尝试：瞬时失败的退避预算重置。
    freshnessRetries = 0
    refreshInfo()
    loadBackgroundImages()
    // 「关于」段可能停在别的分区之外：只有正看着它才需要重新对齐更新状态
    if (active.value === 'about') loadUpdateState()
  } else {
    // 离开设置页就停掉概览轮询，回来时由 refreshInfo 重新决定要不要转
    stopFreshnessPoll()
    // 检查更新的状态只在内存里：离开设置页即清定时器、清倒计时、回 idle
    resetUpdateState()
  }
})

// 分区是 v-if 切换的：进入「关于」才取一次形态与 job，离开就不再为看不见的状态行轮询。
watch(active, (section) => {
  if (section === 'about' && route.path === '/settings') {
    loadUpdateState()
    return
  }
  stopUpdatePoll()
  stopUpdateCountdown()
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

          <div class="field">
            <label class="field-label">统一刷新</label>
            <p v-if="freshnessUnavailable" class="field-help">
              {{ freshnessUnavailable }}
              <button
                v-if="freshnessProblem === 'transient'"
                class="btn btn-sm"
                @click="loadFreshness()"
              >重试</button>
            </p>
            <template v-if="freshnessStates.length">
              <div class="table-scroll">
                <table class="data-table">
                  <thead>
                    <tr>
                      <th>插件</th>
                      <th class="num">条目</th>
                      <th class="num">目录</th>
                      <th>状态</th>
                      <th class="num"></th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="item in freshnessStates" :key="item.plugin">
                      <td :title="item.plugin">{{ pluginLabel(item.plugin) }}</td>
                      <td class="num">{{ item.entries ?? 0 }}</td>
                      <td class="num">{{ item.dirs ?? 0 }}</td>
                      <td>{{ freshnessStatus(item) }}</td>
                      <td class="num">
                        <button
                          class="btn btn-sm"
                          :disabled="!!item.busy || rebuildingPlugin === item.plugin"
                          @click="rebuildFreshness(item)"
                        >重建…</button>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p class="field-help">
                进入插件时自动做增量同步，插件工具栏的「校验」逐项重算并清理已消失的条目。
                重建是逃生门：丢弃指纹库与派生索引后重新全量扫描，用于外部工具保留了修改时间、
                缓存被截断这类无法靠比对发现的变化。没有页面工具栏的插件（例如清理器、下载器）
                也在这张表里。
              </p>
            </template>
            <p v-else-if="!freshnessUnavailable" class="field-help">{{ loading ? '读取中…' : '还没有插件参与统一刷新。' }}</p>
          </div>
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
            <span class="info-value">格子铺 Cubby v{{ appVersion }}</span>
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
          <div class="field">
            <label class="field-label">检查更新</label>
            <div class="field-inline">
              <button
                v-if="showCheckButton"
                id="settings-check-update"
                class="btn"
                :disabled="checkUpdateDisabled"
                @click="retryUpdate"
              >{{ checkUpdateLabel }}</button>
              <button
                v-if="updateState === 'update_available' && canStartDownload"
                id="settings-start-update"
                class="btn btn-primary"
                :disabled="startingDownload"
                @click="startDownload"
              >下载并校验</button>
              <button
                v-if="updateState === 'downloading'"
                id="settings-cancel-update"
                class="btn"
                :disabled="cancellingDownload"
                @click="cancelDownload"
              >取消</button>
              <button
                v-if="updateState === 'ready'"
                id="settings-open-update-dir"
                class="btn"
                @click="openUpdateDir"
              >打开下载位置</button>
              <a
                v-if="showReleasesLink"
                id="settings-open-releases"
                class="info-value"
                :href="RELEASES_URL" target="_blank" rel="noopener"
              >打开发布页</a>
            </div>
            <p
              id="settings-update-status"
              class="field-help"
              role="status"
              aria-live="polite"
            >{{ updateStatusText }}</p>
            <p v-if="updateFormHelp" class="field-help">{{ updateFormHelp }}</p>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>
